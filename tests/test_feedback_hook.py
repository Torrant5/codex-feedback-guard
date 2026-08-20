import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

import conftest_paths  # noqa: F401

from exi import feedback as fb
from exi import feedback_hook


class FeedbackHookBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EXI_DATA_DIR"] = self.tmp.name
        os.environ.pop("EXI_CONFIG", None)
        self.store = fb.FeedbackStore(data_dir=self.tmp.name)

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        os.environ.pop("EXI_CONFIG", None)
        self.tmp.cleanup()

    def run_hook(self, event, payload):
        hook_in = io.StringIO(json.dumps(payload))
        out, err = io.StringIO(), io.StringIO()
        orig = sys.stdin
        sys.stdin = hook_in
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = feedback_hook.handle(event)
        finally:
            sys.stdin = orig
        return rc, out.getvalue(), err.getvalue()

    def is_deny(self, out):
        out = out.strip()
        if not out:
            return False
        obj = json.loads(out)
        return obj.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def deny_reason(self, out):
        return json.loads(out.strip())["hookSpecificOutput"]["permissionDecisionReason"]

    def additional_context(self, out):
        out = out.strip()
        if not out:
            return ""
        return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")

    def make_rule(self, name, n_evidence, spec, desc="d", enabled=True):
        for i in range(n_evidence):
            self.store.record(name, desc, evidence=f"{name}-ev{i}")
        self.store.configure(name, spec)
        if not enabled:
            self.store.set_enabled(name, False)


