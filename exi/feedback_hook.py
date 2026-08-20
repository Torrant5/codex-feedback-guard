"""Codex adapter for the provider-neutral feedback engine.

This is one of four thin surface adapters (see also ``adapter_claude``,
``adapter_copilot_vscode``, ``adapter_copilot_cli``). Its only jobs are:

1. normalize a Codex hook payload into a :class:`feedback_core.Request`
   (namespacing the session id with the ``codex`` provider prefix so multiple
   agents that share one machine's data directory never collide), and
2. encode the neutral outcome from ``feedback_core`` into Codex's exact hook
   output contract.

All policy lives in ``feedback_core``; there is no enforcement logic here. Every
event fails OPEN on an internal error (never wedge the agent on a bug in this
file); ``Stop`` additionally always prints valid JSON. The budget guard's
separate hooks and their fail-closed behavior are untouched.

Backwards compatibility: the public entrypoint ``handle(event)`` and the
internal ``_HANDLERS`` / ``_handle_pre_tool_use`` / ``_approval_ttl`` symbols are
retained so existing installs and tests keep working unchanged.
"""
from __future__ import annotations

import json
import sys
import time

from . import config, feedback_core, feedback_detect
from .feedback_core import (  # re-exported for backwards compatibility
    ABS_MAX_INJECT_CHARS,
    ADMIN_APPROVAL_PREFIX,
    APPROVAL_PREFIX,
    Request,
)

PROVIDER = feedback_core.PROVIDER_CODEX

# Retained alias: tests and older callers reference feedback_hook._approval_ttl.
_approval_ttl = feedback_core.approval_ttl


# ---- payload helpers -------------------------------------------------------
def _read_payload() -> dict:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:  # noqa: BLE001 - undecodable stdin must fail open, not crash
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _raw_session_id(payload: dict) -> str:
    return payload.get("session_id") or payload.get("sessionId") or "unknown-session"


def _turn_id(payload: dict):
    """Native turn/prompt id, or None when Codex does not provide one.

    Returning None (not a constant like ``"unknown-turn"``) lets the core mint a
    fresh per-turn active-turn key so the Stop counter is never a single
    permanent cap shared across every turn of a session.
    """
    return (
        payload.get("turn_id")
        or payload.get("turnId")
        or payload.get("prompt_id")
        or payload.get("promptId")
        or None
    )


def _cwd(payload: dict) -> str:
    import os
    return payload.get("cwd") or payload.get("workspace") or os.getcwd()


def _request(payload: dict, cfg: dict) -> Request:
    name, tinput = feedback_core.extract_tool(payload)
    return Request(
        provider=PROVIDER,
        raw_session_id=_raw_session_id(payload),
        turn_id=_turn_id(payload),
        cwd=_cwd(payload),
        cfg=cfg,
        now=time.time(),
        prompt=feedback_detect.extract_prompt(payload),
        tool_name=name,
        tool_input=tinput,
        stop_hook_active=bool(payload.get("stop_hook_active")),
    )


# ---- output helpers (Codex contract) ---------------------------------------
def _emit_context(text: str, event: str) -> None:
    print(json.dumps(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
        ensure_ascii=False,
    ))


def _emit_deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    print(f"[feedback BLOCK] {reason}", file=sys.stderr)


def _emit_warn_context(text: str) -> None:
    _emit_context(text, "PreToolUse")
    print(f"[feedback WARN] {text}", file=sys.stderr)


def _fail_open(event: str, err: Exception) -> int:
    print(f"[feedback ERROR fail-open] {type(err).__name__}: {err}", file=sys.stderr)
    if event == "Stop":
        print("{}")  # Stop must always emit valid JSON on exit 0.
    return 0


# ---------------------------------------------------------------------------
# Event handlers (normalize -> core -> encode)
# ---------------------------------------------------------------------------
def _handle_user_prompt_submit(payload: dict, cfg: dict) -> int:
    outcome = feedback_core.user_prompt_outcome(_request(payload, cfg))
    if outcome.context:
        _emit_context(outcome.context, "UserPromptSubmit")
    return 0


def _handle_pre_tool_use(payload: dict, cfg: dict) -> int:
    outcome = feedback_core.pre_tool_outcome(_request(payload, cfg))
    if outcome.action in ("deny", "pause"):
        _emit_deny(outcome.reason)
    elif outcome.action == "warn":
        _emit_warn_context(outcome.reason)
    return 0


def _handle_post_tool_use(payload: dict, cfg: dict) -> int:
    feedback_core.post_tool_track(_request(payload, cfg))
    return 0


def _handle_stop(payload: dict, cfg: dict) -> int:
    outcome = feedback_core.stop_outcome(_request(payload, cfg))
    if outcome.action == "block":
        print(json.dumps({"decision": "block", "reason": outcome.reason}, ensure_ascii=False))
        print(f"[feedback BLOCK Stop] {outcome.reason}", file=sys.stderr)
    elif outcome.action == "capped":
        print(json.dumps({"systemMessage": outcome.system_message}, ensure_ascii=False))
        print(f"[feedback Stop cap reached] {outcome.system_message}", file=sys.stderr)
    else:
        if outcome.warn_only:
            print(f"[feedback WARN] {outcome.warn_only}", file=sys.stderr)
        print("{}")
    return 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
_HANDLERS = {
    "UserPromptSubmit": _handle_user_prompt_submit,
    "PreToolUse": _handle_pre_tool_use,
    "PostToolUse": _handle_post_tool_use,
    "Stop": _handle_stop,
}


def handle(event: str, argv=None) -> int:
    if event not in _HANDLERS:
        return 0
    payload = _read_payload()
    try:
        cfg = config.load_config()
    except Exception as e:  # noqa: BLE001 - config problems must fail open
        return _fail_open(event, e)
    try:
        return _HANDLERS[event](payload, cfg)
    except Exception as e:  # noqa: BLE001 - all feedback errors fail OPEN
        return _fail_open(event, e)
