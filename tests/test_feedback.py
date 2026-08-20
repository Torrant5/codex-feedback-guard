import multiprocessing as mp
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest_paths  # noqa: F401

from exi import feedback as fb


# --------------------------------------------------------------------------- #
# Store: count semantics, duplicate-evidence rejection, history
# --------------------------------------------------------------------------- #
class FeedbackStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = fb.FeedbackStore(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_starts_at_one(self):
        r = self.store.record("no-fallback", "Do not add hidden fallbacks", evidence="ev1")
        self.assertEqual(r.count, 1)
        self.assertTrue(r.enabled)

    def test_distinct_evidence_increments_count(self):
        self.store.record("r", "d", evidence="ev1")
        r = self.store.record("r", "d", evidence="ev2")
        self.assertEqual(r.count, 2)
        r = self.store.record("r", "d", evidence="ev3")
        self.assertEqual(r.count, 3)

    def test_duplicate_evidence_rejected_and_count_unchanged(self):
        self.store.record("r", "d", evidence="ev1")
        with self.assertRaises(ValueError):
            self.store.record("r", "d", evidence="ev1")
        self.assertEqual(self.store.get("r").count, 1)

    def test_record_requires_fields(self):
        with self.assertRaises(ValueError):
            self.store.record("", "d", evidence="e")
        with self.assertRaises(ValueError):
            self.store.record("n", "", evidence="e")
        with self.assertRaises(ValueError):
            self.store.record("n", "d", evidence="")

    def test_violation_never_increments_count(self):
        self.store.record("r", "d", evidence="ev1")
        self.store.record_violation("r", "pre_bash", "matched", session_id="s1")
        r = self.store.get("r")
        self.assertEqual(r.count, 1)
        self.assertEqual(len(r.violations), 1)
        self.assertEqual(r.violations[0]["event"], "pre_bash")

    def test_enable_disable_history(self):
        self.store.record("r", "d", evidence="ev1")
        self.store.set_enabled("r", False)
        self.assertFalse(self.store.get("r").enabled)
        self.store.set_enabled("r", True)
        self.assertTrue(self.store.get("r").enabled)

    def test_configure_persists_and_updates(self):
        self.store.record("r", "d", evidence="ev1")
        r = self.store.configure("r", {"event": "pre_bash", "when": "rm -rf"})
        self.assertEqual(len(r.specs), 1)
        r = self.store.configure("r", [{"event": "pre_bash", "when": "a"}, {"event": "pre_bash", "when": "b"}])
        self.assertEqual(len(r.specs), 2)

    def test_configure_unknown_rule(self):
        with self.assertRaises(ValueError):
            self.store.configure("nope", {"event": "pre_bash", "when": "x"})

    def test_corrupt_log_surfaces(self):
        self.store.record("r", "d", evidence="ev1")
        with open(self.store.log_path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        with self.assertRaises(fb.FeedbackDataError):
            self.store.derive()


# --------------------------------------------------------------------------- #
# Pending feedback-candidate lifecycle (disposable session-state helpers)
# --------------------------------------------------------------------------- #
class CandidateLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = fb.FeedbackState(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_is_idempotent(self):
        with self.st.locked() as state:
            r1 = fb.upsert_candidate(state, "c1", "s1", "t1", "h1", ["prohibition"], 100.0, 3600)
            r2 = fb.upsert_candidate(state, "c1", "s1", "t1", "h1", ["prohibition"], 101.0, 3600)
            self.assertEqual(r1, "created")
            self.assertEqual(r2, "pending")
            self.assertEqual(len(state["candidates"]), 1)

    def test_no_prompt_body_stored(self):
        with self.st.locked() as state:
            fb.upsert_candidate(state, "c1", "s1", "t1", "h1", ["prohibition"], 100.0, 3600)
            c = fb.get_candidate(state, "c1")
        self.assertEqual(set(c.keys()),
                         {"id", "hash", "session_id", "turn_id", "cues", "status", "created_at"})
        self.assertNotIn("prompt", c)

    def test_pending_scoped_to_session(self):
        with self.st.locked() as state:
            fb.upsert_candidate(state, "c1", "s1", "t1", "h1", [], 100.0, 3600)
            fb.upsert_candidate(state, "c2", "s2", "t1", "h2", [], 100.0, 3600)
            self.assertEqual(len(fb.pending_candidates(state, "s1", 100.0, 3600)), 1)
            self.assertEqual(len(fb.pending_candidates(state, "s2", 100.0, 3600)), 1)

    def test_expiry_prunes(self):
        with self.st.locked() as state:
            fb.upsert_candidate(state, "c1", "s1", "t1", "h1", [], 100.0, 3600)
            # 2 hours later with a 1h ttl -> pruned, no longer pending
            self.assertEqual(fb.pending_candidates(state, "s1", 100.0 + 7200, 3600), [])

    def test_resolve_and_dismiss_status(self):
        with self.st.locked() as state:
            fb.upsert_candidate(state, "c1", "s1", "t1", "h1", [], 100.0, 3600)
            fb.set_candidate_status(state, "c1", fb.CANDIDATE_RESOLVED, rule="r")
            self.assertEqual(fb.get_candidate(state, "c1")["status"], "resolved")
            self.assertEqual(fb.pending_candidates(state, "s1", 100.0, 3600), [])

    def test_abandon_pending_marks_terminal(self):
        with self.st.locked() as state:
            fb.upsert_candidate(state, "c1", "s1", "t1", "h1", [], 100.0, 3600)
            ids = fb.abandon_pending(state, "s1", 100.0, 3600)
            self.assertEqual(ids, ["c1"])
            self.assertEqual(fb.get_candidate(state, "c1")["status"], "abandoned")
            # abandoned never blocks again
            self.assertEqual(fb.pending_candidates(state, "s1", 100.0, 3600), [])


class CandidateConfigClampTest(unittest.TestCase):
    def test_ttl_clamped(self):
        self.assertEqual(fb.candidate_ttl_seconds({"feedback": {"candidate_ttl_seconds": 1}}), 60)
        self.assertEqual(fb.candidate_ttl_seconds({"feedback": {"candidate_ttl_seconds": 10**9}}),
                         7 * 86400)
        self.assertEqual(fb.candidate_ttl_seconds({"feedback": {"candidate_ttl_seconds": "x"}}), 3600)

    def test_auto_capture_default_on(self):
        self.assertTrue(fb.auto_capture_enabled({}))
        self.assertFalse(fb.auto_capture_enabled({"feedback": {"auto_capture": False}}))


# --------------------------------------------------------------------------- #
# Spec validation (declarative-only allow-list)
# --------------------------------------------------------------------------- #
class SpecValidationTest(unittest.TestCase):
    def ok(self, spec):
        return fb.validate_specs(spec)

    def bad(self, spec):
        with self.assertRaises(ValueError):
            fb.validate_specs(spec)

    def test_valid_examples(self):
        self.ok({"event": "pre_bash", "when": "curl .* \\| sh"})
        self.ok({"event": "pre_edit", "path_glob": "**/*.py"})
        self.ok({"event": "pre_edit", "absent_sibling": "test_{stem}.py"})
        self.ok({"event": "stop_check", "require_regex": "TODO", "severity": "warn"})
        self.ok([{"event": "pre_bash", "when": "a"}, {"event": "stop_check", "forbid_regex": "b"}])

    def test_unknown_event(self):
        self.bad({"event": "post_bash", "when": "x"})

    def test_unknown_key(self):
        self.bad({"event": "pre_bash", "when": "x", "run": "rm -rf /"})  # no shell/checker key allowed
        self.bad({"event": "pre_bash", "when": "x", "command": "echo"})

    def test_bad_severity(self):
        self.bad({"event": "pre_bash", "when": "x", "severity": "explode"})

    def test_bad_regex(self):
        self.bad({"event": "pre_bash", "when": "("})

    def test_missing_condition(self):
        self.bad({"event": "pre_bash"})                 # needs when/forbid_regex
        self.bad({"event": "stop_check"})               # needs a content/sibling condition
        self.bad({"event": "pre_edit"})                 # needs path_glob or condition

    def test_bad_sibling_placeholder(self):
        self.bad({"event": "pre_edit", "absent_sibling": "{whoami}.py"})

    def test_not_object(self):
        self.bad("just a string")
        self.bad([])


# --------------------------------------------------------------------------- #
# Severity resolution
# --------------------------------------------------------------------------- #
class SeverityTest(unittest.TestCase):
    def _rule(self, count):
        r = fb.Rule(name="r", description="d")
        r.count = count
        return r

    def test_auto_ladder(self):
        self.assertEqual(fb.resolve_severity(self._rule(1), {}), fb.WARN)
        self.assertEqual(fb.resolve_severity(self._rule(2), {}), fb.WARN)
        self.assertEqual(fb.resolve_severity(self._rule(3), {}), fb.PAUSE)
        self.assertEqual(fb.resolve_severity(self._rule(4), {}), fb.PAUSE)
        self.assertEqual(fb.resolve_severity(self._rule(5), {}), fb.DENY)
        self.assertEqual(fb.resolve_severity(self._rule(9), {}), fb.DENY)

    def test_explicit_overrides(self):
        self.assertEqual(fb.resolve_severity(self._rule(1), {"severity": "deny"}), fb.DENY)
        self.assertEqual(fb.resolve_severity(self._rule(9), {"severity": "warn"}), fb.WARN)


# --------------------------------------------------------------------------- #
# Glob + sibling + edit extraction
# --------------------------------------------------------------------------- #
class GlobTest(unittest.TestCase):
    def test_star_does_not_cross_slash(self):
        self.assertTrue(fb.glob_match("foo.py", "*.py"))
        self.assertFalse(fb.glob_match("a/foo.py", "*.py"))

    def test_double_star_crosses(self):
        self.assertTrue(fb.glob_match("/a/b/foo.py", "**/*.py"))
        self.assertTrue(fb.glob_match("foo.py", "**/*.py"))
        self.assertFalse(fb.glob_match("/a/b/foo.txt", "**/*.py"))

    def test_question_mark(self):
        self.assertTrue(fb.glob_match("a.c", "?.c"))
        self.assertFalse(fb.glob_match("ab.c", "?.c"))


class SiblingTest(unittest.TestCase):
    def test_expand_stem(self):
        sib = fb.expand_sibling("test_{stem}.py", Path("/a/b/foo.py"))
        self.assertEqual(sib, Path("/a/b/test_foo.py"))

    def test_expand_dir_absolute(self):
        sib = fb.expand_sibling("{dir}/tests/test_{stem}{suffix}", Path("/a/b/foo.py"))
        self.assertEqual(sib, Path("/a/b/tests/test_foo.py"))


class ExtractEditTargetsTest(unittest.TestCase):
    def test_write(self):
        t = fb.extract_edit_targets("Write", {"file_path": "/a/foo.py", "content": "x=1"})
        self.assertEqual(t, [("/a/foo.py", "x=1")])

    def test_edit(self):
        t = fb.extract_edit_targets("Edit", {"file_path": "/a/foo.py", "new_string": "y=2"})
        self.assertEqual(t, [("/a/foo.py", "y=2")])

    def test_apply_patch(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: src/a.py\n"
            "@@\n"
            "+added line\n"
            " context\n"
            "*** Add File: src/b.py\n"
            "+new file body\n"
            "*** End Patch\n"
        )
        t = fb.extract_edit_targets("apply_patch", {"input": patch})
        paths = [p for p, _ in t]
        self.assertEqual(paths, ["src/a.py", "src/b.py"])
        self.assertIn("added line", dict(t)["src/a.py"])
        self.assertIn("new file body", dict(t)["src/b.py"])

    def test_bash_is_not_an_edit(self):
        self.assertEqual(fb.extract_edit_targets("Bash", {"command": "echo hi"}), [])


# --------------------------------------------------------------------------- #
# Enforcement engine
# --------------------------------------------------------------------------- #
class EnginePreBashTest(unittest.TestCase):
    def _rule(self):
        return fb.Rule(name="r", description="use the job-wrapper script", count=1)

    def test_when_matches(self):
        spec = {"event": "pre_bash", "when": r"\btraining-job\b"}
        self.assertIsNotNone(fb.eval_pre_bash(self._rule(), spec, "python training-job main.py"))
        self.assertIsNone(fb.eval_pre_bash(self._rule(), spec, "python other.py"))

    def test_unless_exempts(self):
        spec = {"event": "pre_bash", "when": "python", "unless": "job-wrapper"}
        self.assertIsNone(fb.eval_pre_bash(self._rule(), spec, "job-wrapper run -- python x"))
        self.assertIsNotNone(fb.eval_pre_bash(self._rule(), spec, "python x"))

    def test_scope_gate(self):
        spec = {"event": "pre_bash", "when": "python", "scope": "/gpu-cluster"}
        self.assertIsNone(fb.eval_pre_bash(self._rule(), spec, "python x", cwd="/home/other"))
        self.assertIsNotNone(fb.eval_pre_bash(self._rule(), spec, "python x", cwd="/gpu-cluster/work"))


class EnginePreEditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _rule(self):
        return fb.Rule(name="r", description="ship a test with each module", count=1)

    def test_absent_sibling_fires_when_missing(self):
        spec = {"event": "pre_edit", "path_glob": "**/*.py",
                "exclude_glob": "**/test_*.py", "absent_sibling": "{dir}/test_{stem}.py"}
        target = self.root / "mod.py"
        r = fb.eval_pre_edit(self._rule(), spec, str(target), "code")
        self.assertIsNotNone(r)  # sibling missing
        (self.root / "test_mod.py").write_text("t")
        self.assertIsNone(fb.eval_pre_edit(self._rule(), spec, str(target), "code"))

    def test_test_file_itself_excluded(self):
        spec = {"event": "pre_edit", "path_glob": "**/*.py",
                "exclude_glob": "**/test_*.py", "absent_sibling": "{dir}/test_{stem}.py"}
        target = self.root / "test_mod.py"
        self.assertIsNone(fb.eval_pre_edit(self._rule(), spec, str(target), "code"))

    def test_windows_test_file_path_is_excluded(self):
        spec = {"event": "pre_edit", "path_glob": "**/*.py",
                "exclude_glob": "**/test_*.py", "absent_sibling": "{dir}/test_{stem}.py"}
        self.assertIsNone(fb.eval_pre_edit(
            self._rule(), spec, r"C:\work\repo\tests\test_mod.py", "code"))

    def test_forbid_regex_content(self):
        spec = {"event": "pre_edit", "path_glob": "**/*.py", "forbid_regex": "except:\\s*pass"}
        self.assertIsNotNone(fb.eval_pre_edit(self._rule(), spec, "/a/x.py", "try:\n  f()\nexcept: pass"))
        self.assertIsNone(fb.eval_pre_edit(self._rule(), spec, "/a/x.py", "clean = 1"))

    def test_bare_path_glob_forbids_edit(self):
        spec = {"event": "pre_edit", "path_glob": "**/AGENTS.md"}
        self.assertIsNotNone(fb.eval_pre_edit(self._rule(), spec, "/repo/AGENTS.md", "anything"))
        self.assertIsNone(fb.eval_pre_edit(self._rule(), spec, "/repo/other.md", "anything"))


class EngineStopCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _rule(self):
        return fb.Rule(name="r", description="no debug prints left behind", count=1)

    def test_forbid_regex_on_disk_content(self):
        f = self.root / "x.py"
        f.write_text("print('debug')\n")
        spec = {"event": "stop_check", "forbid_regex": "print\\("}
        self.assertIsNotNone(fb.eval_stop_check(self._rule(), spec, str(f), self.root))
        f.write_text("clean = 1\n")
        self.assertIsNone(fb.eval_stop_check(self._rule(), spec, str(f), self.root))

    def test_require_regex_missing_fires(self):
        f = self.root / "x.py"
        f.write_text("no header here\n")
        spec = {"event": "stop_check", "require_regex": "Copyright"}
        self.assertIsNotNone(fb.eval_stop_check(self._rule(), spec, str(f), self.root))

    def test_refuses_outside_root(self):
        outside = Path("/etc/hosts")
        spec = {"event": "stop_check", "forbid_regex": "."}
        self.assertIsNone(fb.eval_stop_check(self._rule(), spec, str(outside), self.root))


# --------------------------------------------------------------------------- #
# Pause-approval nonce lifecycle
# --------------------------------------------------------------------------- #
class NonceLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.state = {"sessions": {}, "approvals": [], "stop_attempts": {}}
        self.now = 1000.0
        self.ttl = 600

    def test_request_then_approve_then_consume_once(self):
        nonce = fb.request_pause(self.state, "s1", "fp1", "rule-a", self.now, self.ttl)
        self.assertTrue(nonce)
        # cannot consume before approval
        self.assertFalse(fb.consume_permit(self.state, "s1", "fp1", "rule-a", self.now, self.ttl))
        self.assertTrue(fb.approve_nonce(self.state, "s1", nonce, self.now, self.ttl))
        # one-shot consume
        self.assertTrue(fb.consume_permit(self.state, "s1", "fp1", "rule-a", self.now, self.ttl))
        self.assertFalse(fb.consume_permit(self.state, "s1", "fp1", "rule-a", self.now, self.ttl))

    def test_wrong_nonce_does_not_approve(self):
        fb.request_pause(self.state, "s1", "fp1", "rule-a", self.now, self.ttl)
        self.assertFalse(fb.approve_nonce(self.state, "s1", "deadbeef", self.now, self.ttl))

    def test_other_session_cannot_use_permit(self):
        nonce = fb.request_pause(self.state, "s1", "fp1", "rule-a", self.now, self.ttl)
        fb.approve_nonce(self.state, "s1", nonce, self.now, self.ttl)
        self.assertFalse(fb.consume_permit(self.state, "s2", "fp1", "rule-a", self.now, self.ttl))

    def test_other_fingerprint_or_rule_cannot_use_permit(self):
        nonce = fb.request_pause(self.state, "s1", "fp1", "rule-a", self.now, self.ttl)
        fb.approve_nonce(self.state, "s1", nonce, self.now, self.ttl)
        self.assertFalse(fb.consume_permit(self.state, "s1", "fpX", "rule-a", self.now, self.ttl))
        self.assertFalse(fb.consume_permit(self.state, "s1", "fp1", "rule-b", self.now, self.ttl))

    def test_ttl_expires_permit(self):
        nonce = fb.request_pause(self.state, "s1", "fp1", "rule-a", self.now, self.ttl)
        fb.approve_nonce(self.state, "s1", nonce, self.now, self.ttl)
        later = self.now + self.ttl + 1
        self.assertFalse(fb.consume_permit(self.state, "s1", "fp1", "rule-a", later, self.ttl))

    def test_approval_of_expired_pending_fails(self):
        nonce = fb.request_pause(self.state, "s1", "fp1", "rule-a", self.now, self.ttl)
        later = self.now + self.ttl + 1
        self.assertFalse(fb.approve_nonce(self.state, "s1", nonce, later, self.ttl))


# --------------------------------------------------------------------------- #
# Tracked files + stop attempts
# --------------------------------------------------------------------------- #
class TrackingTest(unittest.TestCase):
    def test_track_dedupes_and_resolves(self):
        # Paths are now resolved against the session cwd and only kept if they
        # land INSIDE it (path-safety change). Two spellings of the same
        # in-cwd file collapse to one tracked entry.
        with tempfile.TemporaryDirectory() as cwd:
            sub = Path(cwd) / "a" / "b"
            sub.mkdir(parents=True)
            target = sub / "x.py"
            target.write_text("x=1")
            state = {"sessions": {}}
            fb.track_changed_files(
                state, "s1", cwd,
                [str(sub / ".." / "b" / "x.py"), str(target), "a/b/x.py"],
            )
            files = fb.tracked_files(state, "s1")
            self.assertEqual(len(files), 1)
            self.assertEqual(Path(files[0]).parts[-3:], ("a", "b", "x.py"))

    def test_track_rejects_paths_outside_cwd(self):
        # Absolute paths outside cwd and relative escapes are never tracked.
        with tempfile.TemporaryDirectory() as cwd:
            state = {"sessions": {}}
            fb.track_changed_files(
                state, "s1", cwd,
                ["/etc/hosts", "../escape.py", "/a/b/x.py"],
            )
            self.assertEqual(fb.tracked_files(state, "s1"), [])

    def test_stop_attempt_bump(self):
        # Key is session+turn ONLY now: which rules fired is deliberately not
        # part of the key, so alternating rule sets cannot mint fresh counters.
        state = {"stop_attempts": {}}
        key = fb.stop_attempt_key("s1", "t1")
        self.assertEqual(fb.bump_stop_attempt(state, key), 1)
        self.assertEqual(fb.bump_stop_attempt(state, key), 2)
        # Same session+turn -> same key regardless of rules -> keeps counting up.
        key_again = fb.stop_attempt_key("s1", "t1")
        self.assertEqual(key_again, key)
        self.assertEqual(fb.bump_stop_attempt(state, key_again), 3)
        # A different turn is a fresh counter.
        self.assertEqual(fb.bump_stop_attempt(state, fb.stop_attempt_key("s1", "t2")), 1)


# --------------------------------------------------------------------------- #
# Concurrency: no lost updates in the append-only store
# --------------------------------------------------------------------------- #
def _record_evidence(data_dir, name, evidence):
    fb.FeedbackStore(data_dir=data_dir).record(name, "d", evidence=evidence)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = fb.FeedbackStore(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_distinct_records_no_lost_update(self):
        self.store.record("r", "d", evidence="seed")
        n = 8
        procs = [
            mp.Process(target=_record_evidence, args=(self.tmp.name, "r", f"ev{i}"))
            for i in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
            self.assertEqual(p.exitcode, 0)
        r = self.store.get("r")
        self.assertEqual(r.count, n + 1)  # seed + n distinct
        events = self.store._read_events()
        self.assertEqual(sum(1 for e in events if e.get("type") == "record"), n + 1)


# --------------------------------------------------------------------------- #
# ReDoS safety + runtime size limits
# --------------------------------------------------------------------------- #
class RegexSafetyTest(unittest.TestCase):
    def test_pathological_pattern_times_out_fast(self):
        # A catastrophic-backtracking pattern must NOT hang: it raises a
        # RegexTimeout well under the wall-clock the naive match would take.
        import time as _t
        start = _t.monotonic()
        with self.assertRaises(fb.RegexTimeout):
            fb.safe_search(r"(a+)+$", "a" * 40 + "!")
        self.assertLess(_t.monotonic() - start, 3.0)

    def test_pathological_pattern_rejected_without_sigalrm(self):
        # Exercise the Windows/off-main-thread safety path on every CI host.
        with mock.patch.object(fb, "_HAS_SIGALRM", False):
            with self.assertRaises(fb.RegexTimeout):
                fb.safe_search(r"(a+)+$", "a" * 40 + "!")

    def test_oversize_input_raises_limit(self):
        with self.assertRaises(fb.RegexLimitError):
            fb.safe_search("x", "y" * (fb.MAX_CONTENT_CHARS + 1))

    def test_oversize_pattern_raises_limit(self):
        with self.assertRaises(fb.RegexLimitError):
            fb.safe_search("z" * (fb.MAX_REGEX_PATTERN_CHARS + 1), "hello")

    def test_bounded_helper_rejects_oversize_command(self):
        spec = {"event": "pre_bash", "forbid_regex": "x"}
        with self.assertRaises(fb.RegexLimitError):
            fb.eval_pre_bash(fb.Rule(name="r", count=1), spec, "a" * (fb.MAX_COMMAND_CHARS + 1))


# --------------------------------------------------------------------------- #
# Built-in administrative-mutation matcher
# --------------------------------------------------------------------------- #
class AdminMutationMatchTest(unittest.TestCase):
    def test_matches_supported_mutations_all_forms(self):
        for cmd in (
            "exi feedback configure r --spec-json '{}'",
            "bin/exi feedback disable r",
            "/opt/proj/bin/exi feedback enable r",
            "python -m exi.exicli feedback configure r --spec-json '{}'",
            "EXI_DATA_DIR=/tmp exi feedback disable r",
        ):
            self.assertTrue(fb.matches_admin_mutation(cmd), cmd)

    def test_record_is_not_gated(self):
        self.assertFalse(fb.matches_admin_mutation("exi feedback record --name r --evidence e"))
        self.assertFalse(fb.matches_admin_mutation("exi feedback list"))
        self.assertFalse(fb.matches_admin_mutation("exi feedback show r"))

    def test_none_and_empty(self):
        self.assertFalse(fb.matches_admin_mutation(None))
        self.assertFalse(fb.matches_admin_mutation(""))


# --------------------------------------------------------------------------- #
# Administrative-approval nonce lifecycle (separate pool from pause approvals)
# --------------------------------------------------------------------------- #
class AdminNonceLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.state = {"sessions": {}, "approvals": [], "admin_approvals": [], "stop_attempts": {}}
        self.now = 1000.0
        self.ttl = 600

    def test_request_approve_consume_once(self):
        nonce = fb.request_admin_pause(self.state, "s1", "fp1", self.now, self.ttl)
        self.assertTrue(nonce)
        self.assertFalse(fb.consume_admin_permit(self.state, "s1", "fp1", self.now, self.ttl))
        self.assertTrue(fb.approve_admin_nonce(self.state, "s1", nonce, self.now, self.ttl))
        self.assertTrue(fb.consume_admin_permit(self.state, "s1", "fp1", self.now, self.ttl))
        # one-shot
        self.assertFalse(fb.consume_admin_permit(self.state, "s1", "fp1", self.now, self.ttl))

    def test_session_and_fingerprint_bound(self):
        nonce = fb.request_admin_pause(self.state, "s1", "fp1", self.now, self.ttl)
        fb.approve_admin_nonce(self.state, "s1", nonce, self.now, self.ttl)
        self.assertFalse(fb.consume_admin_permit(self.state, "s2", "fp1", self.now, self.ttl))
        self.assertFalse(fb.consume_admin_permit(self.state, "s1", "fpX", self.now, self.ttl))
        # still there for the right (session, fingerprint)
        self.assertTrue(fb.consume_admin_permit(self.state, "s1", "fp1", self.now, self.ttl))

    def test_ttl_expires(self):
        nonce = fb.request_admin_pause(self.state, "s1", "fp1", self.now, self.ttl)
        fb.approve_admin_nonce(self.state, "s1", nonce, self.now, self.ttl)
        later = self.now + self.ttl + 1
        self.assertFalse(fb.consume_admin_permit(self.state, "s1", "fp1", later, self.ttl))

    def test_pools_are_separate(self):
        # A pause nonce cannot satisfy the admin gate, and vice versa.
        pause_nonce = fb.request_pause(self.state, "s1", "fp1", "rule-a", self.now, self.ttl)
        self.assertFalse(fb.approve_admin_nonce(self.state, "s1", pause_nonce, self.now, self.ttl))
        admin_nonce = fb.request_admin_pause(self.state, "s1", "fp1", self.now, self.ttl)
        self.assertFalse(fb.approve_nonce(self.state, "s1", admin_nonce, self.now, self.ttl))


if __name__ == "__main__":
    unittest.main()
