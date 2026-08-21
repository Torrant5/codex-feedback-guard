"""Feedback-learning store: append-only JSONL rules + safe enforcement engine.

"Never make me say the same thing twice." A human records a piece of feedback
(a *rule*) with an evidence identifier; the store is the source of truth for
what the user has asked and how many *distinct times* they have had to ask it.
That count — and nothing derived automatically — drives how forcefully the
Codex hooks nudge (warn), gate (pause), or block (deny) future work.

Source of truth is ``feedback.jsonl`` (one JSON event per line). Current state
is derived by replaying the log, so history/audit is never lost. A corrupt log
line is surfaced with its line number, never silently dropped — and the hooks
fail *open* (they surface the error and get out of the way) rather than
silently resetting the data.

Design guarantees:
  * ``count`` == number of *distinct human evidence records* for a rule. It is
    incremented ONLY by an explicit ``record`` event citing a new evidence id.
    A hook violation appends a ``violation`` event but never touches ``count``.
  * A ``record`` re-citing an evidence id already on file is rejected, so the
    same complaint logged twice cannot inflate ``count``.
  * Enforcement specs are *declarative only*. They describe path globs, sibling
    existence, and safe regexes over content the tools already carry. There is
    NO facility to run an arbitrary shell command or checker — the engine
    evaluates built-in conditions and nothing else.

Runtime session state (which files changed this session, pending pause
approvals, Stop-loop attempt counters) lives in a *separate*, disposable
``feedback-state.json`` — it is a cache, not the rule data, and is allowed to
start empty if damaged.
"""
from __future__ import annotations

import json
import re
import secrets
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import config
from .locking import file_lock

# ---- severity ladder -------------------------------------------------------
WARN = "warn"
PAUSE = "pause"
DENY = "deny"
SEVERITIES = (WARN, PAUSE, DENY)

# auto severity thresholds (used only when a spec has no explicit severity)
AUTO_PAUSE_AT = 3   # count 3-4 -> pause
AUTO_DENY_AT = 5    # count >= 5 -> deny

EVENTS = ("pre_bash", "pre_edit", "stop_check")

# Absolute, non-configurable ceiling on Stop-loop blocks. `feedback_hook`
# clamps any configured `stop_max_blocks` to this no matter what config says.
HARD_MAX_STOP_BLOCKS = 3

# Pending-candidate expiry defaults / clamps. A candidate is a short-lived,
# body-less marker that "this turn's prompt looked like corrective feedback";
# it must expire so a never-resolved candidate cannot nag or block forever.
DEFAULT_CANDIDATE_TTL = 3600
_MIN_CANDIDATE_TTL = 60
_MAX_CANDIDATE_TTL = 7 * 86_400


def candidate_ttl_seconds(cfg: dict) -> int:
    """Pending-candidate TTL, clamped to a sane [_MIN, _MAX] window."""
    fcfg = cfg.get("feedback", {}) if isinstance(cfg, dict) else {}
    try:
        ttl = int(fcfg.get("candidate_ttl_seconds", DEFAULT_CANDIDATE_TTL))
    except (TypeError, ValueError):
        ttl = DEFAULT_CANDIDATE_TTL
    return max(_MIN_CANDIDATE_TTL, min(ttl, _MAX_CANDIDATE_TTL))


def auto_capture_enabled(cfg: dict) -> bool:
    """Whether zero-click feedback capture is on (default: on)."""
    fcfg = cfg.get("feedback", {}) if isinstance(cfg, dict) else {}
    return bool(fcfg.get("auto_capture", True))


def memory_auto_capture_enabled(cfg: dict) -> bool:
    """Whether autonomous durable-memory capture is on (default: on).

    Distinct from ``feedback.auto_capture``: this governs the per-turn
    memory-review instruction and the body-less memory candidate that backs
    ``exi memory resolve`` — NOT the corrective-feedback loop.
    """
    mcfg = cfg.get("memory", {}) if isinstance(cfg, dict) else {}
    return bool(mcfg.get("auto_capture", True))


def memory_candidate_ttl_seconds(cfg: dict) -> int:
    """Memory-candidate TTL, clamped to the same sane [_MIN, _MAX] window as
    feedback candidates. Falls back to the feedback TTL / default when unset."""
    mcfg = cfg.get("memory", {}) if isinstance(cfg, dict) else {}
    fcfg = cfg.get("feedback", {}) if isinstance(cfg, dict) else {}
    default = fcfg.get("candidate_ttl_seconds", DEFAULT_CANDIDATE_TTL)
    try:
        ttl = int(mcfg.get("candidate_ttl_seconds", default))
    except (TypeError, ValueError):
        ttl = DEFAULT_CANDIDATE_TTL
    return max(_MIN_CANDIDATE_TTL, min(ttl, _MAX_CANDIDATE_TTL))

# Allowed enforcement-spec keys. Anything else is rejected by validation so a
# spec can never smuggle in an arbitrary command/checker to execute.
_COMMON_KEYS = {"event", "severity", "message", "scope", "unless"}
_EVENT_KEYS = {
    "pre_bash": {"when", "forbid_regex"},
    "pre_edit": {"path_glob", "exclude_glob", "absent_sibling", "require_regex", "forbid_regex"},
    "stop_check": {"path_glob", "exclude_glob", "absent_sibling", "require_regex", "forbid_regex"},
}
_REGEX_KEYS = {"when", "forbid_regex", "require_regex", "unless"}


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


class FeedbackDataError(Exception):
    """The feedback rule log is corrupt. Enforcement must fail OPEN, not reset it."""


