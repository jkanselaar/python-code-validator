"""What the bridge answers, without a network to answer from.

Every call the service would take is stubbed: these tests pin the translation
between an MCP message and a `/v1/validate` call, which is the whole job of the
bridge, and the fallback that keeps a client working when the service cannot be
reached.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_stdio  # noqa: E402


class BridgeCase(unittest.TestCase):
    """A bridge whose service is a dictionary."""

    def setUp(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.published: list[dict[str, Any]] | None = None
        self.refuse: mcp_stdio.ServiceRefused | None = None
        self.original = mcp_stdio.post
        mcp_stdio.post = self.post  # type: ignore[assignment]
        self.addCleanup(setattr, mcp_stdio, "post", self.original)
        self.bridge = mcp_stdio.Bridge()
        self.bridge._key = "test-key"

    def post(
        self,
        path: str,
        payload: dict[str, Any] | None,
        key: str | None,
        timeout_s: int = mcp_stdio.TIMEOUT_S,
    ) -> dict[str, Any]:
        self.calls.append((path, payload))
        if path == "/mcp":
            if self.published is None:
                raise OSError("no service here")
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": self.published}}
        if self.refuse is not None:
            raise self.refuse
        return {"valid": True, "score": 1.0, "diagnostics": [], "security": [], "meta": {}}

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        answer = self.bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"code": "x = 1\n", **arguments}},
            }
        )
        assert answer is not None
        return answer


class CallTest(BridgeCase):
    def test_the_tool_the_caller_picked_is_the_mode(self) -> None:
        for name, mode in mcp_stdio.TOOL_MODES.items():
            with self.subTest(tool=name):
                self.calls.clear()
                self.call(name)
                self.assertEqual(
                    self.calls[-1], ("/v1/validate", {"code": "x = 1\n", "mode": mode})
                )

    def test_the_old_single_tool_name_still_reads_its_mode(self) -> None:
        """It sits in the configuration of every client that already added the server."""
        self.call("python_code_validator", mode="repair")
        self.assertEqual(self.calls[-1][1]["mode"], "repair")

        self.call("python_code_validator")
        self.assertEqual(self.calls[-1][1]["mode"], "static")

    def test_a_tool_nobody_offers_is_not_a_call(self) -> None:
        answer = self.call("delete_python")
        self.assertEqual(answer["error"]["code"], mcp_stdio.METHOD_NOT_FOUND)
        self.assertEqual(
            self.calls, [("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})]
        )

    def test_a_negative_verdict_is_still_a_successful_call(self) -> None:
        answer = self.call("validate_python")
        self.assertFalse(answer["result"]["isError"])
        self.assertTrue(answer["result"]["structuredContent"]["valid"])

    def test_a_refusal_keeps_the_way_out(self) -> None:
        """An agent has no operator to ask, so the remedy travels with the refusal."""
        remedy = {"action": "upgrade_key", "hint": "A free key covers static only."}
        self.refuse = mcp_stdio.ServiceRefused(402, {"error": "payment_required", "remedy": remedy})

        result = self.call("repair_python")["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["remedy"], remedy)

    def test_a_refusal_no_key_can_fix_promises_nothing(self) -> None:
        self.refuse = mcp_stdio.ServiceRefused(409, {"error": "unsupported_mode"})

        result = self.call("execute_python")["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn("remedy", result)


class ToolListTest(BridgeCase):
    def list_tools(self) -> list[dict[str, Any]]:
        answer = self.bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert answer is not None
        return list(answer["result"]["tools"])

    def test_the_service_says_what_it_offers(self) -> None:
        """A list this bridge shipped with goes stale; one it asks for does not."""
        self.published = [{"name": "validate_python"}, {"name": "transpile_python"}]
        self.assertEqual(
            [tool["name"] for tool in self.list_tools()],
            ["validate_python", "transpile_python"],
        )

    def test_a_tool_this_bridge_predates_is_callable(self) -> None:
        """The verb in the name is the mode, so a new tool needs no new bridge."""
        self.published = [{"name": "repair_python"}, {"name": "execute_javascript"}]
        self.call("execute_javascript")
        self.assertEqual(self.calls[-1][1]["mode"], "execute")

    def test_an_unreachable_service_still_describes_its_tools(self) -> None:
        """Sandboxes introspect a server by starting it, with no network at all."""
        self.published = None
        tools = self.list_tools()

        self.assertEqual([tool["name"] for tool in tools], list(mcp_stdio.TOOL_MODES))
        execute = next(tool for tool in tools if tool["name"] == "execute_python")
        validate = next(tool for tool in tools if tool["name"] == "validate_python")
        self.assertFalse(execute["annotations"]["readOnlyHint"])
        self.assertTrue(validate["annotations"]["readOnlyHint"])
        # The tool name already fixed the mode, so it is not an argument.
        self.assertNotIn("mode", validate["inputSchema"]["properties"])

    def test_the_offline_description_says_which_options_act(self) -> None:
        """The schema lists every knob for every tool; only some of them act."""
        self.published = None
        tools = {tool["name"]: tool for tool in self.list_tools()}

        validate = tools["validate_python"]["description"]
        self.assertIn("timeout_s, max_iterations, optimize and expected_output", validate)
        repair = tools["repair_python"]["description"]
        self.assertIn("max_iterations (1..10, default 3)", repair)
        self.assertIn("do nothing here", repair)
        execute = tools["execute_python"]["description"]
        self.assertIn("expected_output compares stdout byte for byte", execute)
        options = tools["execute_python"]["inputSchema"]["properties"]["options"]
        self.assertEqual(options["properties"]["max_iterations"]["default"], 3)

    def test_the_offline_description_names_the_tool_to_use_instead(self) -> None:
        """Three tools that all take Python look interchangeable without this."""
        self.published = None
        tools = {tool["name"]: tool for tool in self.list_tools()}

        self.assertIn(
            "Alternatives: repair_python to get the corrected source",
            tools["validate_python"]["description"],
        )
        self.assertIn(
            "execute_python when the fix has to be proven to run",
            tools["repair_python"]["description"],
        )
        self.assertIn("validate_python for the diagnosis", tools["execute_python"]["description"])

    def test_the_service_is_asked_once(self) -> None:
        self.published = [{"name": "validate_python"}]
        self.list_tools()
        self.list_tools()
        self.assertEqual([path for path, _ in self.calls], ["/mcp"])


class HandshakeTest(BridgeCase):
    def initialize(self, version: str) -> dict[str, Any]:
        answer = self.bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": version},
            }
        )
        assert answer is not None
        return dict(answer["result"])

    def test_a_version_we_speak_is_agreed_to(self) -> None:
        self.assertEqual(self.initialize("2024-11-05")["protocolVersion"], "2024-11-05")

    def test_a_version_we_do_not_speak_is_answered_with_ours(self) -> None:
        self.assertEqual(
            self.initialize("1999-01-01")["protocolVersion"], mcp_stdio.PROTOCOL_VERSIONS[0]
        )

    def test_the_handshake_needs_no_network(self) -> None:
        self.initialize("2025-06-18")
        self.assertEqual(self.calls, [])


class LoopTest(unittest.TestCase):
    def test_a_notification_is_not_answered(self) -> None:
        """A message without an id expects no reply, and a reply confuses the client."""
        sink = io.StringIO()
        mcp_stdio.serve(
            io.StringIO('{"jsonrpc":"2.0","method":"notifications/initialized"}\n\n'),
            sink,
        )
        self.assertEqual(sink.getvalue(), "")

    def test_a_broken_message_does_not_end_the_session(self) -> None:
        sink = io.StringIO()
        mcp_stdio.serve(
            io.StringIO('not json\n{"jsonrpc":"2.0","id":1,"method":"nope"}\n'),
            sink,
        )
        self.assertEqual(json.loads(sink.getvalue())["error"]["code"], mcp_stdio.METHOD_NOT_FOUND)


class GeminiExtensionTest(unittest.TestCase):
    """The manifest Gemini CLI installs, which nothing else exercises."""

    def setUp(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.manifest = json.loads((root / "gemini-extension.json").read_text())
        self.root = root

    def test_it_launches_a_file_that_exists(self) -> None:
        server = self.manifest["mcpServers"]["python-code-validator"]
        launched = server["args"][0].replace("${extensionPath}/", "")
        self.assertTrue((self.root / launched).is_file(), launched)

    def test_the_key_is_declared_so_the_cli_passes_it_through(self) -> None:
        """Gemini CLI drops every environment variable a manifest does not ask for."""
        declared = [setting["envVar"] for setting in self.manifest["settings"]]
        self.assertIn("VALIDATOR_API_KEY", declared)

    def test_the_version_is_the_bridge_it_ships(self) -> None:
        self.assertEqual(self.manifest["version"], mcp_stdio.VERSION)


if __name__ == "__main__":
    unittest.main()
