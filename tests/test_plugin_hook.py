"""What the Claude Code hook does with an answer, and with no answer at all.

The service is stubbed everywhere: what these tests pin is the contract with
Claude Code — exit 2 only when the file was rejected, exit 0 on everything the
hook cannot help with, and never a call for a file that was already checked.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "validate_written", ROOT / "plugin" / "hooks" / "validate_written.py"
)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


class HookCase(unittest.TestCase):
    """A hook whose service and caches live in a temporary directory."""

    def setUp(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answer: tuple[int, dict] = (200, {"valid": True, "score": 1.0})
        self.original_post = hook.post
        self.original_cache = hook.cache_dir
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        hook.cache_dir = lambda: self.root / "cache"  # type: ignore[assignment]
        hook.post = self.fake_post  # type: ignore[assignment]
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        # A key in the environment of whoever runs the tests would hide the
        # minting the hook is supposed to do by itself.
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop("VALIDATOR_API_KEY", None)

    def tearDown(self) -> None:
        self.environment.stop()
        hook.post = self.original_post  # type: ignore[assignment]
        hook.cache_dir = self.original_cache  # type: ignore[assignment]
        self.temp.cleanup()

    def fake_post(self, path: str, payload: dict, key: str = "") -> tuple[int, dict]:
        """Stand in for the service, recording what it was asked."""
        self.calls.append((path, payload))
        if path == "/v1/keys":
            return 200, {"api_key": "msvc_free_test"}
        return self.answer

    def written(self, source: str, name: str = "written.py") -> Path:
        """A file on disk, as the edited file the event points at."""
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return path

    def run_hook(
        self,
        path: Path,
        tool: str = "Write",
        argv: list[str] | None = None,
        event: dict | None = None,
    ) -> tuple[int, str, str]:
        """Feed one event through the hook; return its code and its output."""
        event = event or {"tool_name": tool, "tool_input": {"file_path": str(path)}}
        original = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = (
            io.StringIO(json.dumps(event)),
            self.stdout,
            self.stderr,
        )
        try:
            code = hook.main(argv or [])
        finally:
            sys.stdin, sys.stdout, sys.stderr = original
        return code, self.stdout.getvalue(), self.stderr.getvalue()

    def test_an_accepted_file_says_nothing(self) -> None:
        code, out, err = self.run_hook(self.written("x = 1\n"))
        self.assertEqual(code, 0)
        self.assertEqual((out, err), ("", ""))

    def test_a_rejected_file_reaches_the_model(self) -> None:
        self.answer = (
            200,
            {
                "valid": False,
                "diagnostics": [
                    {
                        "severity": "error",
                        "rule": "SyntaxError",
                        "message": "invalid syntax",
                        "line": 2,
                    }
                ],
                "security": [
                    {"id": "PY-OS-SYSTEM", "message": "os.system() runs a shell", "line": 3}
                ],
            },
        )
        code, _, err = self.run_hook(self.written("def f(a b):\n    pass\n"))
        self.assertEqual(code, 2)
        self.assertIn("SyntaxError", err)
        self.assertIn("PY-OS-SYSTEM", err)
        self.assertIn("Fix the file now", err)

    def test_a_repair_is_pointed_at_before_a_rewrite(self) -> None:
        self.answer = (200, {"valid": False, "fixed_code": "x = 1\n"})
        _, _, err = self.run_hook(self.written("x = 1\n"))
        self.assertIn("fixed_code", err)

    def test_a_spent_allowance_goes_to_the_user(self) -> None:
        self.answer = (429, {"remedy": {"hint": "buy credits", "url": "https://api.statemind.ai"}})
        code, out, err = self.run_hook(self.written("x = 1\n"))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("buy credits", json.loads(out)["systemMessage"])

    def test_an_unreachable_service_lets_the_turn_continue(self) -> None:
        self.answer = (0, {})
        code, out, err = self.run_hook(self.written("def f(a b):\n"))
        self.assertEqual((code, out, err), (0, "", ""))

    def test_only_python_is_asked_about(self) -> None:
        code, _, _ = self.run_hook(self.written("# notes\n", name="notes.md"))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])

    def test_the_same_source_is_not_asked_about_twice(self) -> None:
        path = self.written("x = 1\n")
        self.run_hook(path)
        self.run_hook(path, tool="Edit")
        self.assertEqual([call for call, _ in self.calls].count("/v1/validate"), 1)

    def test_a_missing_file_is_not_an_error(self) -> None:
        code, _, _ = self.run_hook(self.root / "gone.py")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])

    def test_the_key_is_kept_between_calls(self) -> None:
        self.run_hook(self.written("x = 1\n"))
        self.run_hook(self.written("y = 2\n", name="other.py"))
        self.assertEqual([call for call, _ in self.calls].count("/v1/keys"), 1)

    def test_cursor_gets_the_verdict_as_context(self) -> None:
        self.answer = (200, {"valid": False, "diagnostics": []})
        code, out, err = self.run_hook(self.written("x = 1\n"), argv=["--cursor"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("rejected this file", json.loads(out)["additional_context"])

    def test_a_path_named_the_way_cursor_names_it_is_read(self) -> None:
        path = self.written("x = 1\n")
        code, _, _ = self.run_hook(path, event={"file_path": str(path)})
        self.assertEqual(code, 0)
        self.assertIn("/v1/validate", [call for call, _ in self.calls])

    def test_a_file_too_large_to_send_is_skipped(self) -> None:
        code, _, _ = self.run_hook(self.written("x = 1\n" * 40_000))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])


class ManifestCase(unittest.TestCase):
    """The files a Claude Code marketplace reads before installing anything."""

    def read(self, *parts: str) -> Any:
        return json.loads((ROOT.joinpath(*parts)).read_text(encoding="utf-8"))

    def test_the_marketplace_lists_the_plugin_where_it_lives(self) -> None:
        listed = self.read(".claude-plugin", "marketplace.json")["plugins"]
        self.assertEqual([plugin["name"] for plugin in listed], ["python-code-validator"])
        self.assertTrue((ROOT / listed[0]["source"]).is_dir())

    def test_the_cursor_hook_runs_the_same_script_from_the_project_root(self) -> None:
        configured = self.read("cursor", "hooks.json")["hooks"]["postToolUse"][0]["command"]
        self.assertIn("--cursor", configured)
        self.assertIn(".cursor/hooks/validate_written.py", configured)

    def test_the_plugin_declares_the_hook_it_ships(self) -> None:
        manifest = self.read("plugin", ".claude-plugin", "plugin.json")
        hooks = self.read("plugin", "hooks", "hooks.json")["hooks"]
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        self.assertIn("Write", hooks["PostToolUse"][0]["matcher"])
        self.assertTrue((ROOT / "plugin" / "skills" / "validate-python" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
