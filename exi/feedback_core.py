"""Provider-neutral feedback/memory policy engine.

This module holds ALL the enforcement *policy* — prompt handling, confirmed
memory retrieval, recurring-rule injection, zero-click candidate creation, tool
warn/pause/deny, the administrative gate, changed-file tracking, and the
unresolved-candidate/stop-check Stop logic — expressed against a normalized
:class:`Request` and returning *neutral outcome objects*. It emits no
provider-specific JSON and reads no provider-specific payload shape.

Each concrete surface (Codex, Claude Code, Copilot in VS Code, Copilot CLI) has
a thin adapter that (1) normalizes its raw hook payload into a ``Request``
— crucially, namespacing the session id by provider so multiple agents sharing
one machine's data directory can never collide — and (2) encodes the neutral
outcome into that surface's exact output contract. Adapters carry no policy;
this module is the single source of truth for it, so a fix here reaches every
surface at once.

Design invariants preserved from the original Codex-only hook:

* Automatic resolution may only ADD evidence / a new rule or DISMISS a
  candidate; it can never weaken or disable an existing rule.
* ``count`` is only ever moved by an explicit human-evidence ``record``; hook
  violations are audit-only.
* Nothing here runs a shell command, calls a network/LLM, or persists a raw
  prompt/transcript. Candidates store a hash + coarse cue categories only.
* Every operation fails OPEN on an internal error (the adapter surfaces the
  error and gets out of the way) — the only intentional "closed" decisions are
  the enforcement warn/pause/deny results themselves.
"""
from __future__ import annotations

import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import feedback, feedback_detect
from .store import ABS_MAX_MEMORY_CHARS, ABS_MAX_MEMORY_RESULTS, Store, relevance_score

# ---- providers -------------------------------------------------------------
PROVIDER_CODEX = "codex"
PROVIDER_CLAUDE = "claude"
PROVIDER_COPILOT_VSCODE = "copilot-vscode"
PROVIDER_COPILOT_CLI = "copilot-cli"
PROVIDERS = (PROVIDER_CODEX, PROVIDER_CLAUDE, PROVIDER_COPILOT_VSCODE, PROVIDER_COPILOT_CLI)

# ---- neutral events --------------------------------------------------------
EV_USER_PROMPT = "user_prompt"
EV_PRE_TOOL = "pre_tool"
EV_POST_TOOL = "post_tool"
EV_STOP = "stop"

# ---- approval markers ------------------------------------------------------
# A user reply approves a paused/admin-gated action ONLY when the ENTIRE
# stripped prompt matches exactly (16 lowercase hex nonce). The model cannot
# forge a user prompt, so this is the out-of-band human "yes".
APPROVAL_PREFIX = "ALLOW_FEEDBACK:"
ADMIN_APPROVAL_PREFIX = "ALLOW_FEEDBACK_ADMIN:"
_APPROVAL_RE = re.compile(r"^ALLOW_FEEDBACK:([0-9a-f]{16})$")
_ADMIN_APPROVAL_RE = re.compile(r"^ALLOW_FEEDBACK_ADMIN:([0-9a-f]{16})$")

# Absolute ceilings (config can only make these SMALLER, never larger).
ABS_MAX_INJECT_CHARS = 3000
# Hard bound on the TOTAL context appended to one prompt across all sections
# (memory + recurring rules + candidate instruction). Each section is already
# individually bounded; this is the belt over the sum so a hostile config or a
# pile of sections can never append an unbounded blob (matters most for the
# Copilot CLI ``modifiedTransformedPrompt`` path).
ABS_MAX_TOTAL_CONTEXT_CHARS = 8000


# ---------------------------------------------------------------------------
# Normalized request
# ---------------------------------------------------------------------------
@dataclass
class Request:
    provider: str
    raw_session_id: str
    turn_id: str | None
    cwd: str
    cfg: dict
    now: float
    prompt: str = ""
    tool_name: str = ""
    tool_input: object = None
    stop_hook_active: bool = False

    @property
    def session_id(self) -> str:
        """Provider-namespaced session key used for ALL session-state storage."""
        return namespace_session(self.provider, self.raw_session_id)


def namespace_session(provider: str, raw_session_id: str) -> str:
    """Prefix a raw session id with its provider so two surfaces sharing one
    machine's data directory can never read/write each other's session state."""
    return f"{provider}:{raw_session_id or 'unknown-session'}"


