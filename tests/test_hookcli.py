"""The `exi-hook` unified entry point: runtime routing per provider + the
install verb (dry-run) — exercised the way generated hook commands invoke it."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import conftest_paths  # noqa: F401

from exi import feedback as fb
from exi import hookcli


class HookCliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EXI_DATA_DIR"] = self.tmp.name
        os.environ.pop("EXI_CONFIG", None)

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        self.tmp.cleanup()

    def run_cli(self, argv, payload=None):
        stdin = io.StringIO(json.dumps(payload) if payload is not None else "")
        out, err = io.StringIO(), io.StringIO()
        orig = sys.stdin
        sys.stdin = stdin
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = hookcli.main(argv)
        finally:
            sys.stdin = orig
        return rc, out.getvalue(), err.getvalue()


class RuntimeRoutingTest(HookCliBase):
    def test_codex_routing_deny(self):
        store = fb.FeedbackStore(data_dir=self.tmp.name)
        for i in range(5):
            store.record("d", "never rm -rf", evidence=f"e{i}")
        store.configure("d", {"event": "pre_bash", "when": "rm -rf"})
        rc, out, _ = self.run_cli(
            ["codex", "PreToolUse"],
            {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "rm -rf /x"}})
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        self.assertEqual(obj["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_claude_routing_context(self):
        store = fb.FeedbackStore(data_dir=self.tmp.name)
        for i in range(4):
            store.record("r", "a high rule", evidence=f"e{i}")
        store.configure("r", {"event": "pre_bash", "when": "x"})
        rc, out, _ = self.run_cli(["claude", "UserPromptSubmit"],
                                  {"session_id": "s1", "prompt": "hi"})
        self.assertEqual(rc, 0)
        self.assertIn("a high rule",
                      json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_copilot_cli_routing_empty_object(self):
        rc, out, _ = self.run_cli(["copilot-cli", "userPromptTransformed"],
                                  {"sessionId": "s1", "prompt": "normal", "transformedPrompt": "TP"})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {})

    def test_unknown_provider_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_cli(["nope", "Stop"], {})


class InstallVerbTest(HookCliBase):
    def test_install_dry_run_writes_nothing(self):
        target = Path(self.tmp.name) / "settings.json"
        rc, out, _ = self.run_cli(
            ["install", "claude", "--path", str(target), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertFalse(target.exists())

    def test_install_writes_and_is_idempotent(self):
        target = Path(self.tmp.name) / "hooks.json"
        self.run_cli(["install", "copilot-cli", "--path", str(target)])
        self.assertTrue(target.exists())
        first = target.read_text(encoding="utf-8")
        self.run_cli(["install", "copilot-cli", "--path", str(target)])
        self.assertEqual(target.read_text(encoding="utf-8"), first)  # idempotent
        doc = json.loads(first)
        self.assertEqual(doc["version"], 1)
        self.assertIn("userPromptTransformed", doc["hooks"])

    def test_install_reports_malformed_json_without_traceback(self):
        target = Path(self.tmp.name) / "broken.json"
        target.write_text("{broken", encoding="utf-8")
        rc, out, err = self.run_cli(
            ["install", "copilot-vscode", "--path", str(target)])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("refusing to overwrite malformed hook JSON", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
