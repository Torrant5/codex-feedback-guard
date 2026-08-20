import tempfile
import time
import unittest

import conftest_paths  # noqa: F401

from exi import store as store_mod
from exi.store import ABS_MAX_MEMORY_RESULTS, Store, feature_set, relevance_score


def _confirm(store, scope, claim, triggers=None, review_after=""):
    o = store.capture(scope, claim, evidence_paths=["a"], triggers=triggers or [],
                      review_after=review_after)
    return store.confirm(o.id, evidence_paths=["b"], review_after=review_after)


class FeatureSetTest(unittest.TestCase):
    def test_english_word_tokens(self):
        self.assertIn("deploy", feature_set("Deploy the pipeline"))

    def test_cjk_unigrams_and_bigrams(self):
        feats = feature_set("配備")
        self.assertIn("配", feats)
        self.assertIn("備", feats)
        self.assertIn("配備", feats)  # adjacent bigram


class RelevanceRankingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ranks_relevant_first(self):
        _confirm(self.store, "backend/deploy", "Deploys must use the deploy-pipeline wrapper",
                 triggers=["deploy"])
        _confirm(self.store, "frontend/css", "Prefer rem units over px for spacing",
                 triggers=["css"])
        hits = self.store.retrieve("how do I deploy to prod", cwd="/x")
        self.assertTrue(hits)
        self.assertIn("deploy-pipeline", hits[0].claim)

    def test_trigger_hit_boosts(self):
        _confirm(self.store, "s1", "aaaa bbbb cccc", triggers=["kafka"])
        hits = self.store.retrieve("we use kafka here", cwd="/x")
        self.assertTrue(hits)
        self.assertEqual(hits[0].scope, "s1")

    def test_cjk_retrieval(self):
        _confirm(self.store, "infra/デプロイ", "デプロイは必ずラッパー経由で行う",
                 triggers=["デプロイ"])
        _confirm(self.store, "other", "全く関係のない情報です", triggers=[])
        hits = self.store.retrieve("デプロイの手順を教えて", cwd="/proj")
        self.assertTrue(hits)
        self.assertEqual(hits[0].scope, "infra/デプロイ")

    def test_scope_matches_cwd_boost(self):
        o = self.store.capture("backend/api", "some claim about xyz", evidence_paths=["a"])
        self.store.confirm(o.id, evidence_paths=["b"])
        # A query with no lexical overlap still surfaces via scope↔cwd match.
        score = relevance_score("unrelated words", "/home/user/backend/api/src", self.store.get(o.id))
        self.assertGreaterEqual(score, 3)

    def test_irrelevant_excluded(self):
        _confirm(self.store, "s1", "kafka streaming topology", triggers=["kafka"])
        hits = self.store.retrieve("what color should the button be", cwd="/x")
        self.assertEqual(hits, [])


class FilteringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidate_not_retrieved(self):
        # Under-evidenced candidate (1 source) must not be injected.
        self.store.capture("s", "deploy pipeline wrapper", evidence_paths=["only"])
        self.assertEqual(self.store.retrieve("deploy pipeline", cwd="/x"), [])

    def test_retired_not_retrieved(self):
        o = _confirm(self.store, "s", "deploy pipeline wrapper")
        self.store.retire(o.id)
        self.assertEqual(self.store.retrieve("deploy pipeline", cwd="/x"), [])

    def test_superseded_not_retrieved(self):
        o1 = _confirm(self.store, "s", "old deploy pipeline claim")
        o2 = self.store.capture("s", "new deploy pipeline claim", evidence_paths=["a"],
                                supersedes=o1.id)
        self.store.confirm(o2.id, evidence_paths=["b"])
        ids = {o.id for o in self.store.retrieve("deploy pipeline", cwd="/x")}
        self.assertNotIn(o1.id, ids)
        self.assertIn(o2.id, ids)

    def test_review_expired_stale_not_retrieved(self):
        past = "2000-01-01T00:00:00+0000"
        _confirm(self.store, "s", "stale deploy pipeline claim", review_after=past)
        self.assertEqual(self.store.retrieve("deploy pipeline", cwd="/x"), [])

    def test_review_future_is_current(self):
        future = "2999-01-01T00:00:00+0000"
        _confirm(self.store, "s", "fresh deploy pipeline claim", review_after=future)
        self.assertTrue(self.store.retrieve("deploy pipeline", cwd="/x"))


class BoundsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_limit_respected_and_hard_capped(self):
        for i in range(ABS_MAX_MEMORY_RESULTS + 6):
            _confirm(self.store, f"s{i}", f"deploy pipeline claim number {i}", triggers=["deploy"])
        self.assertEqual(len(self.store.retrieve("deploy pipeline", cwd="/x", limit=3)), 3)
        # Hard ceiling: asking for more than the absolute cap is clamped.
        self.assertLessEqual(
            len(self.store.retrieve("deploy pipeline", cwd="/x", limit=999)),
            ABS_MAX_MEMORY_RESULTS,
        )

    def test_empty_query_returns_empty(self):
        _confirm(self.store, "s", "deploy pipeline")
        self.assertEqual(self.store.retrieve("   ", cwd="/x"), [])

    def test_empty_store_returns_empty(self):
        self.assertEqual(self.store.retrieve("anything", cwd="/x"), [])

    def test_corrupt_log_raises_for_caller_to_fail_open(self):
        _confirm(self.store, "s", "deploy pipeline")
        with open(self.store.log_path, "a") as f:
            f.write("{broken\n")
        with self.assertRaises(Exception):
            self.store.retrieve("deploy pipeline", cwd="/x")


if __name__ == "__main__":
    unittest.main()