# ---------------------------------------------------------------------------
# Neutral outcomes
# ---------------------------------------------------------------------------
@dataclass
class PromptOutcome:
    # `context` is the bounded text to inject (approval confirmation OR the
    # combined memory/rules/candidate block); "" means inject nothing.
    context: str = ""
    is_approval: bool = False


@dataclass
class ToolOutcome:
    action: str = "allow"     # allow | warn | pause | deny
    reason: str = ""          # deny/pause: full text incl. any nonce line(s);
    #                            warn: the warning text; allow: ""


@dataclass
class StopOutcome:
    action: str = "allow"     # allow | block | capped
    reason: str = ""          # block: continuation reason
    system_message: str = ""  # capped: human-audit note
    warn_only: str = ""       # allow: non-blocking stderr warning text


# ---------------------------------------------------------------------------
# Config clamps (a hostile/typo'd config must never WEAKEN a guard)
# ---------------------------------------------------------------------------
def approval_ttl(fcfg: dict) -> int:
    try:
        ttl = int(fcfg.get("approval_ttl_seconds", 600))
    except (TypeError, ValueError):
        ttl = 600
    return max(1, min(ttl, 86_400))


def inject_min_count(fcfg: dict) -> int:
    try:
        n = int(fcfg.get("inject_min_count", 3))
    except (TypeError, ValueError):
        n = 3
    return max(1, n)


def inject_max_chars(fcfg: dict) -> int:
    try:
        n = int(fcfg.get("inject_max_chars", ABS_MAX_INJECT_CHARS))
    except (TypeError, ValueError):
        n = ABS_MAX_INJECT_CHARS
    return max(1, min(n, ABS_MAX_INJECT_CHARS))


def stop_max_blocks(fcfg: dict) -> int:
    try:
        n = int(fcfg.get("stop_max_blocks", feedback.HARD_MAX_STOP_BLOCKS))
    except (TypeError, ValueError):
        n = feedback.HARD_MAX_STOP_BLOCKS
    return max(0, min(n, feedback.HARD_MAX_STOP_BLOCKS))


def mem_max_results(mcfg: dict) -> int:
    try:
        n = int(mcfg.get("inject_max_results", 5))
    except (TypeError, ValueError):
        n = 5
    return max(0, min(n, ABS_MAX_MEMORY_RESULTS))


def mem_max_chars(mcfg: dict) -> int:
    try:
        n = int(mcfg.get("inject_max_chars", 1500))
    except (TypeError, ValueError):
        n = 1500
    return max(1, min(n, ABS_MAX_MEMORY_CHARS))


# ---------------------------------------------------------------------------
# Shared payload extraction (used by adapters; centralized so no duplication)
# ---------------------------------------------------------------------------
def extract_tool(payload: dict):
    """Best-effort (tool_name, tool_input) across snake_case, camelCase, and the
    Copilot CLI ``toolArgs`` shape."""
    name = (
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("name")
        or payload.get("tool")
        or ""
    )
    if "tool_input" in payload:
        tinput = payload.get("tool_input")
    else:
        tinput = payload.get("toolInput",
                             payload.get("toolArgs",
                                         payload.get("input", payload.get("arguments", {}))))
    return name, tinput


# Tool-name fragments that identify a shell/command execution across surfaces
# (Codex ``shell``/Bash, VS Code ``runInTerminal``, Copilot ``executeCommand``).
_COMMAND_NAME_HINTS = ("bash", "shell", "run", "terminal", "exec", "command")
# Fragments that identify a file-editing tool (VS Code ``editFiles``, etc.).
_EDIT_NAME_HINTS = ("edit", "write", "apply_patch", "applypatch", "str_replace",
                    "createfile", "notebook")


def is_command_tool(tool_name: str) -> bool:
    n = (tool_name or "").lower()
    return any(h in n for h in _COMMAND_NAME_HINTS)


def command_of(tool_name: str, tinput) -> str | None:
    """Extract the shell command string for a command tool, else None.

    Returns None for a non-command tool so ``pre_bash`` specs simply do not
    apply; returns "" for a command tool with an empty/absent command.
    """
    if not is_command_tool(tool_name):
        return None
    if isinstance(tinput, str):
        return tinput
    if isinstance(tinput, dict):
        cmd = tinput.get("command") or tinput.get("cmd") or tinput.get("commandLine")
        if isinstance(cmd, list):
            return " ".join(str(x) for x in cmd)
        return str(cmd) if cmd is not None else ""
    return ""


