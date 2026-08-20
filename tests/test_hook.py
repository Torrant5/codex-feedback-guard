import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

import conftest_paths  # noqa: F401

from exi import hook, guard
from exi.quota import QuotaResult


def q_known(used):
    return QuotaResult(weekly_used=used, resets_at=None, mode="normal", ok=True, reason="")


def q_unknown(reason="weekly window unavailable"):
    return QuotaResult(weekly_used=None, resets_at=None, mode=None, ok=False, reason=reason)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EXI_DATA_DIR"] = self.tmp.name
        self._orig_quota = hook.read_codex_quota

    def tearDown(self):
        hook.read_codex_quota = self._orig_quota
        os.environ.pop("EXI_DATA_DIR", None)
        self.tmp.cleanup()

    def _run(self, event, payload=None):
        hook_in = io.StringIO(json.dumps(payload or {}))
        out, err = io.StringIO(), io.StringIO()
        import sys
        orig = sys.stdin
        sys.stdin = hook_in
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = hook.handle(event)
        finally:
            sys.stdin = orig
        return rc, out.getvalue(), err.getvalue()

    def _is_deny(self, stdout):
        stdout = stdout.strip()
        if not stdout:
            return False
        obj = json.loads(stdout)
        return obj.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def _is_stop(self, stdout):
        stdout = stdout.strip()
        if not stdout:
            return False
        obj = json.loads(stdout)
        return obj.get("continue") is False and "stopReason" in obj and "systemMessage" in obj

    def test_normal_allows(self):
        hook.read_codex_quota = lambda cfg: q_known(30.0)
        self._run("UserPromptSubmit", {"prompt": "hi"})
        rc, out, err = self._run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(rc, 0)
        self.assertFalse(self._is_deny(out))

    def test_repeat_fingerprint_denies(self):
        hook.read_codex_quota = lambda cfg: q_known(30.0)
        self._run("UserPromptSubmit", {"prompt": "go"})
        payload = {"tool_name": "Bash", "tool_input": {"command": "same"}}
        self._run("PreToolUse", payload)
        _, out2, _ = self._run("PreToolUse", payload)
        self.assertFalse(self._is_deny(out2))  # 2nd repeat: still allowed (max=3)
        _, out3, _ = self._run("PreToolUse", payload)
        self.assertTrue(self._is_deny(out3))   # 3rd identical call: deny

    def test_time_hard_denies(self):
        hook.read_codex_quota = lambda cfg: q_known(30.0)
        self._run("UserPromptSubmit", {"prompt": "go"})
        # rewind the turn start ~3h into the past
        st = guard.load_state()
        key = guard.turn_key({})
        st["turns"][key]["started_at"] -= 3 * 3600
        guard.save_state(st)
        _, out, _ = self._run("PreToolUse", {"tool_name": "Read", "tool_input": {"p": "x"}})
        self.assertTrue(self._is_deny(out))

    def test_quota_unknown_does_not_deny_but_time_still_guards(self):
        hook.read_codex_quota = lambda cfg: q_unknown()
        self._run("UserPromptSubmit", {"prompt": "go"})
        _, out, _ = self._run("PreToolUse", {"tool_name": "Read", "tool_input": {"p": "1"}})
        self.assertFalse(self._is_deny(out))  # unknown quota alone must not block
        # but a stale/old turn still trips the time guard
        st = guard.load_state()
        key = guard.turn_key({})
        st["turns"][key]["started_at"] -= 3 * 3600
        guard.save_state(st)
        _, out2, _ = self._run("PreToolUse", {"tool_name": "Read", "tool_input": {"p": "2"}})
        self.assertTrue(self._is_deny(out2))

    def test_weekly_turn_increment_denies(self):
        # weekly usage jumps within the turn beyond hard (5%)
        seq = iter([30.0, 30.0, 40.0])  # UPS sample, PTU1 sample, PTU2 sample
        hook.read_codex_quota = lambda cfg: q_known(next(seq))
        self._run("UserPromptSubmit", {"prompt": "go"})
        self._run("PreToolUse", {"tool_name": "A", "tool_input": {"i": 1}})
        _, out, _ = self._run("PreToolUse", {"tool_name": "B", "tool_input": {"i": 2}})
        self.assertTrue(self._is_deny(out))  # +10% this turn >= hard 5%

    def test_reset_within_turn_not_blocked(self):
        # usage drops (weekly reset) then small climb -> must NOT deny
        seq = iter([60.0, 61.0, 3.0, 4.0])
        hook.read_codex_quota = lambda cfg: q_known(next(seq))
        self._run("UserPromptSubmit", {"prompt": "go"})
        self._run("PreToolUse", {"tool_name": "A", "tool_input": {"i": 1}})
        _, out, _ = self._run("PreToolUse", {"tool_name": "B", "tool_input": {"i": 2}})
        self.assertFalse(self._is_deny(out))

    def test_turn_key_from_payload_session_and_turn_id(self):
        hook.read_codex_quota = lambda cfg: q_known(30.0)
        self._run("UserPromptSubmit", {"prompt": "go", "session_id": "sX", "turn_id": "tY"})
        st = guard.load_state()
        self.assertIn(guard.turn_key({"session_id": "sX", "turn_id": "tY"}), st["turns"])

    def test_two_sessions_do_not_cross_contaminate(self):
        hook.read_codex_quota = lambda cfg: q_known(30.0)
        self._run("UserPromptSubmit", {"prompt": "go", "session_id": "s1", "turn_id": "t1"})
        self._run("UserPromptSubmit", {"prompt": "go", "session_id": "s2", "turn_id": "t1"})

        same_call = {"tool_name": "Bash", "tool_input": {"command": "same"}}
        # session s1 makes the call twice (allowed, max repeat is 3)
        self._run("PreToolUse", {**same_call, "session_id": "s1", "turn_id": "t1"})
        _, out_s1_2, _ = self._run("PreToolUse", {**same_call, "session_id": "s1", "turn_id": "t1"})
        self.assertFalse(self._is_deny(out_s1_2))
        # session s2's own first call must not be affected by s1's fingerprint count
        _, out_s2_1, _ = self._run("PreToolUse", {**same_call, "session_id": "s2", "turn_id": "t1"})
        self.assertFalse(self._is_deny(out_s2_1))
        # third identical call in s1 trips repeat guard; s2 stays independent (still 1st call)
        _, out_s1_3, _ = self._run("PreToolUse", {**same_call, "session_id": "s1", "turn_id": "t1"})
        self.assertTrue(self._is_deny(out_s1_3))
        _, out_s2_2, _ = self._run("PreToolUse", {**same_call, "session_id": "s2", "turn_id": "t1"})
        self.assertFalse(self._is_deny(out_s2_2))  # s2's 2nd call, still under threshold

    def test_precompact_hard_uses_stop_shape_not_permission_decision(self):
        hook.read_codex_quota = lambda cfg: q_known(30.0)
        self._run("UserPromptSubmit", {"prompt": "go"})
        st = guard.load_state()
        key = guard.turn_key({})
        st["turns"][key]["started_at"] -= 3 * 3600  # push past turn_hard_minutes
        guard.save_state(st)
        _, out, _ = self._run("PreCompact", {})
        self.assertTrue(self._is_stop(out))
        self.assertFalse(self._is_deny(out))
        obj = json.loads(out.strip())
        self.assertNotIn("hookSpecificOutput", obj)

    def test_precompact_allows_when_no_hard_finding(self):
        hook.read_codex_quota = lambda cfg: q_known(30.0)
        self._run("UserPromptSubmit", {"prompt": "go"})
        rc, out, _ = self._run("PreCompact", {})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_user_prompt_submit_24h_hard_stops(self):
        # pre-existing samples show a big rolling-24h jump before this new turn starts
        now = 10_000.0
        state = {
            "turns": {},
            "samples": [
                {"ts": now - 3600, "used": 10.0},
                {"ts": now - 1800, "used": 35.0},  # +25% within 24h >= hard 20%
            ],
        }
        guard.save_state(state)
        hook.read_codex_quota = lambda cfg: q_known(35.0)
        import time as time_mod
        orig_time = time_mod.time
        time_mod.time = lambda: now
        try:
            _, out, _ = self._run("UserPromptSubmit", {"prompt": "go", "session_id": "s1", "turn_id": "t1"})
        finally:
            time_mod.time = orig_time
        self.assertTrue(self._is_stop(out))
        # a new turn must still have been initialized despite the stop
        st = guard.load_state()
        self.assertIn(guard.turn_key({"session_id": "s1", "turn_id": "t1"}), st["turns"])

    def test_corrupt_state_fails_closed_for_all_events(self):
        guard.state_path().write_text("{not json", encoding="utf-8")
        hook.read_codex_quota = lambda cfg: q_known(30.0)

        _, out, _ = self._run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertTrue(self._is_deny(out))

        _, out, _ = self._run("PreCompact", {})
        self.assertTrue(self._is_stop(out))

        _, out, _ = self._run("UserPromptSubmit", {"prompt": "go"})
        self.assertTrue(self._is_stop(out))


if __name__ == "__main__":
    unittest.main()