# ---------------------------------------------------------------------------
# ReDoS-safe regex evaluation
#
# Rule specs let a human embed arbitrary regexes (forbid_regex/require_regex/
# unless/when/path_glob translations). A pathological pattern like `(a+)+$`
# against adversarial input can backtrack for an unbounded amount of time in
# Python's `re` engine. Every RUNTIME regex evaluation in this module goes
# through `safe_search`/`safe_match`, which enforce:
#   1. a strict max length on the pattern and the input text, and
#   2. a short SIGALRM/setitimer wall-clock deadline (POSIX, single-threaded
#      hook process only — see `_regex_deadline`).
# On overflow or timeout this raises a clear exception rather than hanging or
# silently treating the rule as matched/unmatched; the top-level feedback hook
# (`feedback_hook.handle`) catches it and fails OPEN, exactly like any other
# internal error, so a bad regex degrades to "no enforcement this call" with a
# visible stderr message, never a hang and never a false block.
# ---------------------------------------------------------------------------
class RegexEvalError(Exception):
    """A runtime regex evaluation could not be completed safely."""


class RegexTimeout(RegexEvalError):
    """A regex search exceeded its wall-clock deadline (suspected ReDoS)."""


class RegexLimitError(RegexEvalError):
    """A regex pattern or its input text exceeded a configured size limit."""


# Validated at configure() time (spec authoring), so a too-long pattern is
# rejected before it is ever stored.
MAX_REGEX_PATTERN_CHARS = 500
MAX_GLOB_PATTERN_CHARS = 500
MAX_SIBLING_TEMPLATE_CHARS = 300
MAX_MESSAGE_CHARS = 2000
MAX_SCOPE_CHARS = 500

# Enforced at RUNTIME against the text a regex is matched against (bash
# command / proposed edit content / on-disk file content). Oversized input
# raises RegexLimitError rather than being fed to the regex engine.
MAX_COMMAND_CHARS = 20_000
MAX_CONTENT_CHARS = 200_000
MAX_PATH_CHARS = 4096
# Internal ceiling for the regex *translated* from a glob pattern (glob_match);
# independent of MAX_REGEX_PATTERN_CHARS, which bounds the raw glob string.
_MAX_TRANSLATED_REGEX_CHARS = 4000

# Wall-clock deadline for a single regex evaluation. POSIX main-thread calls
# also use SIGALRM. Every platform first applies the conservative structural
# rejection below, so Windows never evaluates the common catastrophic nested-
# quantifier/ambiguous-alternation shapes without a deadline.
REGEX_SEARCH_TIMEOUT_SECONDS = 0.5

_HAS_SIGALRM = hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")


def _reject_unsafe_regex_shape(pattern: str) -> None:
    """Reject backtracking-prone structures before calling :mod:`re`.

    Python's standard regex engine has no per-match timeout on Windows.  A
    worker thread cannot safely provide one because a pathological match can
    retain the GIL.  This small, deliberately conservative scanner rejects the
    most dangerous user-controlled shapes on *all* platforms: a repeated group
    that itself contains repetition or alternation, and numeric backreferences.
    Simple rule patterns (literals, character classes, ``\\s*``, anchors, and
    ordinary non-nested repetition) continue to work.
    """
    stack: list[dict[str, bool]] = []
    last_atom: dict[str, bool] | None = None
    in_class = False
    escaped = False
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if escaped:
            if c.isdigit():
                raise RegexTimeout("regex backreferences are not allowed")
            escaped = False
            last_atom = {"repeated": False, "alternation": False}
            i += 1
            continue
        if c == "\\":
            escaped = True
            i += 1
            continue
        if in_class:
            if c == "]":
                in_class = False
                last_atom = {"repeated": False, "alternation": False}
            i += 1
            continue
        if c == "[":
            in_class = True
            i += 1
            continue
        if c == "(":
            stack.append({"repeated": False, "alternation": False})
            last_atom = None
            i += 1
            continue
        if c == ")":
            if stack:
                last_atom = stack.pop()
            i += 1
            continue
        if c == "|":
            if stack:
                stack[-1]["alternation"] = True
            last_atom = None
            i += 1
            continue

        is_quantifier = c in "*+" or (c == "?" and last_atom is not None)
        if c == "{" and last_atom is not None:
            end = pattern.find("}", i + 1)
            if end != -1 and re.fullmatch(r"[0-9]+(?:,[0-9]*)?", pattern[i + 1:end]):
                is_quantifier = True
                i = end
        if is_quantifier:
            if last_atom and (last_atom["repeated"] or last_atom["alternation"]):
                raise RegexTimeout("regex nested repetition/alternation is not allowed")
            if stack:
                stack[-1]["repeated"] = True
            last_atom = {"repeated": True, "alternation": False}
            i += 1
            continue

        # Group-extension punctuation such as ``?:``/``?=`` is not an atom.
        if c == "?" and last_atom is None:
            i += 1
            continue
        last_atom = {"repeated": False, "alternation": False}
        i += 1


@contextmanager
def _regex_deadline(seconds: float):
    """Interrupt the wrapped block with RegexTimeout after `seconds`.

    Uses SIGALRM/setitimer in the current (main) thread only — this is a
    single-threaded hook process on macOS/POSIX, so that is sufficient. The
    previous handler and any previously-armed itimer are restored exactly on
    exit (best-effort remaining-time accounting for a pre-existing itimer,
    which in practice is never armed in this process).
    """
    if not _HAS_SIGALRM or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _handler(signum, frame):
        raise RegexTimeout(f"regex evaluation exceeded {seconds}s deadline")

    old_handler = signal.getsignal(signal.SIGALRM)
    old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
    start = time.monotonic()
    signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)  # cancel ours first
        signal.signal(signal.SIGALRM, old_handler)
        if old_delay > 0:
            remaining = old_delay - (time.monotonic() - start)
            signal.setitimer(signal.ITIMER_REAL, max(remaining, 0.0001), old_interval)


def _safe_regex_op(op, pattern, text, timeout, max_pattern_len, max_input_len,
                   *, preflight=True):
    if pattern is None or text is None:
        return None
    if len(pattern) > max_pattern_len:
        raise RegexLimitError(f"regex pattern exceeds max length ({max_pattern_len} chars)")
    if len(text) > max_input_len:
        raise RegexLimitError(f"regex input text exceeds max length ({max_input_len} chars)")
    if preflight:
        _reject_unsafe_regex_shape(pattern)
    with _regex_deadline(timeout):
        return op(pattern, text)


