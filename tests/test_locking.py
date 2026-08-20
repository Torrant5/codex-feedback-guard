"""Cross-platform lock helper: POSIX + simulated-Windows backend, mutual
exclusion, safe release/close, and no import-time fcntl anywhere at runtime."""
import os
import tempfile
import threading
import unittest
import warnings
from pathlib import Path

import conftest_paths  # noqa: F401

from exi import locking


class FakeMsvcrt:
    """Minimal stand-in for the real ``msvcrt`` so the Windows backend's
    acquire/release/retry logic can be exercised on a POSIX CI runner."""
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, fail_times=0):
        self.calls = []
        self._fail = fail_times
        self.is_locked = False

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK:
            if self._fail > 0:
                self._fail -= 1
                raise OSError(36, "resource deadlock avoided")
            self.is_locked = True
        elif mode == self.LK_UNLCK:
            self.is_locked = False


class NoImportTimeFcntlTest(unittest.TestCase):
    def test_runtime_modules_have_no_module_level_fcntl(self):
        # These import cleanly on Windows only because none of them imports
        # fcntl at module top any more (it would ImportError on Windows).
        from exi import feedback, guard, managed_run, quota, store
        for mod in (store, feedback, guard, quota, managed_run):
            self.assertFalse(hasattr(mod, "fcntl"),
                             f"{mod.__name__} still has a module-level fcntl import")


class PosixBackendTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX flock backend")
    def test_acquire_release_real_file(self):
        backend = locking.PosixLockBackend()
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "x.lock"
            with locking.file_lock_with(backend, lock) as f:
                self.assertTrue(lock.exists())
                self.assertFalse(f.closed)
            self.assertTrue(f.closed)  # handle closed on exit (no ResourceWarning)


class WindowsBackendSimTest(unittest.TestCase):
    def test_locks_single_byte_at_offset_zero_and_releases(self):
        fake = FakeMsvcrt()
        backend = locking.WindowsLockBackend(msvcrt_mod=fake, sleep=lambda s: None)
        with tempfile.TemporaryDirectory() as d:
            with locking.file_lock_with(backend, Path(d) / "w.lock") as f:
                # A single byte was locked at offset 0 while held.
                self.assertIn((fake.LK_NBLCK, 1), fake.calls)
                self.assertTrue(fake.is_locked)
                self.assertEqual(f.tell(), 0)
            self.assertFalse(fake.is_locked)  # released on exit
            self.assertEqual(fake.calls[-1], (fake.LK_UNLCK, 1))

    def test_retries_until_lock_acquired(self):
        fake = FakeMsvcrt(fail_times=3)
        sleeps = []
        backend = locking.WindowsLockBackend(msvcrt_mod=fake, sleep=sleeps.append)
        with tempfile.TemporaryDirectory() as d:
            with locking.file_lock_with(backend, Path(d) / "w.lock"):
                pass
        # 3 failures + 1 success = 4 acquire attempts; slept between failures.
        acquire_calls = [c for c in fake.calls if c[0] == fake.LK_NBLCK]
        self.assertEqual(len(acquire_calls), 4)
        self.assertEqual(len(sleeps), 3)

    def test_gives_up_after_deadline(self):
        fake = FakeMsvcrt(fail_times=10_000)
        backend = locking.WindowsLockBackend(
            msvcrt_mod=fake, sleep=lambda s: None, max_wait=0.0,
            monotonic=iter([0.0, 1.0]).__next__,
        )
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(OSError):
                with locking.file_lock_with(backend, Path(d) / "w.lock"):
                    pass


class MutualExclusionTest(unittest.TestCase):
    def test_threads_serialize_through_file_lock(self):
        # Real backend: two threads each take the lock around a read-modify-write
        # of a shared file; without exclusion the final count would be < N.
        with tempfile.TemporaryDirectory() as d:
            counter = Path(d) / "count"
            counter.write_text("0")
            lock = Path(d) / "count.lock"
            n_per_thread, n_threads = 50, 4

            def worker():
                for _ in range(n_per_thread):
                    with locking.file_lock(lock):
                        v = int(counter.read_text())
                        counter.write_text(str(v + 1))

            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(int(counter.read_text()), n_per_thread * n_threads)

    def test_file_lock_leaves_no_open_handles(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            with tempfile.TemporaryDirectory() as d:
                for _ in range(5):
                    with locking.file_lock(Path(d) / "h.lock"):
                        pass


if __name__ == "__main__":
    unittest.main()
