"""managed-run: supervise ONLY processes this tool launched.

Launches a command in its own process group (setsid), records that pgid as
owned, and periodically checks elapsed time and weekly Codex burn. On a hard
breach it escalates SIGTERM -> grace -> SIGKILL against *its own process group
only*. It never signals a pgid it did not create — `terminate_group` refuses any
pgid absent from the owned set, so an unrelated PID can never be killed.

Per-iteration guard-state sampling and the breach check share a single
`guard.locked_state()` transaction (record_sample + evaluate against the same
in-memory snapshot), so it can never race a concurrent hook process nor grade
a breach against a state older than the sample it just recorded. If that
state is corrupt (`guard.StateCorruptError`), this fails closed: it terminates
the owned pgid it is supervising and returns 137 — never any unrelated PID.
`--dry-run` never terminates anything (breach or corrupt-state alike); it only
logs what it *would* do and keeps monitoring. `status` lists currently-owned
runs and whether their groups are still alive.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import config, guard
from .quota import read_codex_quota_cached as read_codex_quota


def _runs_path() -> Path:
    return config.data_dir() / "managed-runs.json"


def _load_runs() -> dict:
    p = _runs_path()
    if not p.exists():
        return {"runs": []}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"runs": []}


def _save_runs(data: dict) -> None:
    p = _runs_path()
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


@contextmanager
def _locked_runs():
    """Exclusive load-mutate-save transaction over managed-runs.json.

    Mirrors `guard.locked_state()`: holds an flock for the whole
    read-modify-write span so two `managed-run` processes registering or
    deregistering concurrently can't clobber each other's entry (a plain
    load/mutate/save has a lost-update race between the load and the save).
    """
    lock_path = _runs_path().with_suffix(".lock")
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            data = _load_runs()
            yield data
            _save_runs(data)
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def _register(pgid: int, pid: int, cmd: list, dry_run: bool) -> None:
    with _locked_runs() as data:
        data["runs"].append(
            {"pgid": pgid, "pid": pid, "cmd": cmd, "started_at": time.time(), "dry_run": dry_run}
        )


def _deregister(pgid: int) -> None:
    with _locked_runs() as data:
        data["runs"] = [r for r in data["runs"] if r.get("pgid") != pgid]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _reap_owned_leader(pgid: int) -> None:
    """Best-effort, non-blocking reap of `pgid`'s leader if it's our child.

    We always spawn the group leader ourselves (its pid == pgid), so we are
    its parent and this can only ever collect that one already-owned
    process -- never anything unrelated, since `waitpid` refuses to reap
    processes that are not our own children.

    This matters because a signalled-but-unreaped process is a zombie, and
    on Linux (unlike macOS/BSD) `killpg(pgid, 0)` still reports a zombie as
    "alive" until something reaps it. Without this, a liveness check made
    right after termination can never observe the process as gone.
    """
    try:
        os.waitpid(pgid, os.WNOHANG)
    except ChildProcessError:
        pass


def terminate_group(pgid: int, grace_seconds: float, owned_pgids: set) -> str:
    """Escalate SIGTERM -> grace -> SIGKILL against an OWNED process group only.

    Refuses any pgid not in `owned_pgids`, and refuses this process's own group
    and pgid <= 1. Returns 'term', 'kill', 'already-gone', or raises
    PermissionError for a non-owned target. Reaps the group leader itself
    (see `_reap_owned_leader`) before returning, so callers observe the true
    post-termination state instead of racing a zombie.
    """
    if pgid not in owned_pgids:
        raise PermissionError(f"refusing to signal non-owned process group {pgid}")
    if pgid <= 1 or pgid == os.getpgrp():
        raise PermissionError(f"refusing to signal protected process group {pgid}")

    if not _group_alive(pgid):
        return "already-gone"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-gone"

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        _reap_owned_leader(pgid)
        if not _group_alive(pgid):
            return "term"
        time.sleep(0.1)

    _reap_owned_leader(pgid)
    if not _group_alive(pgid):
        return "term"

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "term"

    # SIGKILL is not maskable, so the leader dies almost immediately; reap
    # it (bounded, not open-ended) so the caller's next liveness check sees
    # the true post-kill state instead of a lingering zombie.
    # Even a configured grace of zero still needs a brief reap window after
    # SIGKILL; otherwise Linux can expose the just-killed leader as a zombie
    # to the caller's immediate signal-0 liveness probe.
    kill_reap_seconds = min(max(grace_seconds, 0.1), 2.0)
    kill_deadline = time.time() + kill_reap_seconds
    while True:
        _reap_owned_leader(pgid)
        if not _group_alive(pgid):
            break
        if time.time() >= kill_deadline:
            break
        time.sleep(0.02)
    return "kill"


def _breach(cfg: dict, state: dict, started_at: float, now: float) -> list:
    """Hard-only checks relevant to a long-running managed process.

    Pure: reads only the `state` dict the caller already holds inside its own
    `guard.locked_state()` transaction. It never reloads guard-state.json
    itself, so it can never grade against a snapshot other than the one the
    sample was just recorded into in that same transaction.
    """
    g = cfg["guard"]
    findings = []
    elapsed_min = (now - started_at) / 60.0
    if elapsed_min >= g["turn_hard_minutes"]:
        findings.append(guard._f(guard.HARD, "time", f"managed run {elapsed_min:.0f} min >= hard {g['turn_hard_minutes']} min"))
    inc, n = guard.weekly_increment(state.get("samples", []), now - 24 * 3600.0, now)
    if inc is not None and inc >= g["weekly_24h_hard_pct"]:
        findings.append(guard._f(guard.HARD, "weekly_24h", f"weekly Codex usage +{inc:.1f}% in 24h >= hard {g['weekly_24h_hard_pct']}%"))
    return findings


def _reap_direct_child(proc: subprocess.Popen, timeout: float) -> None:
    """Reap the direct child after signalling its owned process group.

    `terminate_group` handles the whole process group, but `Popen.wait()` is
    still required for the direct child so it does not remain a zombie and so
    Python does not emit a ResourceWarning from `Popen.__del__`.
    """
    try:
        proc.wait(timeout=max(1.0, timeout + 1.0))
    except subprocess.TimeoutExpired:
        # The group already received SIGKILL. A final non-blocking poll keeps
        # this helper bounded; never signal anything outside the owned group.
        proc.poll()


def run(cmd: list, dry_run: bool = False) -> int:
    if not cmd:
        print("managed-run: no command given", file=sys.stderr)
        return 2
    cfg = config.load_config()
    grace = cfg["managed_run"].get("grace_seconds", 10)
    poll = cfg["managed_run"].get("poll_seconds", 5)
    retention = cfg["guard"].get("sample_retention_hours", 48)

    # Launch in a fresh session/process group so we can signal the whole tree.
    proc = subprocess.Popen(cmd, start_new_session=True, shell=False)
    pgid = os.getpgid(proc.pid)
    started_at = time.time()
    _register(pgid, proc.pid, cmd, dry_run)
    owned = {pgid}
    dry_run_warned = False
    corrupt_warned = False
    child_ret = None
    print(f"managed-run: pid={proc.pid} pgid={pgid} dry_run={dry_run} :: {' '.join(cmd)}", file=sys.stderr)

    try:
        while True:
            if child_ret is None:
                child_ret = proc.poll()
            # The direct child may exit while it left background processes
            # behind in the same (owned) process group; keep supervising
            # until the whole group is gone, not just the direct child.
            if child_ret is not None and not _group_alive(pgid):
                return child_ret
            now = time.time()
            # sample quota into shared guard history so 24h math stays warm
            q = read_codex_quota(cfg)
            try:
                with guard.locked_state() as state:
                    guard.record_sample(state, now, q.weekly_used, retention)
                    findings = _breach(cfg, state, started_at, now)
            except guard.StateCorruptError as e:
                reason = f"guard state is corrupt and cannot be trusted ({e})"
                if dry_run:
                    if not corrupt_warned:
                        print(
                            f"[managed-run DRY-RUN] {reason}; would fail closed and "
                            f"terminate pgid={pgid} (monitoring continues; not killing)",
                            file=sys.stderr,
                        )
                        corrupt_warned = True
                    time.sleep(poll)
                    continue
                print(f"[managed-run ENFORCE] {reason}; failing closed, terminating pgid={pgid}", file=sys.stderr)
                action = terminate_group(pgid, grace, owned)
                _reap_direct_child(proc, grace)
                print(f"[managed-run] pgid={pgid} -> {action}", file=sys.stderr)
                return 137

            if findings:
                reason = "; ".join(f["message"] for f in findings)
                if dry_run:
                    if not dry_run_warned:
                        print(f"[managed-run DRY-RUN] would terminate pgid={pgid}: {reason} "
                              f"(monitoring continues; not killing)", file=sys.stderr)
                        dry_run_warned = True
                else:
                    print(f"[managed-run ENFORCE] terminating pgid={pgid}: {reason}", file=sys.stderr)
                    action = terminate_group(pgid, grace, owned)
                    _reap_direct_child(proc, grace)
                    print(f"[managed-run] pgid={pgid} -> {action}", file=sys.stderr)
                    return 137 if child_ret is None else child_ret
            time.sleep(poll)
    finally:
        _deregister(pgid)


def status() -> int:
    data = _load_runs()
    runs = data.get("runs", [])
    if not runs:
        print("(no managed runs)")
        return 0
    for r in runs:
        alive = _group_alive(r["pgid"])
        age = time.time() - r.get("started_at", time.time())
        print(f"pgid={r['pgid']} pid={r['pid']} alive={alive} age={age/60:.1f}min "
              f"dry_run={r.get('dry_run')} :: {' '.join(r.get('cmd', []))}")
    return 0