def safe_search(
    pattern: str,
    text: str,
    *,
    timeout: float = REGEX_SEARCH_TIMEOUT_SECONDS,
    max_pattern_len: int = MAX_REGEX_PATTERN_CHARS,
    max_input_len: int = MAX_CONTENT_CHARS,
    flags: int = 0,
):
    op = (lambda p, t: re.search(p, t, flags)) if flags else re.search
    return _safe_regex_op(op, pattern, text, timeout, max_pattern_len, max_input_len)


def safe_match(
    pattern: str,
    text: str,
    *,
    timeout: float = REGEX_SEARCH_TIMEOUT_SECONDS,
    max_pattern_len: int = _MAX_TRANSLATED_REGEX_CHARS,
    max_input_len: int = MAX_PATH_CHARS,
):
    # safe_match is private-in-practice and receives only regexes translated by
    # _glob_to_regex from the restricted glob grammar. That translation can
    # contain a safe quantified non-capturing group, so do not run the
    # user-regex structural preflight against it.
    return _safe_regex_op(re.match, pattern, text, timeout, max_pattern_len,
                          max_input_len, preflight=False)


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------
@dataclass
class Rule:
    name: str
    description: str = ""
    count: int = 0
    enabled: bool = True
    scope: str = ""
    why: str = ""
    how_to_apply: str = ""
    excuse: str = ""
    specs: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "count": self.count,
            "enabled": self.enabled,
            "scope": self.scope,
            "why": self.why,
            "how_to_apply": self.how_to_apply,
            "excuse": self.excuse,
            "specs": list(self.specs),
            "evidence": list(self.evidence),
            "violations": list(self.violations),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Spec validation (strict, declarative-only)
# ---------------------------------------------------------------------------
def validate_specs(raw) -> list:
    """Validate a spec object or list of spec objects; return a normalized list.

    Raises ValueError on anything not on the declarative allow-list. This is the
    only gate between user input and the enforcement engine, so it is strict:
    unknown keys, unknown events, invalid severities, and uncompilable regexes
    are all rejected. There is intentionally no key that carries a shell command.
    """
    if isinstance(raw, dict):
        specs = [raw]
    elif isinstance(raw, list):
        specs = raw
    else:
        raise ValueError("spec must be a JSON object or a list of objects")
    if not specs:
        raise ValueError("no specs supplied")
    out = []
    for i, spec in enumerate(specs):
        out.append(_validate_one(spec, i))
    return out


def _validate_one(spec, i: int) -> dict:
    where = f"spec[{i}]"
    if not isinstance(spec, dict):
        raise ValueError(f"{where} must be a JSON object")
    event = spec.get("event")
    if event not in EVENTS:
        raise ValueError(f"{where}.event must be one of {EVENTS}, got {event!r}")
    allowed = _COMMON_KEYS | _EVENT_KEYS[event]
    unknown = set(spec) - allowed
    if unknown:
        raise ValueError(f"{where} has unsupported key(s): {sorted(unknown)}")

    sev = spec.get("severity")
    if sev is not None and sev not in SEVERITIES:
        raise ValueError(f"{where}.severity must be one of {SEVERITIES}, got {sev!r}")

    for k in _REGEX_KEYS:
        if k in spec:
            v = spec[k]
            if not isinstance(v, str) or not v:
                raise ValueError(f"{where}.{k} must be a non-empty regex string")
            if len(v) > MAX_REGEX_PATTERN_CHARS:
                raise ValueError(
                    f"{where}.{k} exceeds max regex length ({MAX_REGEX_PATTERN_CHARS} chars)"
                )
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"{where}.{k} is not a valid regex: {e}") from e

    _STRING_LIMITS = {
        "message": MAX_MESSAGE_CHARS,
        "scope": MAX_SCOPE_CHARS,
        "path_glob": MAX_GLOB_PATTERN_CHARS,
        "exclude_glob": MAX_GLOB_PATTERN_CHARS,
        "absent_sibling": MAX_SIBLING_TEMPLATE_CHARS,
    }
    for k in ("message", "scope", "path_glob", "exclude_glob", "absent_sibling"):
        if k in spec:
            v = spec[k]
            if not isinstance(v, str):
                raise ValueError(f"{where}.{k} must be a string")
            limit = _STRING_LIMITS[k]
            if len(v) > limit:
                raise ValueError(f"{where}.{k} exceeds max length ({limit} chars)")
    if "absent_sibling" in spec:
        _validate_sibling_template(spec["absent_sibling"], where)

    # Per-event minimum-condition requirements so a spec always means something.
    if event == "pre_bash":
        if "when" not in spec and "forbid_regex" not in spec:
            raise ValueError(f"{where} (pre_bash) needs 'when' or 'forbid_regex'")
    elif event == "stop_check":
        if not ({"require_regex", "forbid_regex", "absent_sibling"} & set(spec)):
            raise ValueError(
                f"{where} (stop_check) needs at least one of "
                "require_regex/forbid_regex/absent_sibling"
            )
    else:  # pre_edit: a bare path_glob (forbid touching that path) is valid.
        if not ({"path_glob", "require_regex", "forbid_regex", "absent_sibling"} & set(spec)):
            raise ValueError(
                f"{where} (pre_edit) needs path_glob or a content/sibling condition"
            )
    return dict(spec)


_ALLOWED_SIBLING_FIELDS = {"stem", "name", "suffix", "dir", "parent"}


def _validate_sibling_template(template: str, where: str) -> None:
    # Only the known {field} placeholders may appear — no arbitrary format spec.
    for m in re.finditer(r"\{([^}]*)\}", template):
        field_name = m.group(1)
        if field_name not in _ALLOWED_SIBLING_FIELDS:
            raise ValueError(
                f"{where}.absent_sibling uses unknown placeholder {{{field_name}}}; "
                f"allowed: {sorted(_ALLOWED_SIBLING_FIELDS)}"
            )


