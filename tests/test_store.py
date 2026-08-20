import multiprocessing as mp
import os
import tempfile
import unittest

import conftest_paths  # noqa: F401

from exi.store import (
    STATUS_CONFIRMED,
    STATUS_CANDIDATE,
    STATUS_SUPERSEDED,
    Store,
    evidence_source_key,
)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_capture_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.store.capture("scope", "claim", evidence_paths=[])

    def test_capture_and_search(self):
        o = self.store.capture(
            "infra/job-wrapper", "GPU jobs must go through the job-wrapper script",
            evidence_paths=["AGENTS.md#L45"], triggers=["gpu-cluster", "cuda"],
        )
        self.assertEqual(o.status, STATUS_CANDIDATE)
        self.assertEqual(o.confirmed_count, 1)
        hits = self.store.search("job-wrapper")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].id, o.id)

    def test_confirmed_needs_two_independent_evidence(self):
        o = self.store.capture("s", "c", evidence_paths=["src1"])
        self.assertEqual(o.status, STATUS_CANDIDATE)
        # duplicate evidence does not count as independent
        with self.assertRaises(ValueError):
            self.store.confirm(o.id, evidence_paths=["src1"])
        o2 = self.store.confirm(o.id, evidence_paths=["src2"])
        self.assertEqual(o2.confirmed_count, 2)
        self.assertEqual(o2.status, STATUS_CONFIRMED)

    def test_promote_only_confirmed(self):
        o = self.store.capture("s", "weak claim", evidence_paths=["a"])
        # not promotable yet
        self.assertEqual(self.store.promote(), [])
        with self.assertRaises(ValueError):
            self.store.promote(o.id)
        self.store.confirm(o.id, evidence_paths=["b"])
        written = self.store.promote(o.id)
        self.assertEqual(len(written), 1)
        text = written[0].read_text()
        self.assertIn("CANDIDATE", text)
        self.assertIn("weak claim", text)
        # promotion does not touch anything outside data dir
        self.assertIn(self.tmp.name, str(written[0]))

    def test_supersede_marks_old(self):
        old = self.store.capture("s", "old", evidence_paths=["a", "b"])
        self.assertEqual(old.status, STATUS_CONFIRMED)
        new = self.store.capture("s", "new", evidence_paths=["c", "d"], supersedes=old.id)
        state = self.store.derive()
        self.assertEqual(state[old.id].status, STATUS_SUPERSEDED)
        self.assertEqual(state[new.id].supersedes, old.id)

    def test_rebuild_index_from_log(self):
        o = self.store.capture("s", "durable claim about widgets", evidence_paths=["a"])
        # nuke the index; search must rebuild from the JSONL log
        os.remove(self.store.index_path)
        hits = self.store.search("widgets")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].id, o.id)

    def test_due_for_review(self):
        self.store.capture("s", "c", evidence_paths=["a", "b"], review_after="2000-01-01T00:00:00+0900")
        due = self.store.due_for_review()
        self.assertEqual(len(due), 1)


class EvidenceSourceKeyTest(unittest.TestCase):
    def test_strips_fragment(self):
        self.assertEqual(evidence_source_key("AGENTS.md#L10"), "AGENTS.md")
        self.assertEqual(evidence_source_key("AGENTS.md#L20"), "AGENTS.md")
        self.assertEqual(evidence_source_key("https://x/y#section-2"), "https://x/y")

    def test_no_fragment_is_unchanged(self):
        self.assertEqual(evidence_source_key("AGENTS.md"), "AGENTS.md")


class EvidenceSourceIndependenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_file_two_fragments_at_capture_stays_candidate(self):
        o = self.store.capture(
            "s", "c", evidence_paths=["AGENTS.md#L10", "AGENTS.md#L20"],
        )
        self.assertEqual(o.confirmed_count, 1, "same source, different fragments == one source")
        self.assertEqual(o.status, STATUS_CANDIDATE)

    def test_confirm_same_source_different_fragment_is_rejected(self):
        o = self.store.capture("s", "c", evidence_paths=["AGENTS.md#L10"])
        with self.assertRaises(ValueError):
            self.store.confirm(o.id, evidence_paths=["AGENTS.md#L20"])
        # still a candidate: the rejected confirm must not have been recorded
        self.assertEqual(self.store.get(o.id).status, STATUS_CANDIDATE)

    def test_confirm_with_distinct_source_promotes_to_confirmed(self):
        o = self.store.capture("s", "c", evidence_paths=["AGENTS.md#L10"])
        o2 = self.store.confirm(o.id, evidence_paths=["memory/other.md#L1"])
        self.assertEqual(o2.confirmed_count, 2)
        self.assertEqual(o2.status, STATUS_CONFIRMED)

    def test_confirm_dedupes_multiple_new_paths_by_source_key(self):
        o = self.store.capture("s", "c", evidence_paths=["a"])
        o2 = self.store.confirm(o.id, evidence_paths=["b#L1", "b#L2"])
        # "b#L1" and "b#L2" are the same source -> only +1 distinct source
        self.assertEqual(o2.confirmed_count, 2)
        self.assertEqual(o2.status, STATUS_CONFIRMED)


def _confirm_evidence(data_dir: str, obs_id: str, evidence: str) -> None:
    Store(data_dir=data_dir).confirm(obs_id, evidence_paths=[evidence])


class StoreLockConcurrencyTest(unittest.TestCase):
    """Regression for the store-wide fcntl lock: concurrent confirms citing
    distinct evidence sources on the same observation must not lose updates
    to a read-modify-write race between processes.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_confirms_do_not_lose_updates(self):
        o = self.store.capture("s", "c", evidence_paths=["seed"])
        n = 8
        procs = [
            mp.Process(target=_confirm_evidence, args=(self.tmp.name, o.id, f"src{i}"))
            for i in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
            self.assertEqual(p.exitcode, 0)
        final = self.store.get(o.id)
        self.assertEqual(final.confirmed_count, n + 1)  # seed + n distinct new sources
        self.assertEqual(final.status, STATUS_CONFIRMED)
        # the JSONL log itself must not have been corrupted by interleaved writes
        events = self.store._read_events()
        self.assertEqual(len(events), 1 + n)  # 1 capture + n confirms


if __name__ == "__main__":
    unittest.main()