# ---------------------------------------------------------------------------
# Active-turn key (platforms without a native turn/prompt id)
#
# When a surface gives us no turn id, we cannot key the per-turn Stop counter on
# a real turn — but keying it on one permanent constant ("unknown-turn") would
# make the counter a single global cap across ALL turns of a session (after N
# blocks ever, the guard could never block again). Instead we persist a
# body-less synthetic active-turn id at prompt time (rotated each new prompt)
# and reuse it at Stop, so each turn gets its own counter.
# ---------------------------------------------------------------------------
def _set_active_turn(state: dict, session_id: str, tid: str, now: float) -> str:
    sess = state.setdefault("sessions", {}).setdefault(session_id, {})
    sess["active_turn"] = tid
    sess["active_turn_at"] = now
    return tid


def _rotate_active_turn(state: dict, session_id: str, now: float) -> str:
    return _set_active_turn(state, session_id, "t" + secrets.token_hex(8), now)


def _get_active_turn(state: dict, session_id: str) -> str | None:
    return state.get("sessions", {}).get(session_id, {}).get("active_turn")


def _effective_prompt_turn(req: "Request") -> str:
    """Turn id to use at prompt time: the native id if any, else a freshly
    rotated synthetic one — and EITHER way persisted as the session's active
    turn. Persisting even a native id matters: a surface can supply a turn id on
    the prompt but omit it on Stop, and Stop must still recover this turn's key
    (otherwise it would fall back to one permanent cross-turn counter)."""
    st = feedback.FeedbackState()
    with st.locked() as state:
        if req.turn_id:
            return _set_active_turn(state, req.session_id, req.turn_id, req.now)
        return _rotate_active_turn(state, req.session_id, req.now)


def _effective_stop_turn(req: "Request") -> str:
    """Turn id to use at Stop: the native id, else the active-turn persisted at
    prompt time; if none was ever set (orphan Stop with no preceding prompt),
    mint+persist one so it is still never the shared permanent constant."""
    if req.turn_id:
        return req.turn_id
    st = feedback.FeedbackState()
    with st.locked() as state:
        tid = _get_active_turn(state, req.session_id)
        if not tid:
            tid = _rotate_active_turn(state, req.session_id, req.now)
        return tid


# ---------------------------------------------------------------------------
# UserPromptSubmit policy
# ---------------------------------------------------------------------------
def user_prompt_outcome(req: "Request") -> PromptOutcome:
    fcfg = req.cfg.get("feedback", {})
    ttl = approval_ttl(fcfg)
    now = req.now
    session_id = req.session_id
    raw_prompt = req.prompt or ""
    stripped = raw_prompt.strip()

    # Every submitted prompt is a new effective turn, including an approval
    # reply. Some hosts omit turn ids on Stop; persisting/rotating here keeps
    # the Stop cap scoped to this turn instead of reusing the preceding turn's
    # exhausted counter.
    turn_id = _effective_prompt_turn(req)

    # 1) Administrative-gate approval (exact whole-prompt match).
    m_admin = _ADMIN_APPROVAL_RE.match(stripped)
    if m_admin:
        nonce = m_admin.group(1)
        st = feedback.FeedbackState()
        with st.locked() as state:
            approved = feedback.approve_admin_nonce(state, session_id, nonce, now, ttl)
        if approved:
            return PromptOutcome(
                context=(f"Feedback administration approved for this session (nonce {nonce}). "
                         "The next identical `exi feedback configure|disable|enable` call will "
                         "be permitted exactly once."),
                is_approval=True,
            )
        _warn(f"no pending admin gate matched nonce {nonce!r}")
        return PromptOutcome(is_approval=True)

    # 2) Normal pause approval (exact whole-prompt match).
    m = _APPROVAL_RE.match(stripped)
    if m:
        nonce = m.group(1)
        st = feedback.FeedbackState()
        with st.locked() as state:
            approved = feedback.approve_nonce(state, session_id, nonce, now, ttl)
        if approved:
            return PromptOutcome(
                context=(f"Feedback pause approved for this session (nonce {nonce}). The next "
                         "identical tool call will be permitted exactly once."),
                is_approval=True,
            )
        _warn(f"no pending pause matched nonce {nonce!r}")
        return PromptOutcome(is_approval=True)

    # A near-miss marker (right stem, wrong/extra text) approves nothing and is
    # never treated as an ordinary prompt (never a candidate, never injected).
    if stripped.startswith(ADMIN_APPROVAL_PREFIX) or stripped.startswith(APPROVAL_PREFIX):
        _warn("approval marker did not match exactly; ignoring")
        return PromptOutcome(is_approval=True)

    # 3) Normal prompt. Fix the effective turn id (rotating a synthetic one when
    #    the platform lacks one) so candidate ids and the Stop counter are
    #    per-turn, then assemble sections independently (each fail-open).
    cwd = req.cwd
    sections: list[str] = []

    try:
        mem = memory_section(raw_prompt, cwd, req.cfg)
        if mem:
            sections.append(mem)
    except Exception as e:  # noqa: BLE001
        _warn(f"[memory fail-open] {type(e).__name__}: {e}")

    try:
        rule_text = rule_injection_section(req.cfg, cwd)
        if rule_text:
            sections.append(rule_text)
    except Exception as e:  # noqa: BLE001
        _warn(f"[injection fail-open] {type(e).__name__}: {e}")

    try:
        cand_text = candidate_section(raw_prompt, req.cfg, session_id, turn_id, now)
        if cand_text:
            sections.append(cand_text)
    except Exception as e:  # noqa: BLE001
        _warn(f"[candidate fail-open] {type(e).__name__}: {e}")

    if not sections:
        return PromptOutcome()
    text = "\n\n".join(sections)
    if len(text) > ABS_MAX_TOTAL_CONTEXT_CHARS:
        text = text[:ABS_MAX_TOTAL_CONTEXT_CHARS]
        _warn("combined context exceeded total ceiling; trimmed")
    return PromptOutcome(context=text)


