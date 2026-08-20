"""Surface adapters for Claude Code, Copilot in VS Code, and Copilot CLI.

Each adapter (1) maps a surface event name to a neutral event, (2) normalizes
the raw payload into a provider-namespaced :class:`feedback_core.Request`, and
(3) encodes the neutral outcome into that surface's exact output contract. No
policy lives here — it is all in :mod:`feedback_core`. The Codex adapter lives
in :mod:`feedback_hook` for backwards compatibility; this module covers the
three surfaces added for multi-agent support.

Surface output contracts implemented (GitHub docs, verified 2026-08-20):

* Claude Code — PascalCase events, ``hookSpecificOutput.additionalContext`` for
  ``UserPromptSubmit``/``PreToolUse``, ``permissionDecision`` for a tool block,
  top-level ``{"decision":"block","reason":...}`` for ``Stop`` (with
  ``stop_hook_active`` honored to avoid loops).
* Copilot in VS Code — PascalCase events, snake_case payloads; same injection
  and tool-block shapes, but ``Stop`` blocks via
  ``hookSpecificOutput {hookEventName:"Stop", decision:"block", reason:...}``.
* Copilot CLI — camelCase events; injection via ``userPromptTransformed``
  returning ``modifiedTransformedPrompt`` (the original transformed prompt
  verbatim + bounded appended context, or ``{}`` when there is nothing to add,
  because the CLI drops ``userPromptSubmitted`` output); ``preToolUse`` uses
  top-level ``permissionDecision``; ``agentStop`` uses top-level ``decision``.

Nothing here persists a raw prompt or transformed prompt — they are read
transiently and echoed (CLI only), never written to disk.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import config, feedback_core, feedback_detect
from .feedback_core import (
    EV_POST_TOOL,
    EV_PRE_TOOL,
    EV_STOP,
    EV_USER_PROMPT,
    PROVIDER_CLAUDE,
    PROVIDER_COPILOT_CLI,
    PROVIDER_COPILOT_VSCODE,
    Request,
)

# Hard ceiling on the text appended to a Copilot CLI transformed prompt, over
# and above the per-section caps already applied in the core.
ABS_MAX_CLI_APPEND_CHARS = feedback_core.ABS_MAX_TOTAL_CONTEXT_CHARS


# ---- surface event -> neutral event ----------------------------------------
_CLAUDE_EVENTS = {
    "UserPromptSubmit": EV_USER_PROMPT,
    "PreToolUse": EV_PRE_TOOL,
    "PostToolUse": EV_POST_TOOL,
    "Stop": EV_STOP,
}
_VSCODE_EVENTS = dict(_CLAUDE_EVENTS)  # same PascalCase set
_CLI_EVENTS = {
    # Injection MUST use userPromptTransformed: the CLI drops the output of a
    # config-file userPromptSubmitted hook.
    "userprompttransformed": EV_USER_PROMPT,
    "pretooluse": EV_PRE_TOOL,
    "posttooluse": EV_POST_TOOL,
    "agentstop": EV_STOP,
}

_EVENT_MAPS = {
    PROVIDER_CLAUDE: _CLAUDE_EVENTS,
    PROVIDER_COPILOT_VSCODE: _VSCODE_EVENTS,
    PROVIDER_COPILOT_CLI: _CLI_EVENTS,
}


def neutral_event(provider: str, surface_event: str) -> str | None:
    m = _EVENT_MAPS.get(provider, {})
    if provider == PROVIDER_COPILOT_CLI:
        # camelCase, but accept PascalCase compatibility too.
        return m.get((surface_event or "").lower())
    return m.get(surface_event)


# ---- payload normalization -------------------------------------------------
def _raw_session_id(payload: dict) -> str:
    return payload.get("session_id") or payload.get("sessionId") or "unknown-session"


def _turn_id(payload: dict):
    return (
        payload.get("turn_id")
        or payload.get("turnId")
        or payload.get("prompt_id")
        or payload.get("promptId")
        or None
    )


def _cwd(payload: dict) -> str:
    return payload.get("cwd") or payload.get("workspace") or os.getcwd()


def _stop_hook_active(payload: dict) -> bool:
    return bool(payload.get("stop_hook_active") or payload.get("stopHookActive"))


def build_request(provider: str, payload: dict, cfg: dict) -> Request:
    name, tinput = feedback_core.extract_tool(payload)
    return Request(
        provider=provider,
        raw_session_id=_raw_session_id(payload),
        turn_id=_turn_id(payload),
        cwd=_cwd(payload),
        cfg=cfg,
        now=time.time(),
        prompt=feedback_detect.extract_prompt(payload),
        tool_name=name,
        tool_input=tinput,
        stop_hook_active=_stop_hook_active(payload),
    )


# ---- encoders (neutral outcome -> surface stdout) --------------------------
def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _encode_user_prompt(provider: str, outcome, payload: dict) -> str:
    if provider == PROVIDER_COPILOT_CLI:
        # Retain the original transformed prompt verbatim, then append bounded
        # context. If there is nothing to add, return {} (no modification).
        if not outcome.context:
            return "{}"
        base = payload.get("transformedPrompt")
        if base is None:
            base = payload.get("prompt", "")
        appended = outcome.context[:ABS_MAX_CLI_APPEND_CHARS]
        return _dumps({"modifiedTransformedPrompt": f"{base}\n\n{appended}"})
    # Claude / VS Code: additionalContext (exactly one JSON document, or none).
    if not outcome.context:
        return ""
    return _dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": outcome.context}})


def _encode_pre_tool(provider: str, outcome) -> str:
    if outcome.action in ("deny", "pause"):
        if provider == PROVIDER_COPILOT_CLI:
            return _dumps({"permissionDecision": "deny", "permissionDecisionReason": outcome.reason})
        return _dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": outcome.reason,
        }})
    if outcome.action == "warn":
        if provider == PROVIDER_COPILOT_CLI:
            # No additionalContext channel at preToolUse; surface on stderr only.
            print(f"[feedback WARN] {outcome.reason}", file=sys.stderr)
            return "{}"
        print(f"[feedback WARN] {outcome.reason}", file=sys.stderr)
        return _dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": outcome.reason}})
    # allow
    return "{}" if provider == PROVIDER_COPILOT_CLI else ""


def _encode_post_tool(provider: str) -> str:
    return "{}" if provider == PROVIDER_COPILOT_CLI else ""


def _encode_stop(provider: str, outcome) -> str:
    if outcome.action == "block":
        if provider == PROVIDER_COPILOT_VSCODE:
            return _dumps({"hookSpecificOutput": {
                "hookEventName": "Stop", "decision": "block", "reason": outcome.reason}})
        # Claude and Copilot CLI use a top-level decision/reason.
        return _dumps({"decision": "block", "reason": outcome.reason})
    if outcome.action == "capped":
        if provider == PROVIDER_CLAUDE:
            return _dumps({"systemMessage": outcome.system_message})
        print(f"[feedback Stop cap reached] {outcome.system_message}", file=sys.stderr)
        return "{}"
    # allow
    if outcome.warn_only:
        print(f"[feedback WARN] {outcome.warn_only}", file=sys.stderr)
    return "{}"


def _safe_empty(provider: str, neutral: str | None) -> str:
    """The fail-open output for a surface+event: valid JSON that changes nothing."""
    if neutral == EV_STOP:
        return "{}"
    if provider == PROVIDER_COPILOT_CLI:
        return "{}"
    return ""


# ---- dispatch --------------------------------------------------------------
def dispatch(provider: str, surface_event: str, payload: dict, cfg: dict) -> str:
    neutral = neutral_event(provider, surface_event)
    if neutral is None:
        return ""  # unknown event: never block, emit nothing
    try:
        req = build_request(provider, payload, cfg)
        if neutral == EV_USER_PROMPT:
            return _encode_user_prompt(provider, feedback_core.user_prompt_outcome(req), payload)
        if neutral == EV_PRE_TOOL:
            return _encode_pre_tool(provider, feedback_core.pre_tool_outcome(req))
        if neutral == EV_POST_TOOL:
            feedback_core.post_tool_track(req)
            return _encode_post_tool(provider)
        if neutral == EV_STOP:
            return _encode_stop(provider, feedback_core.stop_outcome(req))
    except Exception as e:  # noqa: BLE001 - every surface fails OPEN
        print(f"[feedback ERROR fail-open] {type(e).__name__}: {e}", file=sys.stderr)
        return _safe_empty(provider, neutral)
    return _safe_empty(provider, neutral)


def run(provider: str, surface_event: str) -> int:
    """Read a payload on stdin, dispatch, and print the encoded output (if any)."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception:  # noqa: BLE001 - undecodable stdin must fail open, not crash
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        cfg = config.load_config()
    except Exception as e:  # noqa: BLE001 - config problems fail open
        print(f"[feedback ERROR fail-open] {type(e).__name__}: {e}", file=sys.stderr)
        cfg = {"feedback": {}, "memory": {}}
    out = dispatch(provider, surface_event, payload, cfg)
    if out:
        print(out)
    return 0
