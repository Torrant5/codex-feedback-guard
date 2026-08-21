"""Prompt extraction + a conservative, built-in corrective-feedback detector.

This module is deliberately dependency-free and side-effect-free: it never
touches disk, the network, an LLM, or any subprocess. It exists so the
``UserPromptSubmit`` feedback hook can, with **no extra model/API call**,

  1. pull the actual user prompt out of the several documented/common Codex
     payload shapes (``extract_prompt``), and
  2. cheaply decide whether that prompt *looks like* the user correcting a
     repeated mistake (``detect_feedback``) — in Japanese or English.

Nothing here persists the prompt. Callers hash it (``prompt_hash``) and derive
a stable, internal evidence id (``human_evidence_id``); the raw text only ever
lives in the model's own context, never in this project's durable data.

The detector is intentionally **high-precision / conservative**: a single
strong corrective cue ("二度と", "don't do that again", …) flags feedback, but
weak, ambiguous cues ("again", "また") require two *distinct* categories before
they count, so an ordinary question or a "see you again tomorrow" does not trip
it. Matches inside fenced or inline code are ignored so pasted code that merely
*contains* such words is not mistaken for a complaint.
"""
from __future__ import annotations

import hashlib
import re

# Hard cap on how much prompt text we will ever inspect / hash. A pathologically
# large paste must not turn a hook into a CPU sink; the head is representative
# enough for both detection and a stable hash.
MAX_PROMPT_CHARS = 20_000

# CJK ranges used for character-ngram features and code-vs-prose heuristics
# (Hiragana, Katakana, CJK Unified + Extension-A, compatibility ideographs).
_CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿"


# ---------------------------------------------------------------------------
# Prompt extraction (robust across payload shapes)
# ---------------------------------------------------------------------------
def _coerce(value) -> str:
    """Best-effort flatten of a prompt-ish value to a plain string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for k in ("text", "content", "prompt", "value"):
            s = _coerce(value.get(k))
            if s:
                return s
        return ""
    if isinstance(value, list):
        parts = [_coerce(v) for v in value]
        return " ".join(p for p in parts if p).strip()
    return ""


def extract_prompt(payload: dict) -> str:
    """Extract the user's prompt text from a Codex UserPromptSubmit payload.

    Handles the common documented shapes: a top-level ``prompt`` /
    ``user_prompt`` / ``message`` / ``input`` / ``text`` string (or an object /
    list carrying ``text``/``content``), and a ``messages``/``input`` list of
    role objects (the last user turn wins). Returns ``""`` when nothing
    prompt-like is present. Never raises.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("prompt", "user_prompt", "userPrompt", "message", "text", "content", "input"):
        if key in payload:
            s = _coerce(payload.get(key))
            if s:
                return s[:MAX_PROMPT_CHARS]
    for key in ("messages", "conversation", "input"):
        seq = payload.get(key)
        if isinstance(seq, list):
            for item in reversed(seq):
                if isinstance(item, dict):
                    role = item.get("role")
                    if role in (None, "user"):
                        s = _coerce(item.get("content") or item.get("text"))
                        if s:
                            return s[:MAX_PROMPT_CHARS]
    return ""


# ---------------------------------------------------------------------------
# Hashing / stable ids (no raw prompt ever stored)
# ---------------------------------------------------------------------------
def normalize_prompt(prompt: str) -> str:
    """Collapse whitespace + casefold so trivially different spacing/casing of
    the *same* complaint hashes identically (drives distinct-occurrence dedup)."""
    return re.sub(r"\s+", " ", (prompt or "").strip()).casefold()


def prompt_hash(prompt: str) -> str:
    """A short, stable, non-reversible id for a prompt's normalized text."""
    norm = normalize_prompt(prompt)[:MAX_PROMPT_CHARS]
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def candidate_id(session_id: str, turn_id: str, p_hash: str) -> str:
    """Internal id for a pending feedback candidate (session+turn+prompt hash).

    Derived here so neither the user nor the agent supplies it; the resolution
    CLI looks the candidate up by this id and derives the evidence id from the
    stored hash — the agent can never inject its own evidence identifier.
    """
    raw = f"{session_id}\x1f{turn_id}\x1f{p_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def human_evidence_id(p_hash: str) -> str:
    """Evidence id used when a candidate is recorded into a rule.

    Namespaced by prompt hash so the SAME corrective prompt can never inflate a
    rule's count twice (the store rejects a duplicate evidence id), while a
    genuinely different complaint (distinct hash) counts as a new occurrence.
    """
    return f"hp:{p_hash}"