# --------------------------------------------------------------------------- #
# UserPromptSubmit: injection
# --------------------------------------------------------------------------- #
class InjectionTest(FeedbackHookBase):
    def test_injects_only_count_ge_3(self):
        self.make_rule("low", 2, {"event": "pre_bash", "when": "x"}, desc="low-count rule")
        self.make_rule("high", 4, {"event": "pre_bash", "when": "y"}, desc="high-count rule")
        _, out, _ = self.run_hook("UserPromptSubmit", {"prompt": "hello", "session_id": "s1"})
        ctx = self.additional_context(out)
        self.assertIn("high-count rule", ctx)
        self.assertNotIn("low-count rule", ctx)

    def test_disabled_not_injected(self):
        self.make_rule("d", 4, {"event": "pre_bash", "when": "x"}, desc="disabled rule", enabled=False)
        _, out, _ = self.run_hook("UserPromptSubmit", {"prompt": "hi", "session_id": "s1"})
        self.assertNotIn("disabled rule", self.additional_context(out))

    def test_sorted_by_count_desc(self):
        self.make_rule("mid", 3, {"event": "pre_bash", "when": "x"}, desc="MID")
        self.make_rule("top", 7, {"event": "pre_bash", "when": "y"}, desc="TOP")
        _, out, _ = self.run_hook("UserPromptSubmit", {"prompt": "hi", "session_id": "s1"})
        ctx = self.additional_context(out)
        self.assertLess(ctx.index("TOP"), ctx.index("MID"))

    def test_budget_capped(self):
        cfg_path = os.path.join(self.tmp.name, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump({"feedback": {"inject_max_chars": 400, "inject_min_count": 3}}, f)
        os.environ["EXI_CONFIG"] = cfg_path
        for i in range(20):
            self.make_rule(f"r{i:02d}", 3 + (i % 5), {"event": "pre_bash", "when": "x"},
                           desc="X" * 60)
        _, out, _ = self.run_hook("UserPromptSubmit", {"prompt": "hi", "session_id": "s1"})
        ctx = self.additional_context(out)
        self.assertLessEqual(len(ctx), 400)
        self.assertTrue(ctx)  # still produced something

    def test_injection_hard_capped_at_3000_even_if_config_larger(self):
        # Config can never raise the injection budget above the absolute 3000.
        cfg_path = os.path.join(self.tmp.name, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump({"feedback": {"inject_max_chars": 100000, "inject_min_count": 3}}, f)
        os.environ["EXI_CONFIG"] = cfg_path
        for i in range(80):
            self.make_rule(f"r{i:02d}", 3, {"event": "pre_bash", "when": "x"}, desc="X" * 90)
        _, out, _ = self.run_hook("UserPromptSubmit", {"prompt": "hi", "session_id": "s1"})
        ctx = self.additional_context(out)
        self.assertTrue(ctx)
        self.assertLessEqual(len(ctx), 3000)

    def test_injection_filtered_by_scope_against_cwd(self):
        # A rule whose scope is not a substring of the payload cwd is not injected.
        for ev in ("a", "b", "c"):
            self.store.record("inscope", "IN-SCOPE RULE", evidence=f"in-{ev}", scope="/proj/a")
            self.store.record("outscope", "OUT-SCOPE RULE", evidence=f"out-{ev}", scope="/proj/b")
        _, out, _ = self.run_hook(
            "UserPromptSubmit",
            {"prompt": "hi", "session_id": "s1", "cwd": "/proj/a/work"},
        )
        ctx = self.additional_context(out)
        self.assertIn("IN-SCOPE RULE", ctx)
        self.assertNotIn("OUT-SCOPE RULE", ctx)


# --------------------------------------------------------------------------- #
# PreToolUse: warn / pause / deny
# --------------------------------------------------------------------------- #
class PreToolWarnDenyTest(FeedbackHookBase):
    def test_warn_does_not_block(self):
        self.make_rule("w", 1, {"event": "pre_bash", "when": "training-job"}, desc="use job-wrapper")
        rc, out, err = self.run_hook(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "python training-job.py"}, "session_id": "s1"},
        )
        self.assertEqual(rc, 0)
        self.assertFalse(self.is_deny(out))
        self.assertIn("use job-wrapper", self.additional_context(out) + err)

    def test_deny_hard_at_count_5(self):
        self.make_rule("d", 5, {"event": "pre_bash", "when": "rm -rf"}, desc="never rm -rf")
        _, out, _ = self.run_hook(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}, "session_id": "s1"},
        )
        self.assertTrue(self.is_deny(out))

    def test_no_match_allows(self):
        self.make_rule("d", 5, {"event": "pre_bash", "when": "rm -rf"})
        rc, out, _ = self.run_hook(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1"},
        )
        self.assertEqual(rc, 0)
        self.assertFalse(self.is_deny(out))

    def test_pretool_records_one_violation_per_rule_per_call_no_count_change(self):
        # Two specs of the SAME rule both match one call -> exactly ONE violation
        # event, and count is never changed by enforcement.
        self.make_rule(
            "multi", 1,
            [{"event": "pre_bash", "when": "foo"}, {"event": "pre_bash", "forbid_regex": "foo"}],
            desc="use job-wrapper",
        )
        before = self.store.get("multi").count
        payload = {"tool_name": "Bash", "tool_input": {"command": "foo bar"}, "session_id": "s1"}
        self.run_hook("PreToolUse", payload)
        after = self.store.get("multi")
        self.assertEqual(after.count, before)          # count unchanged
        self.assertEqual(len(after.violations), 1)     # one event for this call
        # A second, separate tool call records its own single event (count still fixed).
        self.run_hook("PreToolUse", payload)
        again = self.store.get("multi")
        self.assertEqual(again.count, before)
        self.assertEqual(len(again.violations), 2)