# ---------------------------------------------------------------------------
# Feedback rule store (source of truth)
# ---------------------------------------------------------------------------
class FeedbackStore:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir) if data_dir else config.data_dir()
        self.log_path = self.dir / "feedback.jsonl"
        self.lock_path = self.dir / "feedback.lock"

    # ---- locking -----------------------------------------------------------
    @contextmanager
    def _locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            yield

    # ---- log I/O -----------------------------------------------------------
    def _append_event(self, event: dict) -> None:
        event.setdefault("ts", _iso(_now()))
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _read_events(self) -> list:
        if not self.log_path.exists():
            return []
        events = []
        with open(self.log_path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise FeedbackDataError(
                        f"corrupt feedback event at {self.log_path}:{lineno}: {e}"
                    ) from e
        return events

    # ---- state derivation --------------------------------------------------
    def derive(self) -> dict:
        state: dict = {}
        for ev in self._read_events():
            t = ev.get("type")
            name = ev.get("name")
            if not name:
                continue
            if t == "record":
                rule = state.get(name)
                ts = ev.get("ts", "")
                if rule is None:
                    rule = Rule(name=name, created_at=ts)
                    state[name] = rule
                # Latest-wins on descriptive fields (only if provided).
                for f_ in ("description", "scope", "why", "how_to_apply", "excuse"):
                    if ev.get(f_):
                        setattr(rule, f_, ev[f_])
                evid = ev.get("evidence")
                # Distinct-evidence count. Duplicate ids never inflate the count
                # (defense in depth; writes also reject duplicates up front).
                if evid and evid not in rule.evidence:
                    rule.evidence.append(evid)
                rule.count = len(rule.evidence)
                rule.updated_at = ts
            elif t == "configure":
                rule = state.get(name)
                if rule is None:
                    continue
                rule.specs = list(ev.get("specs", []))
                rule.updated_at = ev.get("ts", rule.updated_at)
            elif t == "enable":
                rule = state.get(name)
                if rule is not None:
                    rule.enabled = True
                    rule.updated_at = ev.get("ts", rule.updated_at)
            elif t == "disable":
                rule = state.get(name)
                if rule is not None:
                    rule.enabled = False
                    rule.updated_at = ev.get("ts", rule.updated_at)
            elif t == "violation":
                rule = state.get(name)
                if rule is not None:
                    rule.violations.append(
                        {
                            "ts": ev.get("ts", ""),
                            "event": ev.get("event", ""),
                            "detail": ev.get("detail", ""),
                            "session_id": ev.get("session_id", ""),
                        }
                    )
        return state

    def get(self, name: str) -> Rule | None:
        return self.derive().get(name)

    def list(self) -> list:
        rules = list(self.derive().values())
        rules.sort(key=lambda r: r.name)
        return rules

    # ---- mutations ---------------------------------------------------------
    def record(
        self,
        name: str,
        description: str,
        evidence: str,
        scope: str = "",
        why: str = "",
        how_to_apply: str = "",
        excuse: str = "",
    ) -> Rule:
        with self._locked():
            if not name or not description:
                raise ValueError("record requires --name and --description")
            if not evidence:
                raise ValueError("record requires an --evidence identifier (the proof it happened)")
            state = self.derive()
            existing = state.get(name)
            if existing and evidence in existing.evidence:
                raise ValueError(
                    f"evidence {evidence!r} is already recorded for {name!r}; a repeat "
                    "must cite a NEW, distinct occurrence — count is not inflated"
                )
            self._append_event(
                {
                    "type": "record",
                    "name": name,
                    "description": description,
                    "evidence": evidence,
                    "scope": scope,
                    "why": why,
                    "how_to_apply": how_to_apply,
                    "excuse": excuse,
                }
            )
            return self.derive()[name]

    def configure(self, name: str, specs) -> Rule:
        with self._locked():
            state = self.derive()
            if name not in state:
                raise ValueError(f"unknown feedback rule: {name}")
            validated = validate_specs(specs)
            self._append_event({"type": "configure", "name": name, "specs": validated})
            return self.derive()[name]

    def set_enabled(self, name: str, enabled: bool) -> Rule:
        with self._locked():
            state = self.derive()
            if name not in state:
                raise ValueError(f"unknown feedback rule: {name}")
            self._append_event(
                {"type": "enable" if enabled else "disable", "name": name}
            )
            return self.derive()[name]

    def record_violation(self, name: str, event: str, detail: str, session_id: str = "") -> None:
        """Append a violation event. NEVER changes ``count``."""
        with self._locked():
            self._append_event(
                {
                    "type": "violation",
                    "name": name,
                    "event": event,
                    "detail": detail,
                    "session_id": session_id,
                }
            )


# ---------------------------------------------------------------------------
# Severity resolution
# ---------------------------------------------------------------------------
def resolve_severity(rule: Rule, spec: dict) -> str:
    """warn|pause|deny for a matched spec.

    An explicit ``severity`` on the spec wins. Otherwise it is derived from how
    many distinct times the human has had to give this feedback: 1-2 warn,
    3-4 pause, >=5 deny.
    """
    sev = spec.get("severity")
    if sev in SEVERITIES:
        return sev
    if rule.count >= AUTO_DENY_AT:
        return DENY
    if rule.count >= AUTO_PAUSE_AT:
        return PAUSE
    return WARN


# ---------------------------------------------------------------------------
# Glob / path helpers (predictable, no `*` crossing `/`)
# ---------------------------------------------------------------------------
def _glob_to_regex(pattern: str) -> str:
    """Translate a glob to an anchored regex. `**` spans dirs, `*`/`?` do not."""
    i, n = 0, len(pattern)
    out = ["(?s)^"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return "".join(out)


def glob_match(path: str, pattern: str) -> bool:
    if len(pattern) > MAX_GLOB_PATTERN_CHARS:
        raise RegexLimitError(
            f"path_glob/exclude_glob exceeds max length ({MAX_GLOB_PATTERN_CHARS} chars)"
        )
    # Glob syntax is provider-neutral and always uses '/'. Normalize native
    # Windows paths before matching so **/test_*.py exclusions behave exactly
    # as they do on POSIX.
    path = str(path).replace("\\", "/")
    if len(path) > MAX_PATH_CHARS:
        raise RegexLimitError(f"path exceeds max length ({MAX_PATH_CHARS} chars)")
    return safe_match(_glob_to_regex(pattern), path) is not None


def expand_sibling(template: str, target: Path) -> Path:
    p = Path(target)
    subs = {
        "stem": p.stem,
        "name": p.name,
        "suffix": p.suffix,
        "dir": str(p.parent),
        "parent": p.parent.name,
    }
    rel = template.format(**subs)
    sib = Path(rel)
    if not sib.is_absolute():
        sib = p.parent / rel
    return sib


def _within_root(path: Path, root: Path) -> bool:
    """True iff `path`, resolved, stays inside `root` (reject traversal)."""
    try:
        resolved = path.resolve()
        root_r = root.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(root_r)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Edit-target extraction (apply_patch / Edit / Write)
# ---------------------------------------------------------------------------
def extract_edit_targets(tool_name: str, tool_input) -> list:
    """Return [(path_str, proposed_content_str)] the tool would write.

    Only understands the file-editing tools; it never guesses what a Bash
    command writes. ``proposed_content`` is the new text (Write body / Edit
    replacement / apply_patch added lines) so content conditions can be checked
    before the write lands.
    """
    name = (tool_name or "").lower()
    targets: list = []
    if isinstance(tool_input, str):
        patch = tool_input
        ti: dict = {}
    else:
        ti = tool_input if isinstance(tool_input, dict) else {}
        patch = ti.get("patch") or ti.get("input") or ""

    if "apply_patch" in name or (patch and "*** Begin Patch" in str(patch)):
        targets.extend(_parse_apply_patch(str(patch)))
        if targets:
            return targets

    # Write-style
    path = ti.get("file_path") or ti.get("path") or ti.get("filePath")
    if path:
        content = ti.get("content")
        if content is None:
            content = ti.get("new_string") or ti.get("new_str") or ""
        targets.append((str(path), str(content)))
    return targets


def _parse_apply_patch(patch: str) -> list:
    targets = []
    current = None
    added: list = []
    for line in patch.splitlines():
        m = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)$", line)
        if m:
            if current is not None:
                targets.append((current, "\n".join(added)))
            current = m.group(2).strip()
            added = []
            continue
        if current is not None and line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    if current is not None:
        targets.append((current, "\n".join(added)))
    return targets


# ---------------------------------------------------------------------------
# Enforcement engine (pure; returns violation reasons)
# ---------------------------------------------------------------------------
def _scope_ok(spec: dict, cwd: str) -> bool:
    scope = spec.get("scope")
    if not scope:
        return True
    return scope in (cwd or "")


def _msg(spec: dict, rule: Rule, default: str) -> str:
    return spec.get("message") or rule.description or default


def _bounded(text: str | None, limit: int, what: str) -> str:
    text = text or ""
    if len(text) > limit:
        raise RegexLimitError(f"{what} exceeds max length ({limit} chars); refusing to evaluate")
    return text


def eval_pre_bash(rule: Rule, spec: dict, command: str, cwd: str = "") -> str | None:
    """Return a violation reason if this bash command trips the spec, else None."""
    if spec.get("event") != "pre_bash":
        return None
    if not _scope_ok(spec, cwd):
        return None
    command = _bounded(command, MAX_COMMAND_CHARS, "pre_bash command")
    unless = spec.get("unless")
    if unless and safe_search(unless, command):
        return None
    triggered = False
    if "when" in spec and safe_search(spec["when"], command):
        triggered = True
    if "forbid_regex" in spec and safe_search(spec["forbid_regex"], command):
        triggered = True
    if not triggered:
        return None
    return _msg(spec, rule, f"command matches a rule you set ({rule.name})")


def eval_pre_edit(rule: Rule, spec: dict, path: str, content: str, cwd: str = "") -> str | None:
    if spec.get("event") != "pre_edit":
        return None
    if not _scope_ok(spec, cwd):
        return None
    if "path_glob" in spec and not glob_match(path, spec["path_glob"]):
        return None
    if "exclude_glob" in spec and glob_match(path, spec["exclude_glob"]):
        return None
    content = _bounded(content, MAX_CONTENT_CHARS, "pre_edit proposed content")
    unless = spec.get("unless")
    if unless and safe_search(unless, content):
        return None

    reasons = []
    if "absent_sibling" in spec:
        sib = expand_sibling(spec["absent_sibling"], Path(path))
        if not sib.exists():
            reasons.append(f"required sibling missing: {sib}")
    if "forbid_regex" in spec and safe_search(spec["forbid_regex"], content):
        reasons.append("content matches a forbidden pattern")
    if "require_regex" in spec and not safe_search(spec["require_regex"], content):
        reasons.append("content is missing a required pattern")

    has_condition = any(k in spec for k in ("absent_sibling", "forbid_regex", "require_regex"))
    if not has_condition:
        # A bare path_glob rule: editing this path at all is the violation.
        return _msg(spec, rule, f"editing {path} is restricted by rule {rule.name}")
    if not reasons:
        return None
    return _msg(spec, rule, f"{path}: " + "; ".join(reasons))


def eval_stop_check(
    rule: Rule, spec: dict, path: str, root: Path, cwd: str = ""
) -> str | None:
    """Evaluate a stop_check spec against ONE tracked file's on-disk content.

    Refuses to read outside ``root`` (path-traversal guard). Returns a reason or
    None. Never runs a shell command — only declarative content conditions.
    """
    if spec.get("event") != "stop_check":
        return None
    if not _scope_ok(spec, cwd):
        return None
    p = Path(path)
    if "path_glob" in spec and not glob_match(path, spec["path_glob"]):
        return None
    if "exclude_glob" in spec and glob_match(path, spec["exclude_glob"]):
        return None
    if not _within_root(p, root):
        return None  # do not read outside the session root

    reasons = []
    if "absent_sibling" in spec:
        sib = expand_sibling(spec["absent_sibling"], p)
        if not sib.exists():
            reasons.append(f"required sibling missing: {sib}")

    needs_content = ("require_regex" in spec) or ("forbid_regex" in spec)
    content = ""
    if needs_content:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None  # unreadable / gone — nothing to assert
        content = _bounded(content, MAX_CONTENT_CHARS, "stop_check file content")
        unless = spec.get("unless")
        if unless and safe_search(unless, content):
            return None
        if "require_regex" in spec and not safe_search(spec["require_regex"], content):
            reasons.append("content is missing a required pattern")
        if "forbid_regex" in spec and safe_search(spec["forbid_regex"], content):
            reasons.append("content matches a forbidden pattern")

    if not reasons:
        return None
    return _msg(spec, rule, f"{path}: " + "; ".join(reasons))


# ---------------------------------------------------------------------------
# Runtime session state (disposable cache): changed files, pause approvals,
# Stop-loop attempts.
# ---------------------------------------------------------------------------
class FeedbackState:
    """flock'd load-mutate-save over feedback-state.json.

    This is a *cache*: if it is unreadable we start empty (and the caller
    surfaces a warning) rather than failing closed — it holds no rule data,
    only which files changed this session and short-lived approval nonces.
    """

    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir) if data_dir else config.data_dir()
        self.path = self.dir / "feedback-state.json"
        self.lock_path = self.dir / "feedback-state.lock"

    @contextmanager
    def locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            state = self._load()
            yield state
            self._save(state)

    def _load(self) -> dict:
        if not self.path.exists():
            return self._fresh()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return self._fresh()
        if not raw.strip():
            return self._fresh()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Disposable cache: start clean but let the caller warn on stderr.
            return self._fresh(corrupt=True)
        if not isinstance(data, dict):
            return self._fresh(corrupt=True)
        data.setdefault("sessions", {})
        data.setdefault("approvals", [])
        data.setdefault("admin_approvals", [])
        data.setdefault("stop_attempts", {})
        data.setdefault("candidates", [])
        data.setdefault("mem_candidates", [])
        return data

    @staticmethod
    def _fresh(corrupt: bool = False) -> dict:
        return {
            "sessions": {},
            "approvals": [],
            "admin_approvals": [],
            "stop_attempts": {},
            "candidates": [],
            "mem_candidates": [],
            "_corrupt": corrupt,
        }

    def _save(self, state: dict) -> None:
        state.pop("_corrupt", None)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        tmp.replace(self.path)


