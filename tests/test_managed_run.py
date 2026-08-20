import json
import multiprocessing as mp
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import conftest_paths  # noqa: F401

from exi import guard, managed_run


def _alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


@unittest.skipIf(os.name == "nt", "managed-run uses POSIX process groups")
class TerminateGroupTest(unittest.TestCase):
    def _spawn_group(self):
        p = subprocess.Popen(["sleep", "30"], start_new_session=True)
        pgid = os.getpgid(p.pid)
        # ensure it's up
        time.sleep(0.05)
        return p, pgid

    def test_terminates_owned_group(self):
        p, pgid = self._spawn_group()
        try:
            self.assertTrue(_alive(pgid))
            action = managed_run.terminate_group(pgid, grace_seconds=1.0, owned_pgids={pgid})
            self.assertIn(action, ("term", "kill"))
            time.sleep(0.1)
            self.assertFalse(_alive(pgid))
        finally:
            if _alive(pgid):
                os.killpg(pgid, signal.SIGKILL)
            p.wait(timeout=5)

    def test_refuses_non_owned_group(self):
        p, pgid = self._spawn_group()
        try:
            # not in owned set -> must refuse and leave it running
            with self.assertRaises(PermissionError):
                managed_run.terminate_group(pgid, grace_seconds=1.0, owned_pgids={999999})
            self.assertTrue(_alive(pgid), "unrelated process must NOT be killed")
        finally:
            os.killpg(pgid, signal.SIGKILL)
            p.wait(timeout=5)

    def test_refuses_own_group(self):
        my_pgid = os.getpgrp()
        with self.assertRaises(PermissionError):
            managed_run.terminate_group(my_pgid, grace_seconds=1.0, owned_pgids={my_pgid})

    def test_already_gone(self):
        p, pgid = self._spawn_group()
        os.killpg(pgid, signal.SIGKILL)
        p.wait(timeout=5)
        time.sleep(0.05)
        action = managed_run.terminate_group(pgid, grace_seconds=0.5, owned_pgids={pgid})
        self.assertEqual(action, "already-gone")


@unittest.skipIf(os.name == "nt", "managed-run uses POSIX process groups")
class RunSupervisesOwnedGroupTest(unittest.TestCase):
    """Regression: a parent shell exits immediately but leaves a background
    child alive in the same (owned) process group. `run` must keep supervising
    that pgid until the whole group is gone, not just the direct child, and
    must never touch any process group it did not launch.
    """

    def setUp(self):
        self.data_tmp = tempfile.TemporaryDirectory()
        self.cfg_tmp = tempfile.TemporaryDirectory()
        cfg_path = Path(self.cfg_tmp.name) / "config.json"
        cfg_path.write_text(json.dumps({
            "guard": {"turn_hard_minutes": 999, "weekly_24h_hard_pct": 999},
            "managed_run": {"grace_seconds": 1, "poll_seconds": 0.05},
            # point at a command that cannot exist, so quota lookup fails fast
            # instead of shelling out to the real llm-quota during the test.
            "quota": {"cmd": ["/nonexistent/exi-test-llm-quota-stub"]},
        }), encoding="utf-8")
        os.environ["EXI_DATA_DIR"] = self.data_tmp.name
        os.environ["EXI_CONFIG"] = str(cfg_path)

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        os.environ.pop("EXI_CONFIG", None)
        self.data_tmp.cleanup()
        self.cfg_tmp.cleanup()

    def test_waits_for_background_child_after_parent_shell_exits(self):
        # bash exits immediately (rc=7) but leaves `sleep 0.5` running behind
        # it in the same owned process group.
        cmd = ["bash", "-c", "sleep 0.5 & exit 7"]
        started = time.time()
        ret = managed_run.run(cmd, dry_run=False)
        elapsed = time.time() - started
        self.assertEqual(ret, 7, "must surface the direct child's own exit code")
        self.assertGreaterEqual(
            elapsed, 0.4,
            "must keep supervising the owned pgid until the background child exits too",
        )