# --------------------------------------------------------------------------- #
# PreToolUse: pause flow via nonce (UserPromptSubmit approval)
# --------------------------------------------------------------------------- #
class PauseFlowTest(FeedbackHookBase):
    def test_normal_pause_uses_bounded_ttl(self):
        self.assertEqual(feedback_hook._approval_ttl({"approval_ttl_seconds": 0}), 1)
        self.assertEqual(feedback_hook._approval_ttl({"approval_ttl_seconds": 999999}), 86400)
        self.assertEqual(feedback_hook._approval_ttl({"approval_ttl_seconds": "bad"}), 600)

    def _payload(self, session="s1"):
        return {"tool_name": "Bash", "tool_input": {"command": "deploy prod"}, "session_id": session}

    def setUp(self):
        super().setUp()
        # count 3 -> auto pause
        self.make_rule("pause-rule", 3, {"event": "pre_bash", "when": "deploy prod"}, desc="confirm deploys")

    def _nonce_from(self, out):
        m = re.search(r"ALLOW_FEEDBACK:([0-9a-f]+)", self.deny_reason(out))
        self.assertIsNotNone(m)
        return m.group(1)

    def test_pause_denies_then_approves_then_one_shot(self):
        # 1) first attempt: paused (deny) with a nonce
        _, out1, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(out1))
        nonce = self._nonce_from(out1)

        # 2) user approves out of band via UserPromptSubmit exact reply
        _, _, _ = self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK:{nonce}", "session_id": "s1"})

        # 3) same tool call now permitted exactly once (no deny)
        _, out3, _ = self.run_hook("PreToolUse", self._payload())
        self.assertFalse(self.is_deny(out3))

        # 4) a further identical retry is paused again (one-shot consumed)
        _, out4, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(out4))

    def test_forged_nonce_does_not_approve(self):
        _, out1, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(out1))
        # model cannot invent a working nonce
        self.run_hook("UserPromptSubmit", {"prompt": "ALLOW_FEEDBACK:deadbeefdeadbeef", "session_id": "s1"})
        _, out3, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(out3))

    def test_approval_bound_to_session(self):
        _, out1, _ = self.run_hook("PreToolUse", self._payload("s1"))
        nonce = self._nonce_from(out1)
        # approve in a DIFFERENT session — must not unlock s1
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK:{nonce}", "session_id": "s2"})
        _, out3, _ = self.run_hook("PreToolUse", self._payload("s1"))
        self.assertTrue(self.is_deny(out3))

    def test_hard_deny_not_bypassable_by_nonce(self):
        # bump the same rule to count 5 -> deny; a prior pause nonce must not help
        for i in range(2):
            self.store.record("pause-rule", "d", evidence=f"more-{i}")
        _, out1, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(out1))
        # there is no nonce on a hard deny
        self.assertNotIn("ALLOW_FEEDBACK:", self.deny_reason(out1))

    def test_normal_marker_must_match_exactly(self):
        # Prefix/suffix around the marker must NOT approve; only the entire
        # stripped prompt matching ALLOW_FEEDBACK:<16 hex> approves. Each
        # PreToolUse re-mints the pending nonce, so read it fresh every time.
        _, o1, _ = self.run_hook("PreToolUse", self._payload())
        n1 = self._nonce_from(o1)
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK:{n1} please", "session_id": "s1"})
        _, o2, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(o2))  # trailing text -> not approved
        n2 = self._nonce_from(o2)
        self.run_hook("UserPromptSubmit", {"prompt": f"go ALLOW_FEEDBACK:{n2}", "session_id": "s1"})
        _, o3, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(o3))  # leading text -> not approved
        n3 = self._nonce_from(o3)
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK:{n3}", "session_id": "s1"})
        _, o4, _ = self.run_hook("PreToolUse", self._payload())
        self.assertFalse(self.is_deny(o4))  # exact -> approved once