# ---- tracked-file helpers (operate on a loaded state dict) -----------------
def _resolve_within_cwd(cwd: str, p: str) -> Path | None:
    """Resolve `p` against `cwd` (NOT the hook process's os.getcwd()).

    A relative `p` is joined onto `cwd` before resolving; an absolute `p` is
    resolved as-is. Symlinks are followed (`Path.resolve()`), and the FINAL
    real path must land inside the resolved `cwd` — otherwise this returns
    None so the caller never tracks (and Stop never reads) a path outside the
    session's own working directory, whether via a relative escape (`../..`),
    an absolute path elsewhere, or a symlink that points outside `cwd`.
    """
    try:
        base = Path(cwd).resolve() if cwd else Path.cwd()
    except OSError:
        return None
    raw = Path(p)
    candidate = raw if raw.is_absolute() else base / raw
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return resolved


def track_changed_files(state: dict, session_id: str, cwd: str, paths: list) -> None:
    sess = state.setdefault("sessions", {}).setdefault(session_id, {"cwd": cwd, "files": []})
    if cwd:
        sess["cwd"] = cwd
    effective_cwd = sess.get("cwd") or cwd
    files = sess.setdefault("files", [])
    for p in paths:
        if not p:
            continue
        resolved = _resolve_within_cwd(effective_cwd, p)
        if resolved is None:
            continue  # outside cwd, or unresolvable — never tracked
        rp = str(resolved)
        if rp not in files:
            files.append(rp)


