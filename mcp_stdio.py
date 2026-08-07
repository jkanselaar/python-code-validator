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
first call. `initialize` and `tools/list` need neither a key nor the network.
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

# Newest first: a client's requested version wins when we know it, otherwise it
# is told what we do speak and decides.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
TOOL_NAME = "python_code_validator"

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Validate Python: syntax and lint diagnostics, a security policy over the AST, "
        "a credential scan and deterministic repair. Returns a verdict with a score."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python source to validate.",
                "maxLength": 200000,
            },
            "mode": {
                "type": "string",
                "enum": ["static", "repair", "execute"],
                "default": "static",
                "description": (
                    "static analyses only; repair also returns fixed code; "
                    "execute runs it in a sandbox. repair and execute need a paid key."
                ),
            },
            "filename": {
                "type": "string",
                "description": "Name to report diagnostics against.",
            },
        },
        "required": ["code"],
    },
}


def base_url() -> str:
    return os.environ.get("VALIDATOR_URL", DEFAULT_URL).rstrip("/")


def post(path: str, payload: dict[str, Any] | None, key: str | None) -> dict[str, Any]:
    """POST JSON and return the parsed answer, errors included."""
    request = urllib.request.Request(
        f"{base_url()}{path}",
        data=json.dumps(payload).encode() if payload is not None else b"",
        headers={"content-type": "application/json"}
        | ({"authorization": f"Bearer {key}"} if key else {}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as answer:
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

    def key(self) -> str | None:
        """The configured key, or a free one asked for on first use."""
        if self._key is None:
            self._key = str(post("/v1/keys", None, None)["api_key"])
        return self._key

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
                    "serverInfo": {"name": "python-code-validator", "version": "1.1.0"},
                },
            )
        if method == "tools/list":
            return result(call_id, {"tools": [TOOL]})
        if method == "tools/call":
            return self.call(call_id, message.get("params") or {})
        return error(call_id, METHOD_NOT_FOUND, f"unsupported method {method!r}")

    def call(self, call_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("name") not in {TOOL_NAME, "python-code-validator"}:
            return error(call_id, METHOD_NOT_FOUND, f"unknown tool {params.get('name')!r}")
        arguments = params.get("arguments") or {}
        try:
            verdict = post("/v1/validate", arguments, self.key())
        except ServiceRefused as exc:
            return result(
                call_id,
                {
                    "content": [{"type": "text", "text": json.dumps(exc.detail, indent=2)}],
                    "isError": True,
                },
            )
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