# --------------------------------------------------------------------------- #
# PostToolUse tracking + Stop
# --------------------------------------------------------------------------- #
class PostAndStopTest(FeedbackHookBase):
    def test_post_tool_tracks_edit_files(self):
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('debug')\n")
        self.run_hook(
            "PostToolUse",
            {"tool_name": "Write", "tool_input": {"file_path": target, "content": "print('debug')"},
             "session_id": "s1", "cwd": self.tmp.name},
        )
        st = fb.FeedbackState(data_dir=self.tmp.name)
        with st.locked() as state:
            files = fb.tracked_files(state, "s1")
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("x.py"))

    def test_post_tool_ignores_bash(self):
        self.run_hook(
            "PostToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "session_id": "s1"},
        )
        st = fb.FeedbackState(data_dir=self.tmp.name)
        with st.locked() as state:
            self.assertEqual(fb.tracked_files(state, "s1"), [])

    def _track(self, path, session="s1"):
        st = fb.FeedbackState(data_dir=self.tmp.name)
        with st.locked() as state:
            fb.track_changed_files(state, session, self.tmp.name, [path])

    def test_stop_blocks_up_to_three_then_stops(self):
        # count 5 -> deny severity in stop -> blocking
        self.make_rule("no-debug", 5, {"event": "stop_check", "forbid_regex": "print\\("},
                       desc="remove debug prints")
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('debug')\n")
        self._track(target)
        payload = {"session_id": "s1", "turn_id": "t1"}

        blocks = 0
        for _ in range(3):
            _, out, _ = self.run_hook("Stop", payload)
            obj = json.loads(out.strip())
            if obj.get("decision") == "block":
                blocks += 1
        self.assertEqual(blocks, 3)

        # 4th time: must NOT block (loop guard), but still valid JSON
        _, out, _ = self.run_hook("Stop", payload)
        obj = json.loads(out.strip())
        self.assertNotEqual(obj.get("decision"), "block")

    def test_stop_cap_hard_clamped_when_config_exceeds_three(self):
        # config asks for 10 blocks; the hard ceiling clamps it to 3.
        cfg_path = os.path.join(self.tmp.name, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump({"feedback": {"stop_max_blocks": 10}}, f)
        os.environ["EXI_CONFIG"] = cfg_path
        self.make_rule("no-debug", 5, {"event": "stop_check", "forbid_regex": "print\\("},
                       desc="remove debug prints")
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('debug')\n")
        self._track(target)
        payload = {"session_id": "s1", "turn_id": "t1"}
        blocks = 0
        for _ in range(6):
            _, out, _ = self.run_hook("Stop", payload)
            if json.loads(out.strip()).get("decision") == "block":
                blocks += 1
        self.assertEqual(blocks, 3)  # never more than the hard ceiling

    def test_alternating_stop_rules_cannot_exceed_three_blocks_per_turn(self):
        # Two deny-level stop rules; a DIFFERENT single rule fires each attempt
        # (content alternates). The session+turn cap must still hold at 3 total
        # blocks for the turn — the rule set does not mint a fresh counter.
        self.make_rule("no-aaa", 5, {"event": "stop_check", "forbid_regex": "AAA"}, desc="no AAA")
        self.make_rule("no-bbb", 5, {"event": "stop_check", "forbid_regex": "BBB"}, desc="no BBB")
        target = os.path.join(self.tmp.name, "x.py")
        self._track(target)
        payload = {"session_id": "s1", "turn_id": "t1"}
        blocks = 0
        for content in ("AAA\n", "BBB\n", "AAA\n", "BBB\n", "AAA\n"):
            with open(target, "w") as f:
                f.write(content)
            _, out, _ = self.run_hook("Stop", payload)
            if json.loads(out.strip()).get("decision") == "block":
                blocks += 1
        self.assertEqual(blocks, 3)

    def test_stop_warn_does_not_block(self):
        self.make_rule("warn-rule", 1, {"event": "stop_check", "forbid_regex": "print\\("},
                       desc="prefer logging")
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('x')\n")
        self._track(target)
        _, out, _ = self.run_hook("Stop", {"session_id": "s1", "turn_id": "t1"})
        obj = json.loads(out.strip())
        self.assertNotEqual(obj.get("decision"), "block")

    def test_stop_no_rules_emits_empty_json(self):
        _, out, _ = self.run_hook("Stop", {"session_id": "s1", "turn_id": "t1"})
        self.assertEqual(json.loads(out.strip()), {})

    def test_stop_records_violation_without_incrementing_count(self):
        self.make_rule("no-debug", 5, {"event": "stop_check", "forbid_regex": "print\\("},
                       desc="remove debug prints")
        before = self.store.get("no-debug").count
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('debug')\n")
        self._track(target)
        self.run_hook("Stop", {"session_id": "s1", "turn_id": "t1"})
        after = self.store.get("no-debug")
        self.assertEqual(after.count, before)
        self.assertTrue(len(after.violations) >= 1)


# --------------------------------------------------------------------------- #
# Built-in administrative gate: exi feedback configure|disable|enable
# --------------------------------------------------------------------------- #
class AdminGateTest(FeedbackHookBase):
    def _payload(self, cmd="bin/exi feedback disable r", session="s1"):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session}

    def _admin_nonce(self, out):
        m = re.search(r"ALLOW_FEEDBACK_ADMIN:([0-9a-f]{16})", self.deny_reason(out))
        self.assertIsNotNone(m)
        return m.group(1)

    def test_record_is_not_gated(self):
        _, out, _ = self.run_hook(
            "PreToolUse", self._payload("bin/exi feedback record --name r --evidence e"))
        self.assertFalse(self.is_deny(out))

    def test_denies_then_exact_admin_marker_approves_one_shot(self):
        _, o1, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(o1))
        self.assertIn("ALLOW_FEEDBACK_ADMIN:", self.deny_reason(o1))

        # A normal ALLOW_FEEDBACK marker cannot satisfy the admin gate.
        n1 = self._admin_nonce(o1)
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK:{n1}", "session_id": "s1"})
        _, o2, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(o2))

        # The exact admin marker approves; next identical call is allowed once.
        n2 = self._admin_nonce(o2)
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK_ADMIN:{n2}", "session_id": "s1"})
        _, o3, _ = self.run_hook("PreToolUse", self._payload())
        self.assertFalse(self.is_deny(o3))

        # One-shot: a further identical call pauses again.
        _, o4, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(o4))

    def test_admin_marker_must_match_exactly(self):
        _, o1, _ = self.run_hook("PreToolUse", self._payload())
        n1 = self._admin_nonce(o1)
        # trailing text -> not exact -> not approved
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK_ADMIN:{n1} yes", "session_id": "s1"})
        _, o2, _ = self.run_hook("PreToolUse", self._payload())
        self.assertTrue(self.is_deny(o2))

    def test_admin_approval_bound_to_session(self):
        _, o1, _ = self.run_hook("PreToolUse", self._payload(session="s1"))
        nonce = self._admin_nonce(o1)
        # approve in a different session -> must not unlock s1
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK_ADMIN:{nonce}", "session_id": "s2"})
        _, o3, _ = self.run_hook("PreToolUse", self._payload(session="s1"))
        self.assertTrue(self.is_deny(o3))

    def test_admin_approval_bound_to_exact_fingerprint(self):
        _, o1, _ = self.run_hook("PreToolUse", self._payload("bin/exi feedback disable r"))
        nonce = self._admin_nonce(o1)
        self.run_hook("UserPromptSubmit", {"prompt": f"ALLOW_FEEDBACK_ADMIN:{nonce}", "session_id": "s1"})
        # a DIFFERENT admin command (different fingerprint) is not unlocked
        _, o2, _ = self.run_hook("PreToolUse", self._payload("bin/exi feedback enable r"))
        self.assertTrue(self.is_deny(o2))

    def test_hard_deny_precedes_admin_gate(self):
        # An independently matched hard-deny rule wins: hard block, no admin nonce.
        self.make_rule("frozen", 5, {"event": "pre_bash", "when": "feedback disable"},
                       desc="enforcement is frozen")
        _, o, _ = self.run_hook("PreToolUse", self._payload("bin/exi feedback disable r"))
        self.assertTrue(self.is_deny(o))
        reason = self.deny_reason(o)
        self.assertIn("Blocked by recurring feedback", reason)
        self.assertNotIn("ALLOW_FEEDBACK_ADMIN:", reason)