def tracked_files(state: dict, session_id: str) -> list:
    return list(state.get("sessions", {}).get(session_id, {}).get("files", []))


def session_cwd(state: dict, session_id: str) -> str:
    return state.get("sessions", {}).get(session_id, {}).get("cwd", "")


# ---- pause-approval nonce lifecycle ----------------------------------------
def new_nonce() -> str:
    return secrets.token_hex(8)


def _prune_approvals(state: dict, now: float, ttl: int) -> None:
    state["approvals"] = [
        a for a in state.get("approvals", []) if now - a.get("created_at", 0) <= ttl
    ]


def request_pause(
    state: dict, session_id: str, fingerprint: str, rule: str, now: float, ttl: int
) -> str:
    """Create/refresh a PENDING approval; return its nonce.

    Keyed by (session, tool fingerprint, rule). A fresh nonce is minted each
    time so a stale one can't be replayed.
    """
    _prune_approvals(state, now, ttl)
    approvals = state.setdefault("approvals", [])
    for a in approvals:
        if (
            a["session_id"] == session_id
            and a["fingerprint"] == fingerprint
            and a["rule"] == rule
            and a.get("status") == "pending"
        ):
            a["nonce"] = new_nonce()
            a["created_at"] = now
            return a["nonce"]
    nonce = new_nonce()
    approvals.append(
        {
            "nonce": nonce,
            "session_id": session_id,
            "fingerprint": fingerprint,
            "rule": rule,
            "status": "pending",
            "created_at": now,
        }
    )
    return nonce


def approve_nonce(state: dict, session_id: str, nonce: str, now: float, ttl: int) -> bool:
    """Promote a pending approval to 'approved' for an EXACT nonce match.

    Only ever called from the UserPromptSubmit hook (the model cannot forge a
    user prompt), and only within the same session. Returns True if something
    was approved.
    """
    _prune_approvals(state, now, ttl)
    ok = False
    for a in state.get("approvals", []):
        if (
            a["session_id"] == session_id
            and a["nonce"] == nonce
            and a.get("status") == "pending"
        ):
            a["status"] = "approved"
            a["created_at"] = now  # restart TTL for the one-shot consume window
            ok = True
    return ok


