import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import conftest_paths  # noqa: F401

from exi import hookmerge

EXISTING_STOP = {
    "hooks": {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": "'/opt/other-tool/notify.sh' 'codex' '/dev'"}
                ],
            }
        ]
    }
}


EXISTING_STOP_CMD = "'/opt/other-tool/notify.sh' 'codex' '/dev'"


def _cmds(merged, event):
    return [h["command"] for g in merged["hooks"].get(event, []) for h in g["hooks"]]


class HookMergeTest(unittest.TestCase):
    def test_preserves_existing_stop(self):
        merged = hookmerge.merge_hooks(EXISTING_STOP, bin_path="/x/codex-guard")
        # the pre-existing (another tool's) Stop hook is carried over unchanged...
        self.assertIn(EXISTING_STOP_CMD, _cmds(merged, "Stop"))
        # ...alongside exactly one feedback Stop hook.
        self.assertEqual(
            sum(c.endswith("feedback-hook Stop") for c in _cmds(merged, "Stop")), 1
        )
        for ev in hookmerge.GUARD_EVENTS:
            self.assertTrue(any(c.endswith(f"hook {ev}") for c in _cmds(merged, ev)))
        for ev in hookmerge.FEEDBACK_EVENTS:
            self.assertEqual(
                sum(c.endswith(f"feedback-hook {ev}") for c in _cmds(merged, ev)), 1
            )

    def test_idempotent(self):
        once = hookmerge.merge_hooks(EXISTING_STOP, bin_path="/x/codex-guard")
        twice = hookmerge.merge_hooks(once, bin_path="/x/codex-guard")
        for ev, groups in twice["hooks"].items():
            cmds = [h["command"] for g in groups for h in g["hooks"]]
            self.assertEqual(len(cmds), len(set(cmds)), f"{ev} has duplicate commands")
        # both the guard and feedback commands survive a re-merge, exactly once
        self.assertEqual(
            sum(c.endswith("hook PreToolUse") and "feedback" not in c for c in _cmds(twice, "PreToolUse")), 1
        )
        self.assertEqual(
            sum(c.endswith("feedback-hook PreToolUse") for c in _cmds(twice, "PreToolUse")), 1
        )
        self.assertIn(EXISTING_STOP_CMD, _cmds(twice, "Stop"))

    def test_feedback_and_guard_share_event_without_shadowing(self):
        # UserPromptSubmit gets BOTH a guard hook and a feedback hook.
        merged = hookmerge.merge_hooks({}, bin_path="/x/codex-guard")
        ups = _cmds(merged, "UserPromptSubmit")
        self.assertEqual(sum(c.endswith("hook UserPromptSubmit") and "feedback" not in c for c in ups), 1)
        self.assertEqual(sum(c.endswith("feedback-hook UserPromptSubmit") for c in ups), 1)

    def test_empty_start(self):
        merged = hookmerge.merge_hooks({}, bin_path="/x/codex-guard")
        self.assertIn("PreToolUse", merged["hooks"])
        self.assertIn("PostToolUse", merged["hooks"])
        self.assertIn("Stop", merged["hooks"])

    def test_install_writes_backup_and_preserves(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hooks.json"
            original = json.dumps(EXISTING_STOP, separators=(",", ":"))
            path.write_text(original, encoding="utf-8")
            os.chmod(path, 0o600)
            res = hookmerge.install(path=path, dry_run=False)
            self.assertFalse(res["dry_run"])
            written = json.loads(path.read_text())
            stop_cmds = [h["command"] for g in written["hooks"]["Stop"] for h in g["hooks"]]
            self.assertIn(EXISTING_STOP_CMD, stop_cmds)  # pre-existing Stop hook preserved
            self.assertTrue(any(c.endswith("feedback-hook Stop") for c in stop_cmds))
            self.assertIn("PreToolUse", written["hooks"])
            backup_path = path.with_suffix(".json.bak")
            self.assertEqual(backup_path.read_text(), original)
            self.assertEqual(json.loads(backup_path.read_text()), EXISTING_STOP)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)

    def test_dry_run_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hooks.json"
            path.write_text(json.dumps(EXISTING_STOP), encoding="utf-8")
            res = hookmerge.install(path=path, dry_run=True)
            self.assertTrue(res["dry_run"])
            # file unchanged
            self.assertEqual(json.loads(path.read_text()), EXISTING_STOP)
            self.assertFalse(path.with_suffix(".json.bak").exists())

    def test_bin_path_is_shell_quoted(self):
        # A path containing a space and a single quote must round-trip through
        # a shell tokenizer as a single argument, not corrupt the command.
        tricky = "/opt/weird dir/codex's-guard"
        merged = hookmerge.merge_hooks({}, bin_path=tricky)
        command = merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(command)
        self.assertEqual(tokens[0], tricky)
        self.assertEqual(tokens[1:], ["hook", "PreToolUse"])

    def test_existing_backup_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hooks.json"
            path.write_text(json.dumps(EXISTING_STOP), encoding="utf-8")
            backup = path.with_suffix(".json.bak")
            sentinel = {"sentinel": True}
            backup.write_text(json.dumps(sentinel), encoding="utf-8")
            hookmerge.install(path=path, dry_run=False)
            self.assertEqual(json.loads(backup.read_text()), sentinel)

    def test_install_atomic_write_no_leftover_temp(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hooks.json"
            path.write_text(json.dumps(EXISTING_STOP), encoding="utf-8")
            hookmerge.install(path=path, dry_run=False)
            leftovers = [p.name for p in Path(d).iterdir() if ".tmp-" in p.name]
            self.assertEqual(leftovers, [])


class DefaultHookCommandTest(unittest.TestCase):
    """Default (no bin_path override) hook command resolution.

    A normal (non-editable) wheel install has no `bin/codex-guard` inside
    site-packages, so the default command must fall back to the active
    interpreter + `-m exi.guardcli` rather than a nonexistent path.
    """

    def test_falls_back_to_python_module_without_bin_script(self):
        with tempfile.TemporaryDirectory() as d:
            # A directory with neither bin/codex-guard nor anything else —
            # simulates config.ROOT pointing inside site-packages.
            with patch.object(hookmerge.config, "ROOT", Path(d)):
                merged = hookmerge.merge_hooks({})
        command = merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(command)
        self.assertEqual(tokens[:3], [sys.executable, "-m", "exi.guardcli"])
        self.assertEqual(tokens[3:], ["hook", "PreToolUse"])

    def test_prefers_bin_script_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            bin_dir = Path(d) / "bin"
            bin_dir.mkdir()
            script = bin_dir / "codex-guard"
            script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with patch.object(hookmerge.config, "ROOT", Path(d)):
                merged = hookmerge.merge_hooks({})
        command = merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(command)
        self.assertEqual(tokens[0], str(script))
        self.assertEqual(tokens[1:], ["hook", "PreToolUse"])

    def test_explicit_bin_path_override_still_wins(self):
        with tempfile.TemporaryDirectory() as d:
            bin_dir = Path(d) / "bin"
            bin_dir.mkdir()
            (bin_dir / "codex-guard").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            with patch.object(hookmerge.config, "ROOT", Path(d)):
                merged = hookmerge.merge_hooks({}, bin_path="/custom/codex-guard")
        command = merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(command)
        self.assertEqual(tokens[0], "/custom/codex-guard")
        self.assertEqual(tokens[1:], ["hook", "PreToolUse"])


if __name__ == "__main__":
    unittest.main()