def _scope_matches_cwd(rule, cwd: str) -> bool:
    scope = getattr(rule, "scope", "") or ""
    if not scope:
        return True
    return scope in (cwd or "")


def _enabled_rules(store: feedback.FeedbackStore) -> list:
    return [r for r in store.derive().values() if r.enabled]


def rule_injection_section(cfg: dict, cwd: str) -> str:
    fcfg = cfg.get("feedback", {})
    store = feedback.FeedbackStore()
    min_count = inject_min_count(fcfg)
    rules = [r for r in _enabled_rules(store)
             if r.count >= min_count and _scope_matches_cwd(r, cwd)]
    if not rules:
        return ""
    rules.sort(key=lambda r: (-r.count, r.name))
    text = _build_injection(rules, inject_max_chars(fcfg))
    if len(text) > ABS_MAX_INJECT_CHARS:
        text = text[:ABS_MAX_INJECT_CHARS]
    return text


def _rule_severity_label(rule) -> str:
    if rule.count >= feedback.AUTO_DENY_AT:
        return "deny"
    if rule.count >= feedback.AUTO_PAUSE_AT:
        return "pause"
    return "warn"


def _full_entry(rule) -> str:
    lines = [f"- [{rule.name}] (asked {rule.count}x, default {_rule_severity_label(rule)}): {rule.description}"]
    if rule.why:
        lines.append(f"    Why: {rule.why}")
    if rule.how_to_apply:
        lines.append(f"    How to apply: {rule.how_to_apply}")
    if rule.excuse:
        lines.append(f"    Not an excuse: {rule.excuse}")
    return "\n".join(lines)


def _brief_entry(rule) -> str:
    return f"- [{rule.name}] (asked {rule.count}x): {rule.description}"


def _build_injection(rules: list, max_chars: int) -> str:
    header = "Recurring user feedback — apply these without being asked again:\n"
    forms = {r.name: _full_entry(r) for r in rules}

    def assemble() -> str:
        return header + "\n".join(forms[r.name] for r in rules)

    body = assemble()
    if len(body) <= max_chars:
        return body
    for r in reversed(rules):
        forms[r.name] = _brief_entry(r)
        if len(assemble()) <= max_chars:
            return assemble()
    kept = list(rules)
    while kept:
        kept.pop()
        note = f"\n(+{len(rules) - len(kept)} lower-count rule(s) omitted for space)"
        candidate = header + "\n".join(forms[r.name] for r in kept) + note
        if len(candidate) <= max_chars and kept:
            return candidate
    cut = assemble()[:max_chars]
    nl = cut.rfind("\n")
    return cut[:nl] if nl > len(header) else cut


