"""Safe, idempotent merge of the guard's hooks into an existing Codex hooks.json.

Adds UserPromptSubmit / PreToolUse / PreCompact entries pointing at codex-guard
while preserving every existing hook (notably any pre-existing `Stop` hook from
another tool) and its existing data unchanged: `merge_hooks` is a pure (dict in / dict out) deep-copy
+ append — every pre-existing key/value is carried over as-is, only new guard
entries are appended, and re-running is idempotent (no duplicate entries). The
merged file is re-serialized (`json.dumps(..., indent=2)`), so it is not
literally byte-identical to the original file's on-disk formatting, but no
existing hook data is lost, reordered in meaning, or altered.

`install` backs up the pre-existing hooks.json to `<path>.bak` exactly once —
on the very first install, before the first overwrite — and never touches
that backup again on subsequent installs, so it always reflects the original,
pre-guard file (a second/later install does NOT refresh the backup). Both the
backup and the merged file are written atomically (same-dir temp file +
`os.replace`) to avoid a torn write if interrupted mid-write.
"""
from __future__ import annotations

import copy
import json
import os
import shlex
import stat
import sys
from pathlib import Path

from . import config

GUARD_EVENTS = ["UserPromptSubmit", "PreToolUse", "PreCompact"]
# Feedback enforcement hooks (co-exist with the budget-guard hooks above and
# with any pre-existing hook installed by another tool).
FEEDBACK_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]


def guard_bin() -> str:
    """Default hook command, already shell-quoted and ready for `hook <EVENT>` to be appended.

    Prefers the source-checkout `bin/codex-guard` launcher (present for an
    editable install or when running from a checkout) since it needs no
    interpreter selection. Falls back to `<the active interpreter> -m
    exi.guardcli`, which keeps resolving correctly after a normal
    (non-editable) wheel install, where `bin/` is not shipped inside
    site-packages and `config.ROOT / "bin" / "codex-guard"` would otherwise
    become a nonexistent path.
    """
    script = config.ROOT / "bin" / "codex-guard"
    if script.exists():
        return shlex.quote(str(script))
    return " ".join(shlex.quote(part) for part in (sys.executable, "-m", "exi.guardcli"))


def _hook_command(event: str) -> str:
    return f"{guard_bin()} hook {event}"


def _atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write bytes atomically and retain the target/source permission mode."""
    if mode is None and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    try:
        tmp.write_bytes(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def _ensure_command(hooks: dict, event: str, command: str) -> None:
    """Idempotently append an exact command into the matcher="" group for event.

    Preserves every pre-existing entry in that group (e.g. a Stop hook installed by another tool)
    and appends `command` only if that EXACT command string is not already
    present — so re-running never duplicates, and the guard vs feedback commands
    (which share an event name) never shadow each other.
    """
    groups = hooks.setdefault(event, [])
    target = None
    for g in groups:
        if g.get("matcher", "") == "":
            target = g
            break
    if target is None:
        target = {"matcher": "", "hooks": []}
        groups.append(target)
    entries = target.setdefault("hooks", [])
    already = any(
        isinstance(h, dict) and h.get("command", "") == command for h in entries
    )
    if not already:
        entries.append({"type": "command", "command": command})


def merge_hooks(existing: dict, bin_path: str | None = None) -> dict:
    """Return a new hooks dict with guard + feedback hooks merged in.

    Every pre-existing hook object is carried over unchanged; only the guard's
    three events and the feedback engine's four events are appended (each once).
    Idempotent.
    """
    prefix = shlex.quote(bin_path) if bin_path else guard_bin()
    result = copy.deepcopy(existing) if existing else {}
    hooks = result.setdefault("hooks", {})
    for event in GUARD_EVENTS:
        _ensure_command(hooks, event, f"{prefix} hook {event}")
    for event in FEEDBACK_EVENTS:
        _ensure_command(hooks, event, f"{prefix} feedback-hook {event}")
    return result


def install(path: Path | None = None, dry_run: bool = False) -> dict:
    path = Path(path) if path else Path.home() / ".codex" / "hooks.json"
    existing = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    merged = merge_hooks(existing)
    if dry_run:
        return {"path": str(path), "dry_run": True, "result": merged, "existing": existing}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            # Preserve the original bytes and permission mode, not only its
            # parsed JSON structure.
            original_mode = stat.S_IMODE(path.stat().st_mode)
            _atomic_write_bytes(backup, path.read_bytes(), mode=original_mode)
    _atomic_write(path, json.dumps(merged, ensure_ascii=False, indent=2))
    return {"path": str(path), "dry_run": False, "result": merged, "existing": existing}