# ---------------------------------------------------------------------------
# Code stripping (so words inside code don't read as a complaint)
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_RE = re.compile(r"`[^`]*`")


def strip_code(text: str) -> str:
    return _INLINE_RE.sub(" ", _FENCE_RE.sub(" ", text or ""))


# ---------------------------------------------------------------------------
# Corrective-feedback detector
#
# Each pattern maps to a coarse CATEGORY label (our own fixed vocabulary, never
# text lifted from the prompt) so stored "cues" cannot leak user content.
# STRONG cues are unambiguous corrections: any one flags feedback. WEAK cues are
# ambiguous ("again"/"また") and require two *distinct* categories to flag, so a
# lone ambiguous word never trips the detector.
# ---------------------------------------------------------------------------
_STRONG = [
    # --- Japanese ---
    (re.compile(r"二度と"), "prohibition"),
    (re.compile(r"やめて|止めて|やめろ|止めろ"), "stop-directive"),
    (re.compile(r"勝手に"), "unauthorized"),
    (re.compile(r"しないで"), "prohibition"),
    (re.compile(r"前に?も(?:言|伝|お願い|頼)"), "repetition"),
    (re.compile(r"何度も(?:言|伝)"), "repetition"),
    (re.compile(r"再三(?:言|伝|指摘|お願い|頼)"), "repetition"),
    (re.compile(r"また同じ"), "repetition"),
    (re.compile(r"そんな(?:面倒|めんどう)"), "refusal"),
    (re.compile(r"(?:あほ|アホ|馬鹿|バカ)なこと(?:を)?(?:言|や)って(?:い)?ない(?:よね|でしょう|だろ)"), "criticism"),
    (re.compile(r"(?:いかれてる|イカれてる|イかれてる|イカレてる)"), "criticism"),
    (re.compile(r"言った(?:よ|でしょ|はず|じゃん)"), "repetition"),
    (re.compile(r"絶対に[^\n]{0,80}?(?:するな|やるな|しないで)"), "prohibition"),
    # --- English --- (matched case-insensitively)
    (re.compile(r"i(?:'ve| have| already)?\s+(?:told|said)\s+you", re.I), "repetition"),
    (re.compile(r"i\s+said\s+(?:not\s+to|don'?t)", re.I), "prohibition"),
    (re.compile(r"(?:do\s+not|don'?t)\s+do\s+(?:that|this|it)\s+again", re.I), "prohibition"),
    (re.compile(r"stop\s+(?:doing|that)", re.I), "stop-directive"),
    (re.compile(r"never\s+do\s+(?:that|this|it)", re.I), "prohibition"),
    (re.compile(r"how\s+many\s+times", re.I), "repetition"),
    (re.compile(r"like\s+i\s+(?:said|told)", re.I), "repetition"),
]

_WEAK = [
    # --- Japanese ---
    (re.compile(r"また"), "repetition"),
    (re.compile(r"何度も"), "repetition"),
    (re.compile(r"(?:やって|し)ない で"), "prohibition"),
    # --- English ---
    (re.compile(r"\bagain\b", re.I), "repetition"),
    (re.compile(r"you\s+keep\b", re.I), "persistence"),
    (re.compile(r"why\s+(?:did|do)\s+you\b", re.I), "complaint"),
]


def detect_feedback(prompt: str) -> tuple[bool, list]:
    """Return (is_likely_feedback, sorted distinct cue categories).

    Conservative: one STRONG cue is enough; otherwise two DISTINCT weak
    categories are required. Code spans are ignored. Cues are category labels
    only — never substrings of the prompt.
    """
    text = strip_code(prompt or "")[:MAX_PROMPT_CHARS]
    if not text.strip():
        return False, []

    strong = {cat for pat, cat in _STRONG if pat.search(text)}
    if strong:
        return True, sorted(strong)

    weak = {cat for pat, cat in _WEAK if pat.search(text)}
    if len(weak) >= 2:
        return True, sorted(weak)
    return False, []