def memory_section(prompt: str, cwd: str, cfg: dict) -> str:
    if not prompt.strip():
        return ""
    mcfg = cfg.get("memory", {}) if isinstance(cfg.get("memory"), dict) else {}
    max_results = mem_max_results(mcfg)
    max_chars = mem_max_chars(mcfg)
    if max_results == 0:
        return ""
    try:
        min_rel = int(mcfg.get("min_relevance", 2))
    except (TypeError, ValueError):
        min_rel = 2

    store = Store()
    try:
        hits = store.retrieve(prompt, cwd=cwd, limit=max_results)
    except Exception as e:  # noqa: BLE001 - empty/corrupt store must fail open
        _warn(f"[memory] store unavailable ({type(e).__name__}); skipping retrieval")
        return ""
    kept = [o for o in hits if relevance_score(prompt, cwd, o) >= min_rel]
    if not kept:
        return ""
    header = "Relevant knowledge from the exi store (confirmed, human-reviewed observations):"

    def entry(o) -> str:
        claim = o.claim if len(o.claim) <= 240 else o.claim[:237] + "…"
        return f"- [{o.scope}] {claim} (evidence x{o.confirmed_count})"

    lines = [entry(o) for o in kept]

    def assemble(n: int) -> str:
        body = "\n".join(lines[:n])
        if n < len(lines):
            body += f"\n(+{len(lines) - n} more relevant item(s) omitted for space)"
        return header + "\n" + body

    n = len(lines)
    while n > 0 and len(assemble(n)) > max_chars:
        n -= 1
    if n == 0:
        return ""
    text = assemble(n)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def candidate_section(prompt: str, cfg: dict, session_id: str, turn_id: str, now: float) -> str:
    if not feedback.auto_capture_enabled(cfg):
        return ""
    is_fb, cues = feedback_detect.detect_feedback(prompt)
    if not is_fb:
        return ""
    p_hash = feedback_detect.prompt_hash(prompt)
    cid = feedback_detect.candidate_id(session_id, turn_id, p_hash)
    ttl = feedback.candidate_ttl_seconds(cfg)
    st = feedback.FeedbackState()
    with st.locked() as state:
        status = feedback.upsert_candidate(state, cid, session_id, turn_id, p_hash, cues, now, ttl)
    if status in (feedback.CANDIDATE_RESOLVED, feedback.CANDIDATE_DISMISSED,
                  feedback.CANDIDATE_ABANDONED):
        return ""
    cue_str = ", ".join(cues) if cues else "corrective-tone"
    return (
        "This prompt looks like recurring corrective feedback "
        f"(cues: {cue_str}). WITHOUT asking the user and WITHOUT requiring any "
        "manual command from them, resolve feedback candidate "
        f"{cid} now:\n"
        "  1. Decide whether it matches an existing feedback rule (see the "
        "recurring-feedback block above / `exi feedback list`) or is a new rule.\n"
        "  2. Then run EXACTLY ONE of:\n"
        f"     exi feedback resolve --candidate {cid} --name <rule-name> "
        "--description <canonical rule> --why <why it matters> "
        "--how-to-apply <how to comply>\n"
        f"     exi feedback dismiss --candidate {cid} --reason <why this is NOT feedback>\n"
        "The evidence id is derived internally from the candidate — do not pass "
        "one. `resolve` only records a new occurrence/new rule; it cannot "
        "disable or alter existing rules. If unsure it is really feedback, "
        "dismiss with a reason. Do this before you stop."
    )


# ---------------------------------------------------------------------------
# PreToolUse policy
# ---------------------------------------------------------------------------
def _collect_pretool_matches(rules: list, tool_name: str, tinput, cwd: str) -> list:
    matches = []
    command = command_of(tool_name, tinput)
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


def _peek_permit(state, session_id, fingerprint, rule_name, now, ttl) -> bool:
    for a in state.get("approvals", []):
        if (a["session_id"] == session_id and a["fingerprint"] == fingerprint
                and a["rule"] == rule_name and a.get("status") == "approved"
                and now - a.get("created_at", 0) <= ttl):
            return True
    return False


def _record_pretool_violations(store, matches, session_id) -> None:
    seen = set()
    for rule, spec, reason in matches:
        if rule.name in seen:
            continue
        seen.add(rule.name)
        try:
            store.record_violation(rule.name, spec.get("event", "pre_tool"), reason, session_id)
        except Exception as e:  # noqa: BLE001
            _warn(f"could not record violation: {e}")


