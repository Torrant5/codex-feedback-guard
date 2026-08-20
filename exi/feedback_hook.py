"""Codex hook entrypoints for feedback enforcement.

Four events, each fail-OPEN on any internal error (the point is to help, never
to wedge the agent on a bug in this file):

* UserPromptSubmit -- inject high-count rules as developer context AND recognize
  an exact ``ALLOW_FEEDBACK:<nonce>`` reply that approves a paused tool call.
  Only this event can approve a pause: the model cannot forge a user prompt.
* PreToolUse       -- match pre_bash / pre_edit specs and warn / pause / deny.
* PostToolUse      -- track which files this session changed (edit tools only;
  never guesses what Bash wrote), so Stop can check them.
* Stop             -- run stop_check specs over the tracked files; block up to
  a fixed number of times, then hand control back with a manual-confirm note so
  it can never loop forever. Stop always prints valid JSON on stdout.

This module NEVER runs a shell command or arbitrary checker on the user's
behalf; it only evaluates the declarative conditions in ``feedback.py``. The
budget guard's separate hooks and their fail-closed behavior are untouched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

from . import config, feedback, guard

# Approval markers. A user reply is only ever an approval when its ENTIRE
# stripped text matches one of these exactly (16 lowercase hex nonce); any
# prefix/suffix or stray text fails to approve. See `_handle_user_prompt_submit`.
APPROVAL_PREFIX = "ALLOW_FEEDBACK:"
ADMIN_APPROVAL_PREFIX = "ALLOW_FEEDBACK_ADMIN:"
_APPROVAL_RE = re.compile(r"^ALLOW_FEEDBACK:([0-9a-f]{16})$")
_ADMIN_APPROVAL_RE = re.compile(r"^ALLOW_FEEDBACK_ADMIN:([0-9a-f]{16})$")

# Absolute ceiling on injected developer context, enforced no matter what
# `feedback.inject_max_chars` says (config can only make it SMALLER).
ABS_MAX_INJECT_CHARS = 3000


# ---- payload helpers -------------------------------------------------------
def _read_payload() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _session_id(payload: dict) -> str:
    return payload.get("session_id") or payload.get("sessionId") or "unknown-session"


def _turn_id(payload: dict) -> str:
    return (
        payload.get("turn_id")
        or payload.get("turnId")
        or payload.get("prompt_id")
        or payload.get("promptId")
        or "unknown-turn"
    )


def _cwd(payload: dict) -> str:
    return payload.get("cwd") or payload.get("workspace") or os.getcwd()


def _extract_tool(payload: dict):
    name = payload.get("tool_name") or payload.get("toolName") or payload.get("name") or ""
    tinput = (
        payload.get("tool_input")
        if "tool_input" in payload
        else payload.get("toolInput", payload.get("input", payload.get("arguments", {})))
    )
    return name, tinput


def _command_of(tool_name: str, tinput) -> str | None:
    if "bash" not in (tool_name or "").lower() and "shell" not in (tool_name or "").lower():
        return None
    if isinstance(tinput, str):
        return tinput
    if isinstance(tinput, dict):
        cmd = tinput.get("command") or tinput.get("cmd")
        if isinstance(cmd, list):
            return " ".join(str(x) for x in cmd)
        return str(cmd) if cmd is not None else ""
    return ""


# ---- output helpers --------------------------------------------------------
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
    # A warning must not block the tool: surface it as extra context + stderr.
    _emit_context(text, "PreToolUse")
    print(f"[feedback WARN] {text}", file=sys.stderr)


def _fail_open(event: str, err: Exception) -> int:
    msg = f"[feedback ERROR fail-open] {type(err).__name__}: {err}"
    print(msg, file=sys.stderr)
    if event == "Stop":
        print("{}")  # Stop must always emit valid JSON on exit 0.
    return 0


def _warn_if_corrupt(state: dict) -> None:
    if state.get("_corrupt"):
        print("[feedback WARN] session cache was unreadable; starting fresh", file=sys.stderr)


# ---- config clamps (a hostile/typo'd config must not weaken the guards) -----
def _approval_ttl(fcfg: dict) -> int:
    """Approval/permit TTL in seconds, clamped to a sane positive window.

    A zero/negative TTL would make every freshly minted nonce already-expired
    (or, worse, never prune) — clamp to at least 1s and at most one day.
    """
    try:
        ttl = int(fcfg.get("approval_ttl_seconds", 600))
    except (TypeError, ValueError):
        ttl = 600
    return max(1, min(ttl, 86_400))


def _inject_min_count(fcfg: dict) -> int:
    """Minimum rule count to inject, clamped to at least 1 (never inject noise)."""
    try:
        n = int(fcfg.get("inject_min_count", 3))
    except (TypeError, ValueError):
        n = 3
    return max(1, n)


def _inject_max_chars(fcfg: dict) -> int:
    """Injection budget, clamped to (0, ABS_MAX_INJECT_CHARS]. Config can only
    make the cap SMALLER than the absolute 3000-char ceiling, never larger."""
    try:
        n = int(fcfg.get("inject_max_chars", ABS_MAX_INJECT_CHARS))
    except (TypeError, ValueError):
        n = ABS_MAX_INJECT_CHARS
    return max(1, min(n, ABS_MAX_INJECT_CHARS))


def _stop_max_blocks(fcfg: dict) -> int:
    """Stop-loop block cap, HARD-clamped to 0..HARD_MAX_STOP_BLOCKS regardless
    of config, so no configuration can raise it above the absolute ceiling."""
    try:
        n = int(fcfg.get("stop_max_blocks", feedback.HARD_MAX_STOP_BLOCKS))
    except (TypeError, ValueError):
        n = feedback.HARD_MAX_STOP_BLOCKS
    return max(0, min(n, feedback.HARD_MAX_STOP_BLOCKS))


def _scope_matches_cwd(rule: "feedback.Rule", cwd: str) -> bool:
    """A rule with a non-empty `scope` is only injected when that scope is a
    substring of the payload cwd; an empty scope always matches."""
    scope = getattr(rule, "scope", "") or ""
    if not scope:
        return True
    return scope in (cwd or "")


# ---- enabled-rule loading --------------------------------------------------
def _enabled_rules(store: feedback.FeedbackStore) -> list:
    rules = [r for r in store.derive().values() if r.enabled]
    return rules


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------
def _handle_user_prompt_submit(payload: dict, cfg: dict) -> int:
    store = feedback.FeedbackStore()
    fcfg = cfg.get("feedback", {})
    ttl = _approval_ttl(fcfg)
    now = time.time()
    session_id = _session_id(payload)
    prompt = (payload.get("prompt") or payload.get("user_prompt") or "").strip()

    # 1) Administrative-gate approval. The ENTIRE stripped prompt must match
    #    exactly — the model cannot forge a user prompt, and a prefix/suffix
    #    around the marker does not approve. Checked before the normal marker
    #    because it shares the ALLOW_FEEDBACK stem.
    m_admin = _ADMIN_APPROVAL_RE.match(prompt)
    if m_admin:
        nonce = m_admin.group(1)
        st = feedback.FeedbackState()
        approved = False
        with st.locked() as state:
            _warn_if_corrupt(state)
            approved = feedback.approve_admin_nonce(state, session_id, nonce, now, ttl)
        if approved:
            _emit_context(
                f"Feedback administration approved for this session (nonce {nonce}). "
                "The next identical `exi feedback configure|disable|enable` call will "
                "be permitted exactly once.",
                "UserPromptSubmit",
            )
        else:
            print(f"[feedback WARN] no pending admin gate matched nonce {nonce!r}", file=sys.stderr)
        return 0

    # 2) Normal pause approval. Again the ENTIRE stripped prompt must match.
    m = _APPROVAL_RE.match(prompt)
    if m:
        nonce = m.group(1)
        st = feedback.FeedbackState()
        approved = False
        with st.locked() as state:
            _warn_if_corrupt(state)
            approved = feedback.approve_nonce(state, session_id, nonce, now, ttl)
        if approved:
            _emit_context(
                f"Feedback pause approved for this session (nonce {nonce}). The next "
                "identical tool call will be permitted exactly once.",
                "UserPromptSubmit",
            )
        else:
            print(f"[feedback WARN] no pending pause matched nonce {nonce!r}", file=sys.stderr)
        return 0

    # A near-miss marker (right stem, wrong/extra text) must NOT approve and
    # must NOT be treated as an ordinary prompt to inject into.
    if prompt.startswith(ADMIN_APPROVAL_PREFIX) or prompt.startswith(APPROVAL_PREFIX):
        print("[feedback WARN] approval marker did not match exactly; ignoring", file=sys.stderr)
        return 0

    # 3) Inject high-count, in-scope rules as developer context.
    cwd = _cwd(payload)
    min_count = _inject_min_count(fcfg)
    rules = [
        r for r in _enabled_rules(store)
        if r.count >= min_count and _scope_matches_cwd(r, cwd)
    ]
    if not rules:
        return 0
    rules.sort(key=lambda r: (-r.count, r.name))
    text = _build_injection(rules, _inject_max_chars(fcfg))
    if text:
        # Belt-and-suspenders: never emit more than the absolute ceiling even if
        # `_build_injection` degenerately overruns on a single oversized entry.
        if len(text) > ABS_MAX_INJECT_CHARS:
            text = text[:ABS_MAX_INJECT_CHARS]
        _emit_context(text, "UserPromptSubmit")
    return 0


def _rule_severity_label(rule: feedback.Rule) -> str:
    if rule.count >= feedback.AUTO_DENY_AT:
        return "deny"
    if rule.count >= feedback.AUTO_PAUSE_AT:
        return "pause"
    return "warn"


def _full_entry(rule: feedback.Rule) -> str:
    lines = [f"- [{rule.name}] (asked {rule.count}x, default {_rule_severity_label(rule)}): {rule.description}"]
    if rule.why:
        lines.append(f"    Why: {rule.why}")
    if rule.how_to_apply:
        lines.append(f"    How to apply: {rule.how_to_apply}")
    if rule.excuse:
        lines.append(f"    Not an excuse: {rule.excuse}")
    return "\n".join(lines)


def _brief_entry(rule: feedback.Rule) -> str:
    return f"- [{rule.name}] (asked {rule.count}x): {rule.description}"


def _build_injection(rules: list, max_chars: int) -> str:
    header = "Recurring user feedback — apply these without being asked again:\n"
    # Full form for every rule first; brief-ify from the lowest count if over budget.
    forms = {r.name: _full_entry(r) for r in rules}

    def assemble() -> str:
        return header + "\n".join(forms[r.name] for r in rules)

    body = assemble()
    if len(body) <= max_chars:
        return body

    # Convert lowest-count rules to brief form (rules are count-desc, so iterate
    # from the end).
    for r in reversed(rules):
        forms[r.name] = _brief_entry(r)
        if len(assemble()) <= max_chars:
            return assemble()

    # Still over budget: drop lowest-count rules and note the omission.
    kept = list(rules)
    while kept:
        kept.pop()  # drop the last (lowest count)
        note = f"\n(+{len(rules) - len(kept)} lower-count rule(s) omitted for space)"
        candidate = header + "\n".join(forms[r.name] for r in kept) + note
        if len(candidate) <= max_chars and kept:
            return candidate

    # Degenerate: even one entry overflows — hard-cut at a line boundary.
    cut = assemble()[:max_chars]
    nl = cut.rfind("\n")
    return cut[:nl] if nl > len(header) else cut


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------
def _collect_pretool_matches(rules: list, tool_name: str, tinput, cwd: str) -> list:
    """Return [(rule, spec, reason)] for pre_bash / pre_edit specs that fire."""
    matches = []
    command = _command_of(tool_name, tinput)
    edit_targets = feedback.extract_edit_targets(tool_name, tinput)
    for rule in rules:
        for spec in rule.specs:
            ev = spec.get("event")
            if ev == "pre_bash" and command is not None:
                reason = feedback.eval_pre_bash(rule, spec, command, cwd)
                if reason:
                    matches.append((rule, spec, reason))
            elif ev == "pre_edit" and edit_targets:
                for path, content in edit_targets:
                    reason = feedback.eval_pre_edit(rule, spec, path, content, cwd)
                    if reason:
                        matches.append((rule, spec, reason))
    return matches


def _peek_permit(state: dict, session_id: str, fingerprint: str, rule_name: str, now: float, ttl: int) -> bool:
    for a in state.get("approvals", []):
        if (
            a["session_id"] == session_id
            and a["fingerprint"] == fingerprint
            and a["rule"] == rule_name
            and a.get("status") == "approved"
            and now - a.get("created_at", 0) <= ttl
        ):
            return True
    return False


def _record_pretool_violations(store: feedback.FeedbackStore, matches: list, session_id: str) -> None:
    """Append at most ONE violation event per matched rule for this tool call.

    Recording a violation never changes ``count`` (see ``FeedbackStore``); it is
    an audit trail only. Multiple specs of the same rule matching one call yield
    a single event. Audit writes must never wedge the hook, so failures are
    surfaced on stderr and swallowed.
    """
    seen = set()
    for rule, spec, reason in matches:
        if rule.name in seen:
            continue
        seen.add(rule.name)
        try:
            store.record_violation(rule.name, spec.get("event", "pre_tool"), reason, session_id)
        except Exception as e:  # noqa: BLE001 - audit write must not block the tool
            print(f"[feedback WARN] could not record violation: {e}", file=sys.stderr)


def _handle_pre_tool_use(payload: dict, cfg: dict) -> int:
    store = feedback.FeedbackStore()
    rules = _enabled_rules(store)
    tool_name, tinput = _extract_tool(payload)
    cwd = _cwd(payload)
    session_id = _session_id(payload)
    matches = _collect_pretool_matches(rules, tool_name, tinput, cwd)

    if matches:
        # Audit every matched rule once (warn/pause/deny), count untouched.
        _record_pretool_violations(store, matches, session_id)

        # Bucket by resolved severity.
        denies, pauses, warns = [], [], []
        for rule, spec, reason in matches:
            sev = feedback.resolve_severity(rule, spec)
            if sev == feedback.DENY:
                denies.append((rule, reason))
            elif sev == feedback.PAUSE:
                pauses.append((rule, reason))
            else:
                warns.append((rule, reason))

        # Hard deny is evaluated FIRST and is never bypassable — not by a pause
        # nonce and not by an administrative permit (checked only afterward).
        if denies:
            body = "; ".join(f"{r.name}: {msg}" for r, msg in denies)
            _emit_deny(
                f"Blocked by recurring feedback (deny): {body}. This is a hard block "
                "(the user has raised it enough times); fix the underlying issue, do "
                "not retry the same call."
            )
            return 0
    else:
        pauses, warns = [], []

    # Built-in administrative gate for management CLI mutations. Runs AFTER hard
    # deny so it can never let a hard-denied command through, and independently
    # of user rules (it is not itself a rule and writes no rule violation).
    admin_rc = _handle_admin_gate(payload, cfg, tool_name, tinput)
    if admin_rc is not None:
        return admin_rc

    if pauses:
        return _handle_pause(payload, cfg, tool_name, tinput, session_id=session_id,
                             pauses=pauses, warns=warns)

    if warns:
        # Warn-only: surface, do not block.
        body = "; ".join(f"{r.name}: {msg}" for r, msg in warns)
        _emit_warn_context(f"Recurring feedback (warn): {body}")
    return 0


# ---------------------------------------------------------------------------
# Built-in administrative gate (management CLI mutations)
# ---------------------------------------------------------------------------
def _handle_admin_gate(payload: dict, cfg: dict, tool_name, tinput) -> int | None:
    """Human-gate the supported `exi feedback configure|disable|enable` mutations.

    Returns None when the command is not a supported management mutation (let
    normal PreToolUse flow continue). Otherwise consumes a one-shot approved
    admin permit (allow, return 0) or mints a fresh nonce and denies (return 0),
    asking the user to reply with the exact ``ALLOW_FEEDBACK_ADMIN:<nonce>``
    line. Approval is same-session, same exact tool fingerprint, TTL-bound, and
    one-shot; ``exi feedback record`` is intentionally NOT gated.
    """
    command = _command_of(tool_name, tinput)
    if not feedback.matches_admin_mutation(command):
        return None

    fcfg = cfg.get("feedback", {})
    ttl = _approval_ttl(fcfg)
    now = time.time()
    session_id = _session_id(payload)
    fingerprint = guard.fingerprint(tool_name, tinput)

    st = feedback.FeedbackState()
    with st.locked() as state:
        _warn_if_corrupt(state)
        if feedback.consume_admin_permit(state, session_id, fingerprint, now, ttl):
            print(f"[feedback] admin permit consumed for {fingerprint}", file=sys.stderr)
            return 0
        nonce = feedback.request_admin_pause(state, session_id, fingerprint, now, ttl)

    _emit_deny(
        "Feedback administration gate: this command would change or disable "
        "feedback enforcement (a supported `exi feedback configure|disable|enable` "
        "mutation). Codex has no native ask, so confirm out of band: if you (the "
        "user) really want this, reply with the exact line below. The model "
        "cannot approve this itself.\n"
        f"  -> to allow THIS exact call once, reply: {ADMIN_APPROVAL_PREFIX}{nonce}"
    )
    return 0


def _handle_pause(payload, cfg, tool_name, tinput, session_id, pauses, warns) -> int:
    fcfg = cfg.get("feedback", {})
    ttl = _approval_ttl(fcfg)
    now = time.time()
    fingerprint = guard.fingerprint(tool_name, tinput)
    pause_rule_names = list(dict.fromkeys(r.name for r, _ in pauses))

    st = feedback.FeedbackState()
    with st.locked() as state:
        if state.get("_corrupt"):
            print("[feedback WARN] session cache was unreadable; starting fresh", file=sys.stderr)
        # Allow only if EVERY paused rule already has an approved permit.
        all_permitted = all(
            _peek_permit(state, session_id, fingerprint, name, now, ttl)
            for name in pause_rule_names
        )
        if all_permitted:
            for name in pause_rule_names:
                feedback.consume_permit(state, session_id, fingerprint, name, now, ttl)
            # Permit consumed -> let the retry through (no output = allow).
            print(f"[feedback] pause permit consumed for {pause_rule_names}", file=sys.stderr)
            return 0
        # Otherwise mint a nonce for each not-yet-approved rule and deny.
        lines = []
        for name in pause_rule_names:
            if _peek_permit(state, session_id, fingerprint, name, now, ttl):
                continue
            nonce = feedback.request_pause(state, session_id, fingerprint, name, now, ttl)
            reason = next(msg for r, msg in pauses if r.name == name)
            lines.append(f"{name}: {reason}\n  -> to allow THIS exact call once, reply: {APPROVAL_PREFIX}{nonce}")

    detail = "\n".join(lines)
    _emit_deny(
        "Paused by recurring feedback. Codex has no native ask, so confirm out of "
        "band: if you (the user) really want this, reply with the exact line below. "
        "The model cannot approve this itself.\n" + detail
    )
    return 0


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------
def _handle_post_tool_use(payload: dict, cfg: dict) -> int:
    tool_name, tinput = _extract_tool(payload)
    targets = feedback.extract_edit_targets(tool_name, tinput)
    if not targets:
        return 0  # not an edit tool, or nothing to track (never guess Bash writes)
    session_id = _session_id(payload)
    cwd = _cwd(payload)
    paths = [p for p, _ in targets]
    st = feedback.FeedbackState()
    with st.locked() as state:
        if state.get("_corrupt"):
            print("[feedback WARN] session cache was unreadable; starting fresh", file=sys.stderr)
        feedback.track_changed_files(state, session_id, cwd, paths)
    return 0


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------
def _handle_stop(payload: dict, cfg: dict) -> int:
    fcfg = cfg.get("feedback", {})
    max_blocks = _stop_max_blocks(fcfg)
    store = feedback.FeedbackStore()
    rules = [r for r in _enabled_rules(store) if any(s.get("event") == "stop_check" for s in r.specs)]
    session_id = _session_id(payload)
    turn_id = _turn_id(payload)

    st = feedback.FeedbackState()
    with st.locked() as state:
        if state.get("_corrupt"):
            print("[feedback WARN] session cache was unreadable; starting fresh", file=sys.stderr)
        files = feedback.tracked_files(state, session_id)
        cwd = feedback.session_cwd(state, session_id) or _cwd(payload)

    if not rules or not files:
        print("{}")
        return 0

    root = _safe_root(cwd)
    blocking = []   # (rule, reason) for pause/deny severities
    warn_only = []
    for rule in rules:
        for spec in rule.specs:
            if spec.get("event") != "stop_check":
                continue
            for path in files:
                reason = feedback.eval_stop_check(rule, spec, path, root, cwd)
                if not reason:
                    continue
                sev = feedback.resolve_severity(rule, spec)
                if sev == feedback.WARN:
                    warn_only.append((rule, reason))
                else:
                    blocking.append((rule, reason))

    if not blocking:
        if warn_only:
            body = "; ".join(f"{r.name}: {m}" for r, m in warn_only)
            print(f"[feedback WARN] Stop check (non-blocking): {body}", file=sys.stderr)
        print("{}")
        return 0

    # Keyed by session+turn ONLY (not by which rules fired): an agent cannot
    # dodge the cap by alternating which configured rule blocks each attempt.
    key = feedback.stop_attempt_key(session_id, turn_id)
    with st.locked() as state:
        attempt = feedback.bump_stop_attempt(state, key)

    body = "; ".join(f"{r.name}: {m}" for r, m in dict((r.name, (r, m)) for r, m in blocking).values())
    # Record violations for audit (never touches count).
    for r, m in blocking:
        try:
            store.record_violation(r.name, "stop_check", m, session_id)
        except Exception as e:  # noqa: BLE001 - audit write must not wedge Stop
            print(f"[feedback WARN] could not record violation: {e}", file=sys.stderr)

    if attempt <= max_blocks:
        reason = (
            f"Before stopping, resolve recurring feedback (attempt {attempt}/{max_blocks}): "
            f"{body}. Fix the changed file(s), then stop."
        )
        print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
        print(f"[feedback BLOCK Stop {attempt}/{max_blocks}] {body}", file=sys.stderr)
        return 0

    # Loop guard: stop blocking after the cap; require manual human confirmation.
    msg = (
        f"Feedback Stop check still failing after {max_blocks} attempts ({body}). "
        "Not blocking again to avoid a loop — a human should review these changes."
    )
    print(json.dumps({"systemMessage": msg}, ensure_ascii=False))
    print(f"[feedback Stop cap reached] {msg}", file=sys.stderr)
    return 0


def _safe_root(cwd: str):
    from pathlib import Path
    try:
        return Path(cwd) if cwd else Path.cwd()
    except OSError:
        return Path.cwd()


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
        # Unknown event: never block. Stop-shaped safety not needed here.
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