def consume_permit(
    state: dict, session_id: str, fingerprint: str, rule: str, now: float, ttl: int
) -> bool:
    """Consume an APPROVED permit for this exact (session, fingerprint, rule).

    One-shot: the permit is removed on use, so a single approval lets exactly
    one retry of the same tool call through. Expired permits do not count.
    """
    _prune_approvals(state, now, ttl)
    approvals = state.get("approvals", [])
    for i, a in enumerate(approvals):
        if (
            a["session_id"] == session_id
            and a["fingerprint"] == fingerprint
            and a["rule"] == rule
            and a.get("status") == "approved"
        ):
            approvals.pop(i)
            return True
    return False


# ---- administrative-approval nonce lifecycle --------------------------------
# A SEPARATE pool from the rule-pause approvals above. This is the one-shot
# approval for the built-in administrative gate on `exi feedback
# configure/disable/enable` (see feedback_hook._handle_admin_gate) — an
# administration approval, not a rule permit. Keeping it in its own pool means
# an ALLOW_FEEDBACK:<nonce> can never satisfy an admin gate and vice versa,
# independent of the (already-distinct) marker prefixes.
def _prune_admin_approvals(state: dict, now: float, ttl: int) -> None:
    state["admin_approvals"] = [
        a for a in state.get("admin_approvals", []) if now - a.get("created_at", 0) <= ttl
    ]


def request_admin_pause(
    state: dict, session_id: str, fingerprint: str, now: float, ttl: int
) -> str:
    """Create/refresh a PENDING admin approval; return its nonce.

    Keyed by (session, exact tool fingerprint) only — there is no "rule" for
    the built-in gate.
    """
    _prune_admin_approvals(state, now, ttl)
    approvals = state.setdefault("admin_approvals", [])
    for a in approvals:
        if (
            a["session_id"] == session_id
            and a["fingerprint"] == fingerprint
            and a.get("status") == "pending"
        ):
            a["nonce"] = new_nonce()
            a["created_at"] = now
            return a["nonce"]
    nonce = new_nonce()
    approvals.append(
        {
            "nonce": nonce,
            "session_id": session_id,
            "fingerprint": fingerprint,
            "status": "pending",
            "created_at": now,
        }
    )
    return nonce


def approve_admin_nonce(state: dict, session_id: str, nonce: str, now: float, ttl: int) -> bool:
    """Promote a pending ADMIN approval for an EXACT nonce match.

    Only ever called from the UserPromptSubmit hook, same as `approve_nonce`.
    """
    _prune_admin_approvals(state, now, ttl)
    ok = False
    for a in state.get("admin_approvals", []):
        if (
            a["session_id"] == session_id
            and a["nonce"] == nonce
            and a.get("status") == "pending"
        ):
            a["status"] = "approved"
            a["created_at"] = now
            ok = True
    return ok


def consume_admin_permit(
    state: dict, session_id: str, fingerprint: str, now: float, ttl: int
) -> bool:
    """Consume an APPROVED admin permit for this exact (session, fingerprint). One-shot."""
    _prune_admin_approvals(state, now, ttl)
    approvals = state.get("admin_approvals", [])
    for i, a in enumerate(approvals):
        if (
            a["session_id"] == session_id
            and a["fingerprint"] == fingerprint
            and a.get("status") == "approved"
        ):
            approvals.pop(i)
            return True
    return False


# ---- built-in administrative-mutation command matcher -----------------------
# Fixed, non-user-configurable pattern (not a rule spec) recognizing CLI
# invocations that would weaken/change enforcement: `exi feedback
# configure/disable/enable` (record is intentionally excluded — it only adds
# human evidence and must stay usable without a gate). Matches `bin/exi`
# (relative or absolute), a bare `exi` on PATH, and `python -m exi.exicli`.
# This is a conservative belt, not a sandbox: same-user arbitrary code/source
# tampering (editing this file, calling exi's Python API directly, etc.)
# cannot be made cryptographically impossible — see feedback_hook module
# docstring and README. False positives just cost one extra confirmation.
_ADMIN_CMD_PATTERN = r"\bexi(?:\.exicli)?\b\s+feedback\s+(?:configure|disable|enable)\b"


def matches_admin_mutation(command: str | None) -> bool:
    if not command:
        return False
    text = command if len(command) <= MAX_COMMAND_CHARS else command[:MAX_COMMAND_CHARS]
    try:
        return safe_search(_ADMIN_CMD_PATTERN, text, flags=re.IGNORECASE) is not None
    except RegexEvalError:
        # This IS the security boundary: fail CLOSED (treat as a match) rather
        # than let a detection glitch silently let a mutation through. Every
        # other regex evaluation in this module fails OPEN on purpose; this
        # one fixed, non-user-controlled check is the deliberate exception.
        return True


# ---- Stop-loop attempt counter ---------------------------------------------
def stop_attempt_key(session_id: str, turn_id: str) -> str:
    """Keyed by session+turn ONLY.

    Deliberately does NOT fold in which rule(s) triggered the block: if it
    did, an agent could dodge the Stop-loop cap by alternating which
    configured rule fires each attempt (each distinct rule-name set would get
    its own fresh counter), producing unlimited blocks in one turn. Keying by
    session+turn means the cap (see `HARD_MAX_STOP_BLOCKS`) is on the TURN,
    not on any particular rule combination.
    """
    return f"{session_id}\x1f{turn_id}"


def bump_stop_attempt(state: dict, key: str) -> int:
    attempts = state.setdefault("stop_attempts", {})
    attempts[key] = attempts.get(key, 0) + 1
    return attempts[key]


