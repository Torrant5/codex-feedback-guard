"""Per-surface adapter behavior: output shapes, tool/session normalization,
prompt-retention-without-persistence, and the no-permanent-unknown-turn-cap fix.

Adapters are exercised through ``feedback_adapters.dispatch`` (payload dict in,
encoded stdout string out) with an isolated data dir per test."""
import io
import json
import os
import pathlib
import re
import tempfile
import time
import unittest
from contextlib import redirect_stderr

import conftest_paths  # noqa: F401

from exi import config, feedback as fb
from exi import feedback_adapters as ad
from exi import feedback_core

CORRECTIVE = "二度と勝手にコミットしないで。前にも言った。"


class AdapterBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EXI_DATA_DIR"] = self.tmp.name
        os.environ.pop("EXI_CONFIG", None)
        self.store = fb.FeedbackStore(data_dir=self.tmp.name)

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        os.environ.pop("EXI_CONFIG", None)
        self.tmp.cleanup()

    def dispatch(self, provider, event, payload):
        cfg = config.load_config()
        err = io.StringIO()
        with redirect_stderr(err):
            out = ad.dispatch(provider, event, payload, cfg)
        return out, err.getvalue()

    def make_rule(self, name, n_evidence, spec, desc="d"):
        for i in range(n_evidence):
            self.store.record(name, desc, evidence=f"{name}-ev{i}")
        self.store.configure(name, spec)

    def state(self):
        return fb.FeedbackState(data_dir=self.tmp.name)


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
class ClaudeAdapterTest(AdapterBase):
    def test_user_prompt_additional_context_single_doc(self):
        self.make_rule("r", 4, {"event": "pre_bash", "when": "x"}, desc="high rule")
        out, _ = self.dispatch("claude", "UserPromptSubmit",
                               {"session_id": "s1", "prompt": "hi"})
        obj = json.loads(out)  # exactly one JSON document
        self.assertIn("high rule",
                      obj["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(obj["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_pre_tool_deny_shape(self):
        self.make_rule("d", 5, {"event": "pre_bash", "when": "rm -rf"}, desc="never rm -rf")
        out, _ = self.dispatch("claude", "PreToolUse",
                               {"session_id": "s1", "tool_name": "Bash",
                                "tool_input": {"command": "rm -rf /x"}})
        obj = json.loads(out)
        self.assertEqual(obj["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stop_block_top_level_decision(self):
        self._seed_stop_block("claude")
        out, _ = self.dispatch("claude", "Stop", {"session_id": "s1", "turn_id": "t1"})
        obj = json.loads(out)
        self.assertEqual(obj.get("decision"), "block")

    def test_stop_hook_active_prevents_block(self):
        self._seed_stop_block("claude")
        out, _ = self.dispatch("claude", "Stop",
                               {"session_id": "s1", "turn_id": "t1", "stop_hook_active": True})
        self.assertEqual(json.loads(out), {})  # self-limiting: never block again

    def _seed_stop_block(self, provider):
        self.make_rule("no-debug", 5, {"event": "stop_check", "forbid_regex": "print\\("},
                       desc="remove prints")
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('debug')\n")
        sid = feedback_core.namespace_session(provider, "s1")
        with self.state().locked() as st:
            fb.track_changed_files(st, sid, self.tmp.name, [target])


# --------------------------------------------------------------------------- #
# Copilot in VS Code
# --------------------------------------------------------------------------- #
class VscodeAdapterTest(AdapterBase):
    def test_stop_block_wrapped_in_hookspecificoutput(self):
        self.make_rule("no-debug", 5, {"event": "stop_check", "forbid_regex": "print\\("},
                       desc="remove prints")
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('debug')\n")
        sid = feedback_core.namespace_session("copilot-vscode", "s1")
        with self.state().locked() as st:
            fb.track_changed_files(st, sid, self.tmp.name, [target])
        out, _ = self.dispatch("copilot-vscode", "Stop", {"session_id": "s1", "turn_id": "t1"})
        obj = json.loads(out)
        self.assertEqual(obj["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertEqual(obj["hookSpecificOutput"]["decision"], "block")

    def test_editfiles_tool_name_tracked_at_post(self):
        target = os.path.join(self.tmp.name, "y.py")
        with open(target, "w") as f:
            f.write("x = 1\n")
        self.dispatch("copilot-vscode", "PostToolUse",
                      {"session_id": "s1", "cwd": self.tmp.name,
                       "tool_name": "editFiles", "tool_input": {"filePath": target, "content": "x=1"}})
        sid = feedback_core.namespace_session("copilot-vscode", "s1")
        with self.state().locked() as st:
            files = fb.tracked_files(st, sid)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("y.py"))

    def test_user_prompt_additional_context(self):
        self.make_rule("r", 4, {"event": "pre_bash", "when": "x"}, desc="vscode rule")
        out, _ = self.dispatch("copilot-vscode", "UserPromptSubmit",
                               {"session_id": "s1", "prompt": "hi"})
        self.assertIn("vscode rule",
                      json.loads(out)["hookSpecificOutput"]["additionalContext"])


# --------------------------------------------------------------------------- #
# Copilot CLI
# --------------------------------------------------------------------------- #
class CopilotCliAdapterTest(AdapterBase):
    def test_transformed_prompt_retained_verbatim_then_appended(self):
        out, _ = self.dispatch("copilot-cli", "userPromptTransformed",
                               {"sessionId": "s1", "prompt": CORRECTIVE,
                                "transformedPrompt": "ORIGINAL_TP_VERBATIM"})
        obj = json.loads(out)
        mtp = obj["modifiedTransformedPrompt"]
        self.assertTrue(mtp.startswith("ORIGINAL_TP_VERBATIM\n\n"))
        self.assertIn("feedback candidate", mtp)

    def test_transformed_prompt_only_can_create_candidate(self):
        out, _ = self.dispatch("copilot-cli", "userPromptTransformed",
                               {"sessionId": "s1",
                                "transformedPrompt": CORRECTIVE})
        obj = json.loads(out)
        self.assertIn("feedback candidate", obj["modifiedTransformedPrompt"])

    def test_hook_output_is_ascii_safe_for_windows_console(self):
        out = ad._dumps({"message": "— 日本語"})
        self.assertTrue(out.isascii())

    def test_no_context_returns_empty_object(self):
        out, _ = self.dispatch("copilot-cli", "userPromptTransformed",
                               {"sessionId": "s1", "prompt": "just a normal question?",
                                "transformedPrompt": "TP"})
        self.assertEqual(json.loads(out), {})

    def test_pascalcase_event_alias_accepted(self):
        out, _ = self.dispatch("copilot-cli", "UserPromptTransformed",
                               {"sessionId": "s1", "prompt": "normal", "transformedPrompt": "TP"})
        self.assertEqual(json.loads(out), {})

    def test_pre_tool_top_level_permission_decision(self):
        self.make_rule("d", 5, {"event": "pre_bash", "when": "rm -rf"}, desc="never rm -rf")
        out, _ = self.dispatch("copilot-cli", "preToolUse",
                               {"sessionId": "s1", "toolName": "executeCommand",
                                "toolArgs": {"command": "rm -rf /x"}})
        obj = json.loads(out)
        self.assertEqual(obj["permissionDecision"], "deny")
        self.assertNotIn("hookSpecificOutput", obj)

    def test_agent_stop_top_level_decision_and_self_limit(self):
        self.make_rule("no-debug", 5, {"event": "stop_check", "forbid_regex": "print\\("},
                       desc="remove prints")
        target = os.path.join(self.tmp.name, "x.py")
        with open(target, "w") as f:
            f.write("print('debug')\n")
        sid = feedback_core.namespace_session("copilot-cli", "s1")
        with self.state().locked() as st:
            fb.track_changed_files(st, sid, self.tmp.name, [target])
        out, _ = self.dispatch("copilot-cli", "agentStop", {"sessionId": "s1", "turn_id": "t1"})
        self.assertEqual(json.loads(out).get("decision"), "block")
        # stop_hook_active honored (top-level snake key).
        out2, _ = self.dispatch("copilot-cli", "agentStop",
                                {"sessionId": "s1", "turn_id": "t1", "stop_hook_active": True})
        self.assertEqual(json.loads(out2), {})


# --------------------------------------------------------------------------- #
# Cross-cutting: namespacing, prompt-retention, no permanent unknown-turn cap
# --------------------------------------------------------------------------- #
class SessionNamespacingTest(AdapterBase):
    def test_same_raw_session_isolated_across_providers(self):
        # Same raw session id "s1" on two surfaces must not share candidates.
        self.dispatch("claude", "UserPromptSubmit",
                      {"session_id": "s1", "turn_id": "t1", "prompt": CORRECTIVE})
        self.dispatch("copilot-cli", "userPromptTransformed",
                      {"sessionId": "s1", "turn_id": "t1", "prompt": CORRECTIVE,
                       "transformedPrompt": "TP"})
        with self.state().locked() as st:
            sessions = {c["session_id"] for c in st["candidates"]}
        self.assertEqual(sessions,
                         {feedback_core.namespace_session("claude", "s1"),
                          feedback_core.namespace_session("copilot-cli", "s1")})


class PromptRetentionTest(AdapterBase):
    SECRET = "二度と勝手にSUPERSECRETTOKEN999コミットしないで。前にも言った。"

    def test_no_raw_prompt_or_transformed_prompt_persisted(self):
        self.dispatch("copilot-cli", "userPromptTransformed",
                      {"sessionId": "s1", "turn_id": "t1", "prompt": self.SECRET,
                       "transformedPrompt": "TP_WITH_SUPERSECRETTOKEN999"})
        for p in pathlib.Path(self.tmp.name).rglob("*"):
            if p.is_file():
                data = p.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("SUPERSECRETTOKEN999", data, f"prompt leaked into {p}")


class NoPermanentUnknownTurnCapTest(AdapterBase):
    """A surface with NO turn id (Claude/Copilot often lack one) must not share a
    single permanent Stop counter across turns: each new prompt rotates a fresh
    active-turn key, so a later turn can block again after an earlier turn hit
    the cap."""

    def _prompt_no_turn(self):
        self.dispatch("claude", "UserPromptSubmit", {"session_id": "s1", "prompt": CORRECTIVE})

    def _stop_no_turn(self):
        out, _ = self.dispatch("claude", "Stop", {"session_id": "s1"})
        return json.loads(out).get("decision") == "block"

    def test_second_turn_can_block_after_first_turn_hit_cap(self):
        # Turn 1: prompt (creates pending candidate + rotates active turn), then
        # exhaust the 3-block cap.
        self._prompt_no_turn()
        first = sum(1 for _ in range(3) if self._stop_no_turn())
        self.assertEqual(first, 3)
        self.assertFalse(self._stop_no_turn())  # capped -> no more blocks, abandons

        # Turn 2: a NEW prompt rotates a fresh active-turn key -> fresh counter.
        self._prompt_no_turn()
        self.assertTrue(self._stop_no_turn(), "second turn must get its own Stop counter")

    def test_turn_id_on_prompt_but_not_on_stop_stays_per_turn(self):
        # Mixed shape: the surface supplies a turn id on the prompt but omits it
        # on Stop. The active turn persisted at prompt time must give each turn
        # its own Stop counter (not one permanent cross-turn cap).
        def prompt(turn):
            self.dispatch("claude", "UserPromptSubmit",
                          {"session_id": "s1", "turn_id": turn, "prompt": CORRECTIVE})

        def stop_blocks():
            out, _ = self.dispatch("claude", "Stop", {"session_id": "s1"})  # no turn id
            return json.loads(out).get("decision") == "block"

        prompt("turn-A")
        self.assertEqual(sum(1 for _ in range(3) if stop_blocks()), 3)
        self.assertFalse(stop_blocks())  # turn A capped
        prompt("turn-B")
        self.assertTrue(stop_blocks(), "turn B must recover its own Stop counter")

    def test_approval_prompt_rotates_turn_before_stop(self):
        self._prompt_no_turn()
        self.assertEqual(sum(1 for _ in range(3) if self._stop_no_turn()), 3)
        self.assertFalse(self._stop_no_turn())

        # An unmatched exact approval marker creates no candidate, but it is
        # still a distinct user turn and must rotate the synthetic turn id.
        self.dispatch("claude", "UserPromptSubmit",
                      {"session_id": "s1", "prompt": "ALLOW_FEEDBACK:not-found"})
        sid = feedback_core.namespace_session("claude", "s1")
        with self.state().locked() as st:
            now = time.time()
            fb.upsert_candidate(st, "manual-candidate", sid, "approval-turn",
                                "hash", ["correction"], now, 3600)
        self.assertTrue(self._stop_no_turn(),
                        "approval turn must not inherit the prior turn's exhausted cap")


if __name__ == "__main__":
    unittest.main()
