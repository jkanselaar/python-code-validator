#!/usr/bin/env python3
"""Serve the hosted validator to MCP clients that speak stdio.

Clients that only launch a command — and sandboxes that introspect a server by
starting it — cannot reach `https://api.statemind.ai/mcp` themselves. This is
the bridge: a JSON-RPC loop on stdin/stdout that forwards `tools/call` to the
service over HTTPS. Standard library only, so `python3 mcp_stdio.py` is the
whole installation.

    {"mcpServers": {"python-code-validator": {"command": "python3",
                                              "args": ["mcp_stdio.py"]}}}

`VALIDATOR_API_KEY` is used when set; otherwise a free key is minted on the
first call. `initialize` needs neither a key nor the network, and `tools/list`
falls back to the built-in list when the service cannot be reached.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, TextIO

DEFAULT_URL = "https://api.statemind.ai"
TIMEOUT_S = 60
# A client blocks on tools/list before it can do anything, so asking the
# service what it offers must fail fast and hand over to the built-in list.
DISCOVERY_TIMEOUT_S = 5
VERSION = "1.17.1"

# Newest first: a client's requested version wins when we know it, otherwise it
# is told what we do speak and decides.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# One tool per mode: a model picks tools by name, and a name that says what
# happens (execute_python runs the code) is a decision it can get right, where
# mode="execute" is a detail three levels into a schema it can get wrong in
# either direction. The service names a tool <verb>_<language>, so the verb is
# the mode, whether or not this bridge shipped before the tool existed.
VERB_MODES = {"validate": "static", "repair": "repair", "execute": "execute"}
TOOL_MODES = {f"{verb}_python": mode for verb, mode in VERB_MODES.items()}
# The name this bridge answered to before it had one tool per mode. It is still
# accepted, because it sits in the configuration of every client that already
# added the server, and it still reads its mode from the arguments.
LEGACY_NAMES = ("python_code_validator", "python-code-validator")

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

_WHAT = {
    "static": (
        "Validate Python",
        "Check Python source without running it: parse, lint (ruff), type-check (mypy), "
        "AST security policy, credential scan. Safe on code you do not trust. Use it on "
        "every Python file you generated or edited, before writing it to disk. "
        "Alternatives: repair_python to get the corrected source instead of the "
        "diagnosis; execute_python to prove the code runs.",
    ),
    "repair": (
        "Repair Python",
        "Everything validation does, plus deterministic fixes: the corrected source comes "
        "back in fixed_code, and the original is kept whenever the fix cannot be proven "
        "safe. The code is still never run. Use it when validation failed and you want the "
        "fix rather than the diagnosis. Alternatives: validate_python when the diagnosis "
        "is enough; execute_python when the fix has to be proven to run.",
    ),
    "execute": (
        "Execute Python",
        "Everything repair does, and then RUNS the code in a throwaway container — no "
        "network, read-only filesystem, killed at options.timeout_s — reporting exit code, "
        "stdout and stderr. This is a side effect: do not submit code you do not want "
        "executed. Use it only when you need proof that the code runs, or that it prints "
        "the right thing. Alternatives: validate_python for the diagnosis and repair_python "
        "for the fix, neither of which runs anything.",
    ),
}

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "The Python source to check, a whole module rather than a fragment: "
                "diagnostics carry the line and column of the text you send, and a "
                "fragment hides the imports and definitions the type check needs."
            ),
            "minLength": 1,
            "maxLength": 200000,
        },
        "options": {
            "type": "object",
            "description": (
                "Tuning knobs. Each one only acts in the mode that does the "
                "corresponding work, and this tool fixes the mode by its name."
            ),
            "properties": {
                "timeout_s": {
                    "type": "number",
                    "description": (
                        "Wall clock for the run, execute mode only; omitted means the "
                        "service default. The deployment may cap it below 60 and "
                        "refuses a larger value."
                    ),
                    "exclusiveMinimum": 0,
                    "maximum": 60,
                },
                "max_iterations": {
                    "type": "integer",
                    "description": (
                        "Fix/verify rounds the repair loop may take: raise it for a "
                        "file with several independent faults. Ignored in validate."
                    ),
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                },
                "optimize": {
                    "type": "boolean",
                    "description": (
                        "Also fold constants and drop dead code, in repair and "
                        "execute. Off by default: it rewrites the program, and the "
                        "rewrite only comes back when every effectful construct "
                        "provably survives."
                    ),
                    "default": False,
                },
                "expected_output": {
                    "type": "string",
                    "description": (
                        "Exact stdout the program must produce, execute mode only. A "
                        "mismatch is an 'expected-output' diagnostic and makes the "
                        "response invalid even when the program exits cleanly."
                    ),
                },
                "examples": {
                    "type": "string",
                    "description": (
                        "What the code is supposed to do, execute mode only: doctest "
                        "lines ('>>> f(2)' then '4') or plain assertions "
                        "('assert f(2) == 4'). They are run against the code, one that "
                        "does not hold is a 'python:example-mismatch' error, and repair "
                        "searches for a single-token change that makes them all pass. "
                        "This is the only way the service can tell code that runs from "
                        "code that is right, so send it whenever you know what you "
                        "asked for."
                    ),
                },
                "transpile_to": {
                    "type": "string",
                    "description": (
                        "Language for a translated copy in transpiled, e.g. "
                        "'javascript'. Made from the code as it ends up, so in repair "
                        "and execute it translates the repaired source."
                    ),
                },
            },
        },
    },
    "required": ["code"],
}

# What the schema cannot say: which knob does anything in which tool. An agent
# reading only the schema sets timeout_s on a call that never runs anything.
_ARGUMENTS = {
    "static": (
        "Arguments: code is the whole file, UTF-8, empty is refused with 400 and the "
        "deployment's size cap with 413; line and column in the answer count from 1 in "
        "what you sent. Of options only transpile_to acts here; timeout_s, "
        "max_iterations, optimize, examples and expected_output need a pass that rewrites "
        "or runs the code, so send code alone — they are ignored rather than refused. Code that "
        "does not parse is answered, not refused: valid=false with the syntax error "
        "located."
    ),
    "repair": (
        "Arguments: code is the whole file. options.max_iterations (1..10, default 3) "
        "caps the fix/verify rounds and options.optimize (default false) adds the "
        "rewrite; fixed_code is null when nothing could be proven safe to change, which "
        "means 'no fix', not an error. options.timeout_s and options.expected_output do "
        "nothing here, and neither does options.examples: nothing is run, so there is no "
        "clock, no stdout, and no way to check an example."
    ),
    "execute": (
        "Arguments: code is the whole file, and options.timeout_s is the wall clock for "
        "the run (the deployment may cap it below the 60 the schema allows and refuses a "
        "larger value). options.expected_output compares stdout byte for byte, which is "
        "how you ask for 'it did the right thing' rather than 'it ran'; options.examples "
        "is the same question for code with no output, and each one is run against the "
        "code. The program that runs is the repaired one, so read fixed_code before you trust runtime.stdout, "
        "and it runs exactly once however many rounds the repair took."
    ),
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "boolean", "description": "False when anything is an error."},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "diagnostics": {
            "type": "array",
            "description": "Correctness problems, each with rule, message, line and column.",
            "items": {"type": "object"},
        },
        "security": {"type": "array", "items": {"type": "object"}},
        "fixes": {"type": "array", "items": {"type": "string"}},
        "fixed_code": {
            "type": ["string", "null"],
            "description": "Repaired source, only in repair and execute mode.",
        },
        "runtime": {"type": "object", "description": "Sandbox result, only in execute mode."},
        "meta": {"type": "object"},
    },
    "required": ["valid", "score", "diagnostics", "security", "meta"],
}


def _tool(name: str, mode: str) -> dict[str, Any]:
    """Describe one tool the way the service describes it.

    An agent decides from this text alone whether to call the tool, so it says
    what the call does to the world and what it costs: the free tier covers
    static only, and only execute runs the submitted code.
    """
    title, what = _WHAT[mode]
    cost = (
        "A free key covers this call, 100 per day, then HTTP 429; get one with POST /v1/keys."
        if mode == "static"
        else "This call needs a paid key and answers HTTP 402 without one."
    )
    return {
        "name": name,
        "title": title,
        "description": (
            f"{what}\n"
            f"Auth: the bridge sends VALIDATOR_API_KEY, or mints a free key on first use. "
            f"{cost}\n"
            f"{_ARGUMENTS[mode]}\n"
            "Nothing on your machine is read or written: the code you pass is sent to "
            "https://api.statemind.ai and retained there to improve the service.\n"
            "Returns valid, score 0..1, diagnostics (rule, message, line, column), security "
            "findings, fixes, fixed_code and runtime; see outputSchema."
        ),
        "inputSchema": _INPUT_SCHEMA,
        "outputSchema": _OUTPUT_SCHEMA,
        "annotations": {
            "title": title,
            # Only execute runs the submitted code, and then only in the
            # service's own throwaway sandbox.
            "readOnlyHint": mode != "execute",
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    }


BUILTIN_TOOLS = [_tool(name, mode) for name, mode in TOOL_MODES.items()]


def base_url() -> str:
    return os.environ.get("VALIDATOR_URL", DEFAULT_URL).rstrip("/")


def post(
    path: str,
    payload: dict[str, Any] | None,
    key: str | None,
    timeout_s: int = TIMEOUT_S,
) -> dict[str, Any]:
    """POST JSON and return the parsed answer, errors included."""
    request = urllib.request.Request(
        f"{base_url()}{path}",
        data=json.dumps(payload).encode() if payload is not None else b"",
        headers={"content-type": "application/json"}
        | ({"authorization": f"Bearer {key}"} if key else {}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as answer:
            return json.loads(answer.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"detail": body[:500]}
        raise ServiceRefused(exc.code, detail) from None


class ServiceRefused(Exception):
    """The service answered, but refused the call."""

    def __init__(self, status: int, detail: dict[str, Any]) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class Bridge:
    """Translate MCP messages into calls on the hosted validator."""

    def __init__(self) -> None:
        self._key = os.environ.get("VALIDATOR_API_KEY") or None
        self._tools: list[dict[str, Any]] | None = None

    def key(self) -> str | None:
        """The configured key, or a free one asked for on first use."""
        if self._key is None:
            self._key = str(post("/v1/keys", None, None)["api_key"])
        return self._key

    def tools(self) -> list[dict[str, Any]]:
        """What the service offers, asked rather than assumed.

        A bridge that ships its own list goes stale the moment the service
        gains a tool, and the client never learns. So the list comes from the
        deployment this bridge points at — ``tools/list`` needs no key — and
        the built-in one is the answer when it cannot be reached, which is the
        case in the sandboxes that start a server just to read it.
        """
        if self._tools is None:
            self._tools = self._published() or BUILTIN_TOOLS
        return self._tools

    def _published(self) -> list[dict[str, Any]] | None:
        try:
            answer = post(
                "/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                None,
                DISCOVERY_TIMEOUT_S,
            )
        except (ServiceRefused, OSError, ValueError):
            return None
        tools = (answer.get("result") or {}).get("tools")
        if not isinstance(tools, list) or not tools:
            return None
        return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]

    def mode_of(self, name: Any, arguments: dict[str, Any]) -> str | None:
        """The mode a call asks for, or None when it names no tool at all."""
        if name in LEGACY_NAMES:
            mode = arguments.get("mode")
            return mode if isinstance(mode, str) else "static"
        if not isinstance(name, str):
            return None
        # Any tool the deployment publishes, including one this bridge
        # predates, because the verb in the name is the mode it sells.
        known = name in TOOL_MODES or any(tool.get("name") == name for tool in self.tools())
        return VERB_MODES.get(name.split("_")[0]) if known else None

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        call_id = message.get("id")
        if call_id is None:
            return None
        if method == "initialize":
            asked = (message.get("params") or {}).get("protocolVersion")
            return result(
                call_id,
                {
                    "protocolVersion": (
                        asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "python-code-validator", "version": VERSION},
                },
            )
        if method == "tools/list":
            return result(call_id, {"tools": self.tools()})
        if method == "tools/call":
            return self.call(call_id, message.get("params") or {})
        return error(call_id, METHOD_NOT_FOUND, f"unsupported method {method!r}")

    def call(self, call_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(params.get("arguments") or {})
        mode = self.mode_of(params.get("name"), arguments)
        if mode is None:
            return error(call_id, METHOD_NOT_FOUND, f"unknown tool {params.get('name')!r}")
        # The tool the caller picked is the mode.
        arguments["mode"] = mode
        try:
            verdict = post("/v1/validate", arguments, self.key())
        except ServiceRefused as exc:
            return result(call_id, refusal(exc))
        # A verdict of "this code is broken" is a successful call: ``isError``
        # means the tool itself failed, and a client that sees it may discard
        # the verdict or retry instead of showing it.
        return result(
            call_id,
            {
                "content": [{"type": "text", "text": json.dumps(verdict, indent=2)}],
                "structuredContent": verdict,
                "isError": False,
            },
        )


def refusal(exc: ServiceRefused) -> dict[str, Any]:
    """Render a refused call, keeping the way out the service supplied.

    A refusal reaches an agent with no operator to ask, so the ``remedy`` the
    service attaches — mint a key, upgrade it, wait for the quota — is carried
    up beside the text rather than buried in it.
    """
    answer: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(exc.detail, indent=2)}],
        "isError": True,
    }
    remedy = exc.detail.get("remedy")
    if isinstance(remedy, dict):
        answer["remedy"] = remedy
    return answer


def result(call_id: Any, payload: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": call_id, "result": payload}


def error(call_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": call_id, "error": {"code": code, "message": message}}


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Run the loop until stdin closes."""
    bridge = Bridge()
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            answer = bridge.handle(message)
        except Exception as exc:  # keeps the loop alive for the next call
            answer = error(message.get("id"), INTERNAL_ERROR, str(exc))
        if answer is not None:
            sink.write(json.dumps(answer) + "\n")
            sink.flush()


if __name__ == "__main__":
    serve()