# ---------------------------------------------------------------------------
# Pending feedback-candidate lifecycle (disposable session-state cache)
#
# A candidate records ONLY: an internal id, the prompt HASH (never the body),
# session/turn, detection cue categories, a status, and a creation timestamp.
# The raw prompt is already in the model's context and is never persisted here.
# Statuses: pending -> resolved | dismissed | abandoned. `abandoned` is the
# terminal state a Stop cap leaves behind for later human audit — it never
# blocks again, so the loop is bounded across turns as well as within one.
# ---------------------------------------------------------------------------
CANDIDATE_PENDING = "pending"
CANDIDATE_RESOLVED = "resolved"
CANDIDATE_DISMISSED = "dismissed"
CANDIDATE_ABANDONED = "abandoned"


def prune_candidates(state: dict, now: float, ttl: int) -> None:
    """Drop candidates older than the TTL regardless of status (cache hygiene)."""
    state["candidates"] = [
        c for c in state.get("candidates", []) if now - c.get("created_at", 0) <= ttl
    ]


def get_candidate(state: dict, candidate_id: str) -> dict | None:
    for c in state.get("candidates", []):
        if c.get("id") == candidate_id:
            return c
    return None


def upsert_candidate(
    state: dict,
    candidate_id: str,
    session_id: str,
    turn_id: str,
    p_hash: str,
    cues: list,
    now: float,
    ttl: int,
) -> str:
    """Ensure a pending candidate exists; return its current status.

    Idempotent: a second detection of the same (session, turn, prompt-hash) —
    same id — never creates a duplicate and never resurrects an already
    resolved/dismissed/abandoned candidate.
    """
    prune_candidates(state, now, ttl)
    existing = get_candidate(state, candidate_id)
    if existing is not None:
        return existing.get("status", CANDIDATE_PENDING)
    state.setdefault("candidates", []).append(
        {
            "id": candidate_id,
            "hash": p_hash,
            "session_id": session_id,
            "turn_id": turn_id,
            "cues": list(cues),
            "status": CANDIDATE_PENDING,
            "created_at": now,
        }
    )
    return "created"


def pending_candidates(state: dict, session_id: str, now: float, ttl: int) -> list:
    """Non-expired pending candidates for a session (drives Stop blocking)."""
    prune_candidates(state, now, ttl)
    return [
        c
        for c in state.get("candidates", [])
        if c.get("session_id") == session_id and c.get("status") == CANDIDATE_PENDING
    ]


def set_candidate_status(state: dict, candidate_id: str, status: str, **extra) -> bool:
    c = get_candidate(state, candidate_id)
    if c is None:
        return False
    c["status"] = status
    for k, v in extra.items():
        c[k] = v
    return True


def abandon_pending(state: dict, session_id: str, now: float, ttl: int) -> list:
    """Mark this session's pending candidates abandoned; return their ids."""
    ids = []
    for c in pending_candidates(state, session_id, now, ttl):
        c["status"] = CANDIDATE_ABANDONED
        ids.append(c["id"])
    return ids


# ---------------------------------------------------------------------------
# Durable-memory candidate lifecycle (disposable session-state cache)
#
# Distinct from the feedback candidate above and stored under its own
# ``mem_candidates`` key. A memory candidate is opened on EVERY normal turn (no
# trigger-word detector — the model decides what is worth remembering), so it is
# intentionally lightweight and NEVER Stop-blocks: an unresolved one just
# expires. It records ONLY: an internal id, the prompt HASH (never the body),
# session/turn, a status, a creation timestamp, and the set of claim
# fingerprints already resolved from it (so resolving the same memory twice from
# one turn cannot inflate evidence or create noise). Statuses: pending ->
# resolved | dismissed. `resolved` is non-terminal here — a single turn may
# yield several DISTINCT memories, so a resolved candidate stays usable for a
# different claim within its TTL. `dismissed` is purely for auditability and is
# never required for an ordinary turn.
# ---------------------------------------------------------------------------
MEM_PENDING = "pending"
MEM_RESOLVED = "resolved"
MEM_DISMISSED = "dismissed"
MAX_MEM_CANDIDATES = 2048
MAX_MEMORIES_PER_CANDIDATE = 8


def prune_mem_candidates(state: dict, now: float, ttl: int) -> None:
    """Drop memory candidates older than the TTL regardless of status."""
    kept = [
        c for c in state.get("mem_candidates", []) if now - c.get("created_at", 0) <= ttl
    ]
    kept.sort(key=lambda c: c.get("created_at", 0))
    state["mem_candidates"] = kept[-MAX_MEM_CANDIDATES:]


def get_mem_candidate(state: dict, candidate_id: str) -> dict | None:
    for c in state.get("mem_candidates", []):
        if c.get("id") == candidate_id:
            return c
    return None


def upsert_mem_candidate(
    state: dict,
    candidate_id: str,
    session_id: str,
    turn_id: str,
    p_hash: str,
    now: float,
    ttl: int,
) -> str:
    """Ensure a pending memory candidate exists; return its current status.

    Idempotent per (session, turn, prompt-hash) id: a re-detection never
    duplicates and never resurrects a resolved/dismissed candidate's lifecycle
    (it just returns its status). No prompt body is ever stored.
    """
    prune_mem_candidates(state, now, ttl)
    existing = get_mem_candidate(state, candidate_id)
    if existing is not None:
        return existing.get("status", MEM_PENDING)
    state.setdefault("mem_candidates", []).append(
        {
            "id": candidate_id,
            "hash": p_hash,
            "session_id": session_id,
            "turn_id": turn_id,
            "status": MEM_PENDING,
            "resolved_claims": [],
            "created_at": now,
        }
    )
    return "created"


def set_mem_candidate_status(state: dict, candidate_id: str, status: str, **extra) -> bool:
    c = get_mem_candidate(state, candidate_id)
    if c is None:
        return False
    c["status"] = status
    for k, v in extra.items():
        c[k] = v
    return True


def mem_claim_already_resolved(candidate: dict, claim_fp: str) -> bool:
    return claim_fp in candidate.get("resolved_claims", [])


def mark_mem_claim_resolved(candidate: dict, claim_fp: str) -> None:
    resolved = candidate.setdefault("resolved_claims", [])
    if claim_fp not in resolved:
        resolved.append(claim_fp)
