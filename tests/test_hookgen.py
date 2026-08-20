"""Hook installers/generators: correct per-surface schema + event names,
idempotent merge, preservation of unrelated hooks, one-time backup, dry-run,
and executable-safe (Windows-usable) generated commands."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_paths  # noqa: F401

from exi import hookgen


class CommandStringTest(unittest.TestCase):
    def test_uses_console_script_no_posix_quoting(self):
        cmd = hookgen.command_string("claude", "UserPromptSubmit")
        self.assertEqual(cmd, "exi-hook claude UserPromptSubmit")
        self.assertNotIn("'", cmd)  # no POSIX single-quote quoting

    def test_windows_command_is_executable_safe(self):
        # The VS Code generator emits an explicit `windows` command; it must be a
        # plain invocable token form, not a /bin/sh or single-quoted POSIX string.
        doc = hookgen.merge_vscode({})
        win = doc["hooks"]["Stop"][0]["windows"]
        self.assertIn("exi-hook", win)
        self.assertNotIn("/bin/sh", win)
        self.assertNotIn("'", win)

    def test_exe_with_spaces_double_quoted(self):
        cmd = hookgen.command_string("claude", "Stop", exe=r"C:\Program Files\exi\exi-hook.exe")
        self.assertTrue(cmd.startswith('"C:\\Program Files\\exi\\exi-hook.exe"'))

    def test_exe_rejects_quotes_and_newlines(self):
        for exe in ('bad"name', "bad\nname", ""):
            with self.subTest(exe=exe), self.assertRaises(ValueError):
                hookgen.command_string("claude", "Stop", exe=exe)


class ClaudeGeneratorTest(unittest.TestCase):
    def test_events_and_shape(self):
        doc = hookgen.merge_claude({})
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            group = doc["hooks"][event][0]
            self.assertEqual(group["matcher"], "")
            self.assertEqual(group["hooks"][0]["type"], "command")
            self.assertIn(f"claude {event}", group["hooks"][0]["command"])

    def test_preserves_unrelated_and_idempotent(self):
        existing = {"hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "other-tool stop"}]}]}}
        once = hookgen.merge_claude(existing)
        twice = hookgen.merge_claude(once)
        self.assertEqual(once, twice)  # idempotent
        stop_cmds = [h["command"] for h in once["hooks"]["Stop"][0]["hooks"]]
        self.assertIn("other-tool stop", stop_cmds)   # unrelated preserved
        self.assertEqual(sum(1 for c in stop_cmds if "claude Stop" in c), 1)  # no dup


class VscodeGeneratorTest(unittest.TestCase):
    def test_direct_array_pascalcase(self):
        doc = hookgen.merge_vscode({})
        self.assertEqual(doc["version"], 1)
        self.assertEqual(set(doc["hooks"]),
                         {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"})
        self.assertEqual(doc["hooks"]["Stop"][0]["type"], "command")
        self.assertIn("copilot-vscode Stop", doc["hooks"]["Stop"][0]["command"])

    def test_idempotent_and_preserves(self):
        existing = {"version": 1, "hooks": {"Stop": [
            {"type": "command", "command": "someone-else stop"}]}}
        once = hookgen.merge_vscode(existing)
        twice = hookgen.merge_vscode(once)
        self.assertEqual(once, twice)
        cmds = [e["command"] for e in once["hooks"]["Stop"]]
        self.assertIn("someone-else stop", cmds)

    def test_rejects_unsupported_document_version(self):
        with self.assertRaises(ValueError):
            hookgen.merge_vscode({"version": 2, "hooks": {}})


class CliGeneratorTest(unittest.TestCase):
    def test_camelcase_events(self):
        doc = hookgen.merge_cli({})
        self.assertEqual(doc["version"], 1)
        self.assertEqual(set(doc["hooks"]),
                         {"userPromptTransformed", "preToolUse", "postToolUse", "agentStop"})
        self.assertIn("copilot-cli userPromptTransformed",
                      doc["hooks"]["userPromptTransformed"][0]["command"])

    def test_no_userpromptsubmitted_event(self):
        # Injection must ride userPromptTransformed, never userPromptSubmitted
        # (whose output the CLI drops).
        self.assertNotIn("userPromptSubmitted", hookgen.merge_cli({})["hooks"])


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        target = Path(self.tmp.name) / "settings.json"
        res = hookgen.install("claude", path=target, dry_run=True)
        self.assertFalse(target.exists())
        self.assertTrue(res["dry_run"])
        self.assertIn("hooks", res["result"])

    def test_install_backs_up_once(self):
        target = Path(self.tmp.name) / "settings.json"
        target.write_text('{"hooks": {"Stop": [{"matcher": "", "hooks": [' \
                          '{"type": "command", "command": "orig"}]}]}}', encoding="utf-8")
        original = target.read_bytes()
        hookgen.install("claude", path=target)
        backup = target.with_suffix(target.suffix + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), original)  # backup == pre-install bytes
        # Second install must NOT refresh the backup.
        hookgen.install("claude", path=target)
        self.assertEqual(backup.read_bytes(), original)
        # Installed file has our hooks + the preserved original.
        merged = json.loads(target.read_text(encoding="utf-8"))
        cmds = [h["command"] for h in merged["hooks"]["Stop"][0]["hooks"]]
        self.assertIn("orig", cmds)
        self.assertTrue(any("claude Stop" in c for c in cmds))

    def test_default_paths(self):
        with mock.patch.dict(os.environ,
                             {"USERPROFILE": "/home/u", "HOME": "/home/u"}, clear=False):
            cli = hookgen.default_path("copilot-cli", scope="user")
            self.assertTrue(str(cli).endswith(os.path.join(".copilot", "hooks", "exi-feedback-cli.json")))
            vscode = hookgen.default_path("copilot-vscode", scope="user")
            self.assertTrue(str(vscode).endswith(os.path.join(
                ".copilot", "hooks", "exi-feedback-vscode.json")))
        proj = hookgen.default_path("copilot-vscode", scope="project", project="/proj")
        self.assertEqual(proj, Path("/proj") / ".github" / "hooks" / "exi-feedback-vscode.json")

    def test_refuses_to_overwrite_malformed_json(self):
        target = Path(self.tmp.name) / "broken.json"
        target.write_text("{broken", encoding="utf-8")
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            hookgen.install("copilot-vscode", path=target)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(target.with_suffix(".json.bak").exists())


if __name__ == "__main__":
    unittest.main()
