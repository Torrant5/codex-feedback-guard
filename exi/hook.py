"""Codex hook entrypoints: UserPromptSubmit / PreToolUse / PreCompact.

Each reads the hook's JSON payload on stdin, updates guard state, consults the
weekly Codex quota (best-effort), evaluates thresholds, and:

* HARD breach on PreToolUse   -> hookSpecificOutput permissionDecision=deny.
* HARD breach on PreCompact / UserPromptSubmit -> official {continue:false,
  stopReason, systemMessage} stop shape (never permissionDecision there).
* SOFT breach -> emit a warning on stderr (non-blocking).
* otherwise    -> allow silently.

Turn state is keyed by (session_id, turn_id) from the hook payload, so
concurrent sessions never share turn counters; usage samples stay global.
State reads/writes go through `guard.locked_state()`, an flock'd
load-mutate-save transaction, so concurrent hook processes can't lose
updates. A corrupt state file is never silently reset: it raises and every
event fails closed (deny / stop).

Quota is read via the cached reader (`quota.read_codex_quota_cached`, TTL =
`quota.cache_seconds`, default 30s), so a tool-call-heavy turn does not spawn
`llm-quota` on every single `PreToolUse`.

Quota `unknown` never blocks on its own: time / tool-count / repeat guards stay
active regardless. Nothing from the conversation body or any secret is stored —
only counters, fingerprints (hashed), and weekly usage percentages.
"""
from __future__ import annotations

import json
import sys
import time

from . import config, guard
from .quota import read_codex_quota_cached as read_codex_quota

STOP_INSTRUCTION = (
    "STOP now. Do not call more tools. Report current state to the user: what "
    "you were doing, why the budget guard tripped, and what remains. Wait for "
    "the user before resuming."
)


def _read_payload() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _extract_tool(payload: dict):
    name = payload.get("tool_name") or payload.get("toolName") or payload.get("name") or ""
    tinput = (
        payload.get("tool_input")
        if "tool_input" in payload
        else payload.get("toolInput", payload.get("input", payload.get("arguments", {})))
    )
    return name, tinput


def _deny(reason: str) -> None:
    """Emit a PreToolUse deny decision (stdout) + visible reason (stderr)."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    print(f"[codex-guard BLOCK] {reason}", file=sys.stderr)


def _stop(reason: str) -> None:
    """Emit the official hard-stop shape (PreCompact / UserPromptSubmit). Never permissionDecision."""
    out = {"continue": False, "stopReason": reason, "systemMessage": reason}
    print(json.dumps(out, ensure_ascii=False))
    print(f"[codex-guard BLOCK] {reason}", file=sys.stderr)


def _warn(reason: str) -> None:
    print(f"[codex-guard WARN] {reason}", file=sys.stderr)


def _reason_text(findings: list) -> str:
    msgs = "; ".join(f"{f['code']}: {f['message']}" for f in findings)
    return f"Codex budget guard tripped ({msgs}). {STOP_INSTRUCTION}"


def handle(event: str, argv=None) -> int:
    cfg = config.load_config()
    now = time.time()
    payload = _read_payload()
    retention = cfg["guard"].get("sample_retention_hours", 48)
    tkey = guard.turn_key(payload)

    q = read_codex_quota(cfg)  # best-effort; q.weekly_used is None when unknown

    if event not in ("UserPromptSubmit", "PreToolUse", "PreCompact"):
        return 0  # unknown event: do nothing, never block.

    try:
        with guard.locked_state() as state:
            if event == "UserPromptSubmit":
                guard.start_turn(state, now, q.weekly_used, tkey)
                guard.record_sample(state, now, q.weekly_used, retention)
                # A fresh turn allows freely; surface only a pre-existing 24h HARD state.
                ctx = guard.compute_context(state, tkey, now, cfg)
                ctx["elapsed_minutes"] = None  # brand-new turn: ignore time here
                ctx["tool_count"] = 0
                ctx["max_fingerprint"] = 0
                ctx["turn_pct"] = None
                findings = guard.evaluate(cfg, ctx)  # only h24 can fire
            elif event == "PreToolUse":
                name, tinput = _extract_tool(payload)
                guard.record_tool(state, tkey, name, tinput)
                guard.record_sample(state, now, q.weekly_used, retention)
                ctx = guard.compute_context(state, tkey, now, cfg)
                findings = guard.evaluate(cfg, ctx)
            else:  # PreCompact
                guard.record_sample(state, now, q.weekly_used, retention)
                ctx = guard.compute_context(state, tkey, now, cfg)
                findings = guard.evaluate(cfg, ctx)
    except guard.StateCorruptError as e:
        reason = f"guard state is corrupt and cannot be trusted ({e}); failing closed. {STOP_INSTRUCTION}"
        if event == "PreToolUse":
            _deny(reason)
        else:
            _stop(reason)
        return 0

    level = guard.worst_level(findings)

    if event == "UserPromptSubmit":
        if level == guard.HARD:
            _stop(_reason_text(findings) + f" [quota: {q.reason or q.mode or 'ok'}]")
        return 0

    if event == "PreToolUse":
        if level == guard.HARD:
            suffix = "" if q.ok else f" [quota unknown: {q.reason}; time/count/repeat guards still enforced]"
            _deny(_reason_text(findings) + suffix)
            return 0
        if level == guard.WARN:
            _warn("; ".join(f["message"] for f in findings))
        return 0

    # PreCompact
    if level == guard.HARD:
        # Best-effort hard stop at the compaction boundary.
        _stop(_reason_text(findings))
    return 0