def _fingerprint(tool_name, tinput) -> str:
    from . import guard
    return guard.fingerprint(tool_name, tinput)


def pre_tool_outcome(req: "Request") -> ToolOutcome:
    store = feedback.FeedbackStore()
    rules = _enabled_rules(store)
    tool_name, tinput = req.tool_name, req.tool_input
    cwd = req.cwd
    session_id = req.session_id
    matches = _collect_pretool_matches(rules, tool_name, tinput, cwd)

    pauses, warns = [], []
    if matches:
        _record_pretool_violations(store, matches, session_id)
        denies = []
        for rule, spec, reason in matches:
            sev = feedback.resolve_severity(rule, spec)
            if sev == feedback.DENY:
                denies.append((rule, reason))
            elif sev == feedback.PAUSE:
                pauses.append((rule, reason))
            else:
                warns.append((rule, reason))
        # Hard deny wins first and is never bypassable (not by a pause nonce and
        # not by an admin permit, which is only checked afterward).
        if denies:
            body = "; ".join(f"{r.name}: {msg}" for r, msg in denies)
            return ToolOutcome(action="deny", reason=(
                f"Blocked by recurring feedback (deny): {body}. This is a hard block "
                "(the user has raised it enough times); fix the underlying issue, do "
                "not retry the same call."))

    # Administrative gate (management CLI mutations). After hard deny.
    admin = _admin_gate_outcome(req, tool_name, tinput)
    if admin is not None:
        return admin

    if pauses:
        return _pause_outcome(req, tool_name, tinput, session_id, pauses)

    if warns:
        body = "; ".join(f"{r.name}: {msg}" for r, msg in warns)
        return ToolOutcome(action="warn", reason=f"Recurring feedback (warn): {body}")
    return ToolOutcome(action="allow")


def _admin_gate_outcome(req: "Request", tool_name, tinput):
    command = command_of(tool_name, tinput)
    if not feedback.matches_admin_mutation(command):
        return None
    fcfg = req.cfg.get("feedback", {})
    ttl = approval_ttl(fcfg)
    now = req.now
    session_id = req.session_id
    fingerprint = _fingerprint(tool_name, tinput)
    st = feedback.FeedbackState()
    with st.locked() as state:
        if feedback.consume_admin_permit(state, session_id, fingerprint, now, ttl):
            _log(f"admin permit consumed for {fingerprint}")
            return ToolOutcome(action="allow")
        nonce = feedback.request_admin_pause(state, session_id, fingerprint, now, ttl)
    return ToolOutcome(action="deny", reason=(
        "Feedback administration gate: this command would change or disable "
        "feedback enforcement (a supported `exi feedback configure|disable|enable` "
        "mutation). This assistant cannot approve it itself; confirm out of band: "
        "if you (the user) really want this, reply with the exact line below.\n"
        f"  -> to allow THIS exact call once, reply: {ADMIN_APPROVAL_PREFIX}{nonce}"))


def _pause_outcome(req: "Request", tool_name, tinput, session_id, pauses) -> ToolOutcome:
    fcfg = req.cfg.get("feedback", {})
    ttl = approval_ttl(fcfg)
    now = req.now
    fingerprint = _fingerprint(tool_name, tinput)
    pause_rule_names = list(dict.fromkeys(r.name for r, _ in pauses))
    st = feedback.FeedbackState()
    with st.locked() as state:
        all_permitted = all(
            _peek_permit(state, session_id, fingerprint, name, now, ttl)
            for name in pause_rule_names)
        if all_permitted:
            for name in pause_rule_names:
                feedback.consume_permit(state, session_id, fingerprint, name, now, ttl)
            _log(f"pause permit consumed for {pause_rule_names}")
            return ToolOutcome(action="allow")
        lines = []
        for name in pause_rule_names:
            if _peek_permit(state, session_id, fingerprint, name, now, ttl):
                continue
            nonce = feedback.request_pause(state, session_id, fingerprint, name, now, ttl)
            reason = next(msg for r, msg in pauses if r.name == name)
            lines.append(f"{name}: {reason}\n  -> to allow THIS exact call once, reply: {APPROVAL_PREFIX}{nonce}")
    detail = "\n".join(lines)
    return ToolOutcome(action="pause", reason=(
        "Paused by recurring feedback. This assistant cannot approve its own "
        "paused action; confirm out of band: if you (the user) really want it, "
        "reply with the exact line below.\n" + detail))


