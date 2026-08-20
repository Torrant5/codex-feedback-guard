"""Concurrent writes through the shared lock stay consistent — the property
that matters when several agent sessions share one machine's data dir."""
import os
import tempfile
import threading
import unittest

import conftest_paths  # noqa: F401

from exi import feedback as fb
from exi.store import Store


class ConcurrentStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_tracked_file_writes_do_not_lose_updates(self):
        # N threads each track a distinct file under a distinct session; every
        # write must survive (no lost update from an interleaved load/save).
        n = 40

        def worker(i):
            st = fb.FeedbackState(data_dir=self.tmp.name)
            with st.locked() as state:
                fb.track_changed_files(state, f"sess-{i}", self.tmp.name,
                                       [os.path.join(self.tmp.name, f"f{i}.py")])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        st = fb.FeedbackState(data_dir=self.tmp.name)
        with st.locked() as state:
            self.assertEqual(len(state["sessions"]), n)

    def test_concurrent_store_captures_all_land(self):
        n = 30
        store = Store(data_dir=self.tmp.name)

        def worker(i):
            Store(data_dir=self.tmp.name).capture(
                f"scope-{i}", f"claim number {i}", evidence_paths=[f"ev-{i}"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(store.list()), n)


if __name__ == "__main__":
    unittest.main()