class BreachIsPureOverGivenStateTest(unittest.TestCase):
    """`_breach` must never touch disk itself — only the state its caller
    already loaded inside `guard.locked_state()`. No EXI_DATA_DIR is set up
    here on purpose: if `_breach` tried to load state from disk, it would
    hit the real (non-isolated) guard-state.json and this test would be
    unreliable / order-dependent.
    """

    def test_breach_reads_only_the_given_state_dict(self):
        cfg = {"guard": {"turn_hard_minutes": 1, "weekly_24h_hard_pct": 999}}
        now = 1_000_000.0
        started_at = now - 120  # 2 min elapsed >= hard 1 min
        findings = managed_run._breach(cfg, {"samples": []}, started_at, now)
        self.assertTrue(any(f["code"] == "time" for f in findings))

    def test_breach_weekly_24h_from_given_samples(self):
        cfg = {"guard": {"turn_hard_minutes": 999, "weekly_24h_hard_pct": 20.0}}
        now = 1_000_000.0
        state = {"samples": [{"ts": now - 3600, "used": 10.0}, {"ts": now - 60, "used": 35.0}]}
        findings = managed_run._breach(cfg, state, now - 1, now)
        self.assertTrue(any(f["code"] == "weekly_24h" for f in findings))


def _register_n(data_dir: str, base_pgid: int, n: int) -> None:
    os.environ["EXI_DATA_DIR"] = data_dir
    from exi import managed_run as mr  # re-import in the spawned process
    for i in range(n):
        mr._register(base_pgid + i, base_pgid + i, ["true"], False)


class RegistryLockConcurrencyTest(unittest.TestCase):
    """Regression for the managed-runs.json registry lock: concurrent
    registrations from separate processes must not lose entries to a
    load/mutate/save race.
    """

    def setUp(self):
        self.data_tmp = tempfile.TemporaryDirectory()
        os.environ["EXI_DATA_DIR"] = self.data_tmp.name

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        self.data_tmp.cleanup()

    def test_concurrent_registrations_are_not_lost(self):
        n_procs, n_each = 5, 20
        procs = [
            mp.Process(target=_register_n, args=(self.data_tmp.name, 1000 + p * n_each, n_each))
            for p in range(n_procs)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
            self.assertEqual(p.exitcode, 0)
        data = managed_run._load_runs()
        self.assertEqual(len(data["runs"]), n_procs * n_each)


@unittest.skipIf(os.name == "nt", "managed-run uses POSIX process groups")
class StateCorruptEnforcementTest(unittest.TestCase):
    """Regression: a corrupt guard-state.json must fail closed on the owned
    process group only — never leave it running silently, never touch any
    unrelated PID.
    """

    def setUp(self):
        self.data_tmp = tempfile.TemporaryDirectory()
        self.cfg_tmp = tempfile.TemporaryDirectory()
        cfg_path = Path(self.cfg_tmp.name) / "config.json"
        cfg_path.write_text(json.dumps({
            "guard": {"turn_hard_minutes": 999, "weekly_24h_hard_pct": 999},
            "managed_run": {"grace_seconds": 1, "poll_seconds": 0.05},
            "quota": {"cmd": ["/nonexistent/exi-test-llm-quota-stub"], "cache_seconds": 0},
        }), encoding="utf-8")
        os.environ["EXI_DATA_DIR"] = self.data_tmp.name
        os.environ["EXI_CONFIG"] = str(cfg_path)
        guard.state_path().write_text("{not json", encoding="utf-8")

    def tearDown(self):
        os.environ.pop("EXI_DATA_DIR", None)
        os.environ.pop("EXI_CONFIG", None)
        self.data_tmp.cleanup()
        self.cfg_tmp.cleanup()

    def test_corrupt_state_terminates_owned_group_and_returns_137(self):
        ret = managed_run.run(["sleep", "5"], dry_run=False)
        self.assertEqual(ret, 137)

    def test_corrupt_state_in_dry_run_does_not_stop_the_process(self):
        started = time.time()
        ret = managed_run.run(["sleep", "0.3"], dry_run=True)
        elapsed = time.time() - started
        self.assertEqual(ret, 0, "dry-run must let the child finish on its own, not force-stop it")
        self.assertGreaterEqual(elapsed, 0.25)


if __name__ == "__main__":
    unittest.main()