# --------------------------------------------------------------------------- #
# Fail-open on internal error
# --------------------------------------------------------------------------- #
class FailOpenTest(FeedbackHookBase):
    def test_corrupt_data_fails_open_pretooluse(self):
        self.make_rule("r", 5, {"event": "pre_bash", "when": "x"})
        # corrupt the rule log
        with open(self.store.log_path, "a") as f:
            f.write("{broken\n")
        rc, out, err = self.run_hook(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "x"}, "session_id": "s1"},
        )
        self.assertEqual(rc, 0)
        self.assertFalse(self.is_deny(out))  # failed OPEN, did not block
        self.assertIn("fail-open", err)

    def test_corrupt_data_stop_still_emits_json(self):
        self.make_rule("r", 5, {"event": "stop_check", "forbid_regex": "x"})
        with open(self.store.log_path, "a") as f:
            f.write("{broken\n")
        rc, out, err = self.run_hook("Stop", {"session_id": "s1", "turn_id": "t1"})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.strip()), {})  # valid JSON on fail-open
        self.assertIn("fail-open", err)

    def test_unexpected_exception_fails_open(self):
        orig = feedback_hook._handle_pre_tool_use
        feedback_hook._HANDLERS["PreToolUse"] = lambda p, c: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            rc, out, err = self.run_hook(
                "PreToolUse",
                {"tool_name": "Bash", "tool_input": {"command": "x"}, "session_id": "s1"},
            )
        finally:
            feedback_hook._HANDLERS["PreToolUse"] = orig
        self.assertEqual(rc, 0)
        self.assertFalse(self.is_deny(out))
        self.assertIn("boom", err)


if __name__ == "__main__":
    unittest.main()
