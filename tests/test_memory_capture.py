"""Autonomous durable-memory capture.

Covers the store trust model (user-authoritative `active` vs evidence-disciplined
`confirmed`), the body-less per-turn memory-candidate lifecycle, secret
rejection, the `exi memory resolve/dismiss/candidates` CLI, and the always-on
per-turn instruction across all four surfaces — including the transformedPrompt-
only Copilot CLI, a proof that no raw prompt/secret is persisted, the combined
feedback+memory injection shape, no Stop-block on an ordinary turn, and
Windows-safe (ASCII) output.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

import conftest_paths  # noqa: F401

from exi import config, exicli, feedback as fb, feedback_detect as fbd, secretscan
from exi import feedback_adapters as ad
from exi import feedback_core, feedback_hook
from exi.store import (
    AUTHORITATIVE_KINDS,
    MEM_KINDS,
    STATUS_ACTIVE,
    STATUS_CONFIRMED,
    Store,
    normalize_claim,
)


# --------------------------------------------------------------------------- #
# Store.remember: trust model + evidence discipline
# --------------------------------------------------------------------------- #
class RememberTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_preference_is_authoritative_active_on_single_source(self):
        obs, action = self.store.remember(
            "workflow/testing", "Run pytest -q before committing", "preference",
            evidence_paths=["turn:aaaa"], triggers=["commit"], authoritative=True)
        self.assertEqual(action, "created")
        self.assertEqual(obs.status, STATUS_ACTIVE)
        self.assertEqual(obs.confirmed_count, 1)  # NOT faked as two sources
        # Retrievable immediately.
        hits = self.store.retrieve("should I commit now", cwd="/proj")
        self.assertEqual([o.id for o in hits], [obs.id])

    def test_technical_fact_stays_candidate_until_second_source(self):
        obs, action = self.store.remember(
            "bug/deadlock", "Deadlock from reversed lock order in the worker pool",
            "root-cause", evidence_paths=["trace/deadlock-a.txt"], authoritative=False)
        self.assertEqual(action, "created")
        self.assertNotEqual(obs.status, STATUS_CONFIRMED)
        self.assertEqual(self.store.retrieve("deadlock lock order worker", cwd="/x"), [])
        # A genuinely different verified source confirms it.
        obs2, action2 = self.store.remember(
            "bug/deadlock", "Deadlock from reversed lock order in the worker pool",
            "root-cause", evidence_paths=["src/worker_pool.py"], authoritative=False)
        self.assertEqual(action2, "reinforced")
        self.assertEqual(obs2.id, obs.id)
        self.assertEqual(obs2.status, STATUS_CONFIRMED)
        self.assertEqual(obs2.confirmed_count, 2)
        self.assertTrue(self.store.retrieve("deadlock lock order worker", cwd="/x"))

    def test_turn_hashes_never_count_as_technical_evidence(self):
        obs, _ = self.store.remember(
            "env/build", "Build requires the internal wrapper", "environment",
            evidence_paths=["turn:first", "turn:second"], authoritative=False)
        self.assertEqual(obs.confirmed_count, 0)
        self.assertNotEqual(obs.status, STATUS_CONFIRMED)
        self.assertEqual(self.store.retrieve("internal build wrapper", cwd="/x"), [])

    def test_same_source_is_duplicate_no_inflation(self):
        self.store.remember("s", "claim one", "procedure",
                            evidence_paths=["docs/same.md"], authoritative=False)
        obs, action = self.store.remember("s", "claim one", "procedure",
                                          evidence_paths=["docs/same.md"], authoritative=False)
        self.assertEqual(action, "duplicate")
        self.assertEqual(obs.confirmed_count, 1)

    def test_active_not_downgraded_by_reinforcement(self):
        self.store.remember("s", "prefer tabs", "preference",
                            evidence_paths=["turn:a"], authoritative=True)
        obs, action = self.store.remember("s", "prefer tabs", "preference",
                                          evidence_paths=["turn:b"], authoritative=True)
        self.assertEqual(action, "reinforced")
        self.assertEqual(obs.status, STATUS_ACTIVE)  # stays authoritative
        self.assertEqual(obs.confirmed_count, 2)

    def test_dedup_by_normalized_claim_within_scope(self):
        a, _ = self.store.remember("s", "Prefer  Tabs", "preference",
                                   evidence_paths=["turn:a"], authoritative=True)
        b, action = self.store.remember("s", "prefer tabs", "preference",
                                        evidence_paths=["turn:b"], authoritative=True)
        self.assertEqual(a.id, b.id)
        self.assertEqual(action, "reinforced")

    def test_different_scope_is_distinct_memory(self):
        a, _ = self.store.remember("s1", "same claim", "preference",
                                   evidence_paths=["turn:a"], authoritative=True)
        b, action = self.store.remember("s2", "same claim", "preference",
                                        evidence_paths=["turn:a"], authoritative=True)
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(action, "created")

    def test_retrieval_includes_active_and_confirmed_excludes_candidate(self):
        self.store.remember("pref", "user pref claim", "preference",
                            evidence_paths=["turn:a"], authoritative=True)
        self.store.remember("cand", "under evidenced technical claim", "procedure",
                            evidence_paths=["turn:b"], authoritative=False)
        c = self.store.capture("conf", "confirmed technical claim", evidence_paths=["x"])
        self.store.confirm(c.id, evidence_paths=["y"])
        scopes = {o.scope for o in self.store.retrieve("claim", cwd="/x")}
        self.assertIn("pref", scopes)     # active
        self.assertIn("conf", scopes)     # confirmed
        self.assertNotIn("cand", scopes)  # under-evidenced candidate excluded

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.store.remember("s", "c", "not-a-kind",
                                evidence_paths=["turn:a"], authoritative=False)

    def test_authoritative_flag_must_match_kind(self):
        with self.assertRaises(ValueError):
            self.store.remember("s", "technical claim", "environment",
                                evidence_paths=["evidence/a"], authoritative=True)
        with self.assertRaises(ValueError):
            self.store.remember("s", "user preference", "preference",
                                evidence_paths=["turn:a"], authoritative=False)

    def test_kind_stored_and_in_to_dict(self):
        obs, _ = self.store.remember("s", "c", "environment",
                                     evidence_paths=["turn:a"], authoritative=False)
        self.assertEqual(obs.kind, "environment")
        self.assertEqual(obs.to_dict()["kind"], "environment")

    def test_authoritative_kinds_are_preference_and_constraint(self):
        self.assertEqual(set(AUTHORITATIVE_KINDS), {"preference", "constraint"})
        for k in ("environment", "procedure", "root-cause", "decision"):
            self.assertIn(k, MEM_KINDS)
            self.assertNotIn(k, AUTHORITATIVE_KINDS)


# --------------------------------------------------------------------------- #
# Memory-candidate lifecycle (session cache)
# --------------------------------------------------------------------------- #
class MemCandidateLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = fb.FeedbackState(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_idempotent(self):
        with self.st.locked() as s:
            r1 = fb.upsert_mem_candidate(s, "cid", "codex:s1", "t1", "h", 1000.0, 3600)
            r2 = fb.upsert_mem_candidate(s, "cid", "codex:s1", "t1", "h", 1000.0, 3600)
            self.assertEqual(r1, "created")
            self.assertEqual(r2, fb.MEM_PENDING)
            self.assertEqual(len(s["mem_candidates"]), 1)

    def test_no_prompt_body_stored(self):
        with self.st.locked() as s:
            fb.upsert_mem_candidate(s, "cid", "codex:s1", "t1", "hashonly", 1000.0, 3600)
            c = fb.get_mem_candidate(s, "cid")
        self.assertNotIn("prompt", c)
        self.assertEqual(c["hash"], "hashonly")

    def test_expiry_prunes(self):
        with self.st.locked() as s:
            fb.upsert_mem_candidate(s, "cid", "codex:s1", "t1", "h", 1000.0, 60)
            fb.prune_mem_candidates(s, now=1000.0 + 61, ttl=60)
            self.assertEqual(s["mem_candidates"], [])

    def test_candidate_cache_has_absolute_bound(self):
        with self.st.locked() as s:
            s["mem_candidates"] = [
                {"id": f"c{i}", "created_at": 1000.0 + i, "status": fb.MEM_PENDING}
                for i in range(fb.MAX_MEM_CANDIDATES + 5)
            ]
            fb.prune_mem_candidates(s, now=4000.0, ttl=10_000)
            self.assertEqual(len(s["mem_candidates"]), fb.MAX_MEM_CANDIDATES)
            self.assertEqual(s["mem_candidates"][0]["id"], "c5")

    def test_session_namespaced_distinct(self):
        with self.st.locked() as s:
            fb.upsert_mem_candidate(s, "a", "codex:s1", "t1", "h", 1000.0, 3600)
            fb.upsert_mem_candidate(s, "b", "claude:s1", "t1", "h", 1000.0, 3600)
            self.assertEqual(len(s["mem_candidates"]), 2)

    def test_claim_resolution_tracking(self):
        with self.st.locked() as s:
            fb.upsert_mem_candidate(s, "cid", "codex:s1", "t1", "h", 1000.0, 3600)
            c = fb.get_mem_candidate(s, "cid")
            self.assertFalse(fb.mem_claim_already_resolved(c, "fp1"))
            fb.mark_mem_claim_resolved(c, "fp1")
            self.assertTrue(fb.mem_claim_already_resolved(c, "fp1"))


# --------------------------------------------------------------------------- #
# Secret scanner
# --------------------------------------------------------------------------- #
class SecretScanTest(unittest.TestCase):
    def test_detects_known_shapes(self):
        for bad in (
            "use AKIAIOSFODNN7EXAMPLE here",
            "-----BEGIN RSA PRIVATE KEY-----\nMII...",
            "token = ghp_012345678901234567890123456789abcd",
            "password: hunter2hunter2hunter2",
            "the key is sk-abcdefghijklmnopqrstuvwxyz012345",
        ):
            self.assertIsNotNone(secretscan.find_secret(bad), bad)

    def test_passes_ordinary_prose(self):
        for ok in (
            "Run pytest -q before committing",
            "Deploys go through the deploy-pipeline wrapper",
            "The password policy requires rotation every 90 days",
            "デプロイは必ずラッパー経由で行う",
        ):
            self.assertIsNone(secretscan.find_secret(ok), ok)

    def test_reason_never_echoes_secret_value(self):
        reason = secretscan.find_secret("AKIAIOSFODNN7EXAMPLE")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", reason)

    def test_assert_raises(self):
        with self.assertRaises(secretscan.SecretDetected):
            secretscan.assert_no_secret("api_key=abcdefghijklmnop", "claim")


# --------------------------------------------------------------------------- #
# `exi memory` CLI
# --------------------------------------------------------------------------- #
class MemoryCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EXI_DATA_DIR"] = self.tmp.name
        os.environ.pop("EXI_CONFIG", None)
        self.st = fb.FeedbackState(data_dir=self.tmp.name)

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        os.environ.pop("EXI_CONFIG", None)
        self.tmp.cleanup()

    def _make_candidate(self, cid="cid", session="codex:s1", p_hash="hh", created=None):
        with self.st.locked() as s:
            fb.upsert_mem_candidate(s, cid, session, "t1", p_hash,
                                    created if created is not None else time.time(), 3600)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = exicli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_resolve_preference_active(self):
        self._make_candidate()
        rc, out, _ = self._run(["memory", "resolve", "--candidate", "cid",
                                "--kind", "preference", "--scope", "w", "--claim", "prefer tabs"])
        self.assertEqual(rc, 0)
        self.assertIn("active", out)
        self.assertTrue(Store(data_dir=self.tmp.name).retrieve("tabs", cwd="/x"))

    def test_resolve_technical_not_retrievable_yet(self):
        self._make_candidate()
        rc, out, _ = self._run(["memory", "resolve", "--candidate", "cid",
                                "--kind", "procedure", "--scope", "w",
                                "--claim", "verified build procedure xyz",
                                "--evidence", "docs/build-a.md"])
        self.assertEqual(rc, 0)
        self.assertIn("candidate", out)
        self.assertEqual(Store(data_dir=self.tmp.name).retrieve("build procedure xyz", cwd="/x"), [])

    def test_resolve_technical_requires_real_evidence(self):
        self._make_candidate()
        rc, _, err = self._run(["memory", "resolve", "--candidate", "cid",
                                "--kind", "procedure", "--scope", "w",
                                "--claim", "verified build procedure xyz"])
        self.assertEqual(rc, 2)
        self.assertIn("require at least one verified --evidence", err)
        self.assertEqual(Store(data_dir=self.tmp.name).list(), [])

    def test_resolve_technical_confirms_after_second_actual_source(self):
        self._make_candidate(cid="c1", p_hash="h1")
        rc1, _, _ = self._run([
            "memory", "resolve", "--candidate", "c1", "--kind", "procedure",
            "--scope", "w", "--claim", "verified build procedure xyz",
            "--evidence", "docs/build-a.md",
        ])
        self._make_candidate(cid="c2", p_hash="h2")
        rc2, out2, _ = self._run([
            "memory", "resolve", "--candidate", "c2", "--kind", "procedure",
            "--scope", "w", "--claim", "verified build procedure xyz",
            "--evidence", "ci/build-proof.txt",
        ])
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertIn("confirmed", out2)
        hits = Store(data_dir=self.tmp.name).retrieve("build procedure xyz", cwd="/x")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].confirmed_count, 2)

    def test_resolve_same_claim_twice_idempotent(self):
        self._make_candidate()
        self._run(["memory", "resolve", "--candidate", "cid", "--kind", "preference",
                   "--scope", "w", "--claim", "prefer tabs"])
        rc, out, _ = self._run(["memory", "resolve", "--candidate", "cid", "--kind", "preference",
                                "--scope", "w", "--claim", "prefer tabs"])
        self.assertEqual(rc, 0)
        self.assertIn("no change", out)
        obs = Store(data_dir=self.tmp.name).list()
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].confirmed_count, 1)

    def test_resolve_multiple_distinct_from_one_candidate(self):
        self._make_candidate()
        self._run(["memory", "resolve", "--candidate", "cid", "--kind", "preference",
                   "--scope", "w", "--claim", "prefer tabs"])
        self._run(["memory", "resolve", "--candidate", "cid", "--kind", "constraint",
                   "--scope", "net", "--claim", "never call metered apis"])
        self.assertEqual(len(Store(data_dir=self.tmp.name).list()), 2)

    def test_same_claim_in_two_scopes_is_not_collapsed(self):
        self._make_candidate()
        self._run(["memory", "resolve", "--candidate", "cid", "--kind", "preference",
                   "--scope", "project-a", "--claim", "prefer local tests"])
        rc, _, err = self._run([
            "memory", "resolve", "--candidate", "cid", "--kind", "preference",
            "--scope", "project-b", "--claim", "prefer local tests",
        ])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(Store(data_dir=self.tmp.name).list()), 2)

    def test_resolve_unknown_candidate(self):
        rc, _, err = self._run(["memory", "resolve", "--candidate", "nope", "--kind",
                                "preference", "--scope", "w", "--claim", "c"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown or expired", err)

    def test_resolve_expired_candidate(self):
        self._make_candidate(created=time.time() - 10_000)
        rc, _, err = self._run(["memory", "resolve", "--candidate", "cid", "--kind",
                                "preference", "--scope", "w", "--claim", "c"])
        self.assertEqual(rc, 2)
        self.assertIn("expired", err)

    def test_resolve_session_mismatch(self):
        self._make_candidate(session="codex:s1")
        rc, _, err = self._run(["memory", "resolve", "--candidate", "cid", "--kind",
                                "preference", "--scope", "w", "--claim", "c",
                                "--session", "codex:other"])
        self.assertEqual(rc, 2)
        self.assertIn("different session", err)

    def test_resolve_rejects_secret_claim(self):
        self._make_candidate()
        rc, _, err = self._run(["memory", "resolve", "--candidate", "cid", "--kind",
                                "preference", "--scope", "w",
                                "--claim", "prod key AKIAIOSFODNN7EXAMPLE"])
        self.assertEqual(rc, 2)
        self.assertIn("secret", err.lower())
        self.assertEqual(Store(data_dir=self.tmp.name).list(), [])

    def test_resolve_rejects_oversized_fields_before_secret_scan(self):
        self._make_candidate()
        hidden = "x" * (exicli.MAX_MEMORY_CLAIM_CHARS + 1) + " api_key=abcdefghijklmnop"
        rc, _, err = self._run(["memory", "resolve", "--candidate", "cid", "--kind",
                                "preference", "--scope", "w", "--claim", hidden])
        self.assertEqual(rc, 2)
        self.assertIn("claim exceeds", err)
        self.assertEqual(Store(data_dir=self.tmp.name).list(), [])

    def test_resolve_caps_distinct_memories_per_turn(self):
        self._make_candidate()
        for i in range(fb.MAX_MEMORIES_PER_CANDIDATE):
            rc, _, _ = self._run([
                "memory", "resolve", "--candidate", "cid", "--kind", "preference",
                "--scope", "w", "--claim", f"preference {i}",
            ])
            self.assertEqual(rc, 0)
        rc, _, err = self._run([
            "memory", "resolve", "--candidate", "cid", "--kind", "preference",
            "--scope", "w", "--claim", "one too many",
        ])
        self.assertEqual(rc, 2)
        self.assertIn("maximum", err)
        self.assertEqual(len(Store(data_dir=self.tmp.name).list()),
                         fb.MAX_MEMORIES_PER_CANDIDATE)

    def test_resolve_cannot_alter_existing_observation(self):
        # remember() only ever appends capture/confirm; there is no CLI path to
        # retire/supersede via `memory resolve`. Prove an existing confirmed
        # observation is untouched by an unrelated resolve.
        store = Store(data_dir=self.tmp.name)
        c = store.capture("keep", "keep me", evidence_paths=["a"])
        store.confirm(c.id, evidence_paths=["b"])
        self._make_candidate()
        self._run(["memory", "resolve", "--candidate", "cid", "--kind", "preference",
                   "--scope", "other", "--claim", "unrelated"])
        again = Store(data_dir=self.tmp.name).get(c.id)
        self.assertEqual(again.status, STATUS_CONFIRMED)
        self.assertEqual(again.claim, "keep me")

    def test_dismiss(self):
        self._make_candidate()
        rc, out, _ = self._run(["memory", "dismiss", "--candidate", "cid",
                                "--reason", "nothing durable here"])
        self.assertEqual(rc, 0)
        self.assertIn("dismissed", out)
        with open(os.path.join(self.tmp.name, "feedback-state.json"), "rb") as f:
            state_blob = f.read()
        self.assertNotIn(b"nothing durable here", state_blob)

    def test_dismissed_candidate_cannot_later_resolve(self):
        self._make_candidate()
        self._run(["memory", "dismiss", "--candidate", "cid",
                   "--reason", "nothing durable here"])
        rc, _, err = self._run(["memory", "resolve", "--candidate", "cid",
                                "--kind", "preference", "--scope", "w",
                                "--claim", "prefer tabs"])
        self.assertEqual(rc, 2)
        self.assertIn("dismissed, cannot resolve", err)
        self.assertEqual(Store(data_dir=self.tmp.name).list(), [])

    def test_resolved_candidate_cannot_be_dismissed(self):
        self._make_candidate()
        self._run(["memory", "resolve", "--candidate", "cid",
                   "--kind", "preference", "--scope", "w", "--claim", "prefer tabs"])
        rc, _, err = self._run(["memory", "dismiss", "--candidate", "cid",
                                "--reason", "changed mind"])
        self.assertEqual(rc, 2)
        self.assertIn("already resolved", err)

    def test_list_active_status_is_supported(self):
        self._make_candidate()
        self._run(["memory", "resolve", "--candidate", "cid",
                   "--kind", "preference", "--scope", "w", "--claim", "prefer tabs"])
        rc, out, err = self._run(["list", "--status", "active"])
        self.assertEqual(rc, 0, err)
        self.assertIn("prefer tabs", out)

    def test_dismiss_requires_reason(self):
        self._make_candidate()
        rc, _, err = self._run(["memory", "dismiss", "--candidate", "cid", "--reason", "  "])
        self.assertEqual(rc, 2)

    def test_candidates_list_no_bodies(self):
        self._make_candidate()
        rc, out, _ = self._run(["memory", "candidates"])
        self.assertEqual(rc, 0)
        self.assertIn("cid", out)
        self.assertIn("bodies are never stored", out)


# --------------------------------------------------------------------------- #
# Per-turn injection across all four surfaces
# --------------------------------------------------------------------------- #
class InjectionAcrossProvidersTest(unittest.TestCase):
    NORMAL = "I always want tests run with pytest -q before committing"
    SECRET = "my token is ghp_012345678901234567890123456789abcd keep it"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EXI_DATA_DIR"] = self.tmp.name
        os.environ.pop("EXI_CONFIG", None)

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        os.environ.pop("EXI_CONFIG", None)
        self.tmp.cleanup()

    def _codex(self, event, payload):
        hook_in = io.StringIO(json.dumps(payload))
        out, err = io.StringIO(), io.StringIO()
        orig = sys.stdin
        sys.stdin = hook_in
        try:
            with redirect_stdout(out), redirect_stderr(err):
                feedback_hook.handle(event)
        finally:
            sys.stdin = orig
        return out.getvalue()

    def _dispatch(self, provider, event, payload):
        cfg = config.load_config()
        err = io.StringIO()
        with redirect_stderr(err):
            return ad.dispatch(provider, event, payload, cfg)

    @staticmethod
    def _ctx_from_claude(out):
        return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_codex_injects_memory_instruction(self):
        out = self._codex("UserPromptSubmit",
                          {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        self.assertIn("Durable-memory review", self._ctx_from_claude(out))
        self.assertIn("exi memory resolve", self._ctx_from_claude(out))

    def test_claude_injects_memory_instruction(self):
        out = self._dispatch("claude", "UserPromptSubmit",
                             {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        self.assertIn("Durable-memory review", self._ctx_from_claude(out))

    def test_vscode_injects_memory_instruction(self):
        out = self._dispatch("copilot-vscode", "UserPromptSubmit",
                             {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        self.assertIn("Durable-memory review", self._ctx_from_claude(out))

    def test_copilot_cli_transformed_only_injects_and_echoes_verbatim(self):
        # Copilot CLI: only a transformedPrompt is present (no `prompt` key).
        out = self._dispatch("copilot-cli", "userPromptTransformed",
                             {"sessionId": "s1", "transformedPrompt": self.NORMAL})
        mtp = json.loads(out)["modifiedTransformedPrompt"]
        self.assertTrue(mtp.startswith(self.NORMAL))  # original verbatim first
        self.assertIn("Durable-memory review", mtp)   # then appended context

    def test_candidate_id_matches_across_hook_and_cli(self):
        # The injected candidate id must be the one `exi memory resolve` accepts.
        out = self._codex("UserPromptSubmit",
                          {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        ctx = self._ctx_from_claude(out)
        h = fbd.prompt_hash(self.NORMAL)
        cid = fbd.candidate_id(feedback_core.namespace_session("codex", "s1"), "t1", h)
        self.assertIn(cid, ctx)
        st = fb.FeedbackState(data_dir=self.tmp.name)
        with st.locked() as s:
            self.assertIsNotNone(fb.get_mem_candidate(s, cid))

    def test_cross_provider_candidate_isolation(self):
        self._codex("UserPromptSubmit", {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        self._dispatch("claude", "UserPromptSubmit",
                       {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        st = fb.FeedbackState(data_dir=self.tmp.name)
        with st.locked() as s:
            sessions = {c["session_id"] for c in s["mem_candidates"]}
        self.assertEqual(sessions, {"codex:s1", "claude:s1"})

    def test_combined_feedback_and_memory_single_doc(self):
        out = self._codex(
            "UserPromptSubmit",
            {"prompt": "二度と勝手にコミットしないで。前にも言った。",
             "session_id": "s1", "turn_id": "t1", "cwd": "/x"})
        # Exactly one JSON document.
        stripped = out.strip()
        self.assertEqual(stripped.count("\n"), 0)
        ctx = self._ctx_from_claude(out)
        self.assertIn("feedback candidate", ctx)     # feedback loop
        self.assertIn("Durable-memory review", ctx)  # memory loop

    def test_no_raw_prompt_or_secret_persisted_any_surface(self):
        payloads = [
            ("codex", "UserPromptSubmit",
             {"prompt": self.SECRET, "session_id": "s1", "turn_id": "t1"}),
            ("claude", "UserPromptSubmit",
             {"prompt": self.SECRET, "session_id": "s2", "turn_id": "t1"}),
            ("copilot-vscode", "UserPromptSubmit",
             {"prompt": self.SECRET, "session_id": "s3", "turn_id": "t1"}),
            ("copilot-cli", "userPromptTransformed",
             {"sessionId": "s4", "transformedPrompt": self.SECRET}),
        ]
        for provider, event, payload in payloads:
            if provider == "codex":
                self._codex(event, payload)
            else:
                self._dispatch(provider, event, payload)
        # Scan the ENTIRE data dir: neither the secret nor the raw prompt text
        # may appear anywhere (only a hash + metadata is persisted).
        needle = "ghp_012345678901234567890123456789abcd"
        for root, _dirs, files in os.walk(self.tmp.name):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    blob = f.read()
                self.assertNotIn(needle.encode(), blob, f"secret leaked into {name}")
                self.assertNotIn(b"keep it", blob, f"raw prompt leaked into {name}")

    def test_normal_turn_no_stop_block(self):
        # A per-turn memory candidate must NOT cause Stop to block (no loop).
        self._codex("UserPromptSubmit", {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        out = self._codex("Stop", {"session_id": "s1", "turn_id": "t1"})
        self.assertEqual(json.loads(out.strip()), {})

    def test_output_is_ascii_safe(self):
        out = self._dispatch("copilot-cli", "userPromptTransformed",
                             {"sessionId": "s1", "transformedPrompt": "デプロイの話"})
        self.assertTrue(out.isascii())

    def test_auto_capture_off_disables_instruction(self):
        cfg_path = os.path.join(self.tmp.name, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump({"memory": {"auto_capture": False}}, f)
        os.environ["EXI_CONFIG"] = cfg_path
        out = self._codex("UserPromptSubmit",
                          {"prompt": self.NORMAL, "session_id": "s1", "turn_id": "t1"})
        self.assertNotIn("Durable-memory review", out)

    def test_total_context_bounded(self):
        # Even with retrieval + rules + feedback candidate + memory instruction,
        # the combined document stays within the absolute total ceiling.
        store = Store(data_dir=self.tmp.name)
        for i in range(10):
            o = store.capture(f"s{i}", "deploy pipeline claim " + "z" * 60,
                              evidence_paths=["a"], triggers=["deploy"])
            store.confirm(o.id, evidence_paths=["b"])
        out = self._codex("UserPromptSubmit",
                          {"prompt": "deploy pipeline please", "session_id": "s1", "turn_id": "t1"})
        ctx = self._ctx_from_claude(out)
        self.assertLessEqual(len(ctx), feedback_core.ABS_MAX_TOTAL_CONTEXT_CHARS)


# --------------------------------------------------------------------------- #
# Config knobs
# --------------------------------------------------------------------------- #
class MemoryConfigTest(unittest.TestCase):
    def test_defaults_enable_autonomous_capture(self):
        cfg = config.load_config()
        self.assertTrue(fb.memory_auto_capture_enabled(cfg))
        self.assertEqual(cfg["memory"]["auto_capture"], True)

    def test_memory_auto_capture_distinct_from_feedback(self):
        cfg = {"feedback": {"auto_capture": False}, "memory": {"auto_capture": True}}
        self.assertTrue(fb.memory_auto_capture_enabled(cfg))
        self.assertFalse(fb.auto_capture_enabled(cfg))

    def test_candidate_ttl_clamped(self):
        self.assertEqual(fb.memory_candidate_ttl_seconds({"memory": {"candidate_ttl_seconds": 5}}), 60)
        self.assertEqual(
            fb.memory_candidate_ttl_seconds({"memory": {"candidate_ttl_seconds": 10**9}}),
            7 * 86_400)

    def test_normalize_claim(self):
        self.assertEqual(normalize_claim("  Prefer   Tabs  "), "prefer tabs")


if __name__ == "__main__":
    unittest.main()