# ---------------------------------------------------------------------------
# PostToolUse policy
# ---------------------------------------------------------------------------
def post_tool_track(req: "Request") -> None:
    targets = feedback.extract_edit_targets(req.tool_name, req.tool_input)
    if not targets:
        return
    paths = [p for p, _ in targets]
    st = feedback.FeedbackState()
    with st.locked() as state:
        _warn_if_corrupt(state)
        feedback.track_changed_files(state, req.session_id, req.cwd, paths)


# ---------------------------------------------------------------------------
# Stop policy
# ---------------------------------------------------------------------------
def stop_outcome(req: "Request") -> StopOutcome:
    fcfg = req.cfg.get("feedback", {})
    max_blocks = stop_max_blocks(fcfg)
    ttl = feedback.candidate_ttl_seconds(req.cfg)
    now = req.now
    store = feedback.FeedbackStore()
    rules = [r for r in _enabled_rules(store)
             if any(s.get("event") == "stop_check" for s in r.specs)]
    session_id = req.session_id

    st = feedback.FeedbackState()
    with st.locked() as state:
        _warn_if_corrupt(state)
        files = feedback.tracked_files(state, session_id)
        cwd = feedback.session_cwd(state, session_id) or req.cwd
        pending = feedback.pending_candidates(state, session_id, now, ttl)

    cand_pending = bool(pending) and feedback.auto_capture_enabled(req.cfg)

    root = _safe_root(cwd)
    blocking, warn_only = [], []
    if rules and files:
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

    if not blocking and not cand_pending:
        if warn_only:
            body = "; ".join(f"{r.name}: {m}" for r, m in warn_only)
            return StopOutcome(action="allow", warn_only=f"Stop check (non-blocking): {body}")
        return StopOutcome(action="allow")

    # The host says our Stop hook is already active in this continuation: never
    # block again (at most one continuation on a self-limiting platform).
    if req.stop_hook_active:
        return StopOutcome(action="allow")

    turn_id = _effective_stop_turn(req)
    key = feedback.stop_attempt_key(session_id, turn_id)
    with st.locked() as state:
        attempt = feedback.bump_stop_attempt(state, key)

    reasons = []
    if blocking:
        body = "; ".join(
            f"{r.name}: {m}" for r, m in dict((r.name, (r, m)) for r, m in blocking).values())
        reasons.append(f"unresolved recurring feedback — fix the changed file(s): {body}")
        for r, m in blocking:
            try:
                store.record_violation(r.name, "stop_check", m, session_id)
            except Exception as e:  # noqa: BLE001
                _warn(f"could not record violation: {e}")
    if cand_pending:
        ids = ", ".join(c["id"] for c in pending)
        reasons.append(
            "an unresolved feedback candidate is still pending "
            f"({ids}). Resolve it WITHOUT asking the user: run `exi feedback "
            "resolve --candidate <id> --name <rule> --description <...> --why "
            "<...> --how-to-apply <...>` to record/merge a rule, or `exi feedback "
            "dismiss --candidate <id> --reason <...>` if it is not feedback")
    body = " AND ".join(reasons)

    if attempt <= max_blocks:
        return StopOutcome(action="block", reason=(
            f"Before stopping (attempt {attempt}/{max_blocks}): {body}. Then stop."))

    abandoned = []
    if cand_pending:
        with st.locked() as state:
            abandoned = feedback.abandon_pending(state, session_id, now, ttl)
    msg = (f"Feedback still unresolved after {max_blocks} attempts ({body}). Not "
           "blocking again to avoid a loop — a human should review.")
    if abandoned:
        msg += f" Left {len(abandoned)} feedback candidate(s) for audit: {', '.join(abandoned)}."
    return StopOutcome(action="capped", system_message=msg)


def _safe_root(cwd: str):
    try:
        return Path(cwd) if cwd else Path.cwd()
    except OSError:
        return Path.cwd()


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------
def _warn(msg: str) -> None:
    print(f"[feedback WARN] {msg}", file=sys.stderr)


def _log(msg: str) -> None:
    print(f"[feedback] {msg}", file=sys.stderr)


def _warn_if_corrupt(state: dict) -> None:
    if state.get("_corrupt"):
        print("[feedback WARN] session cache was unreadable; starting fresh", file=sys.stderr)
