"""Conservative secret/credential detector for durable-memory content.

A canonical memory claim, its scope/triggers, and any evidence identifiers are
authored by the active agent and then persisted to the local observation store.
They must never carry a live secret (API key, token, password, private key).
This module is a deliberately HIGH-PRECISION rejecter: it flags shapes that are
almost certainly credentials (known token prefixes, private-key headers, or a
secret-ish keyword immediately assigned a long opaque value) and leaves ordinary
prose alone. It runs with no network, no LLM, and no subprocess — pure
standard-library string matching, like the rest of this project.

The policy is *rejection*, not silent redaction: a claim that looks like it
contains a secret is refused so the model rewrites it without the secret, rather
than a mangled half-secret landing in durable memory. Callers use
``assert_no_secret`` (raises ``SecretDetected``) or ``find_secret`` (returns a
short, fixed-vocabulary reason string that never echoes the matched value).
"""
from __future__ import annotations

import re

# Bound the text we will scan so a huge paste can't turn this into a CPU sink.
_MAX_SCAN_CHARS = 20_000


class SecretDetected(ValueError):
    """Text contains a secret-like token and must not be persisted."""


# Known high-confidence credential shapes. Each maps to a coarse reason label —
# never the matched substring, so the reason itself can be logged/stored safely.
_TOKEN_PATTERNS = [
    (re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----"), "private-key block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS temporary access key id"),
    (re.compile(r"\bgh[posru]_[0-9A-Za-z]{20,}\b"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b"), "GitHub fine-grained token"),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "Slack token"),
    (re.compile(r"\bsk-[0-9A-Za-z]{20,}\b"), "OpenAI-style secret key"),
    (re.compile(r"\bsk-ant-[0-9A-Za-z-]{20,}\b"), "Anthropic-style secret key"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"), "Google API key"),
    (re.compile(r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{5,}"),
     "JWT / bearer token"),
]

# A secret-ish keyword immediately assigned a long opaque value. Requires a
# real assignment (`=`/`:`) AND a >=12-char value with no spaces so ordinary
# sentences ("the password policy requires ...") do not trip it.
_KEYWORD_ASSIGN = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|passphrase|access[_-]?key|"
    r"client[_-]?secret|private[_-]?key|auth[_-]?token|bearer)\b\s*[:=]\s*"
    r"['\"]?[^\s'\"]{12,}",
)


def find_secret(text: str) -> str | None:
    """Return a short reason if ``text`` looks like it carries a secret, else None.

    The reason is a fixed label (or a keyword name), never the matched secret,
    so it is safe to surface on stderr or in an error message.
    """
    if not text:
        return None
    sample = text if len(text) <= _MAX_SCAN_CHARS else text[:_MAX_SCAN_CHARS]
    for pat, label in _TOKEN_PATTERNS:
        if pat.search(sample):
            return f"looks like a {label}"
    m = _KEYWORD_ASSIGN.search(sample)
    if m:
        # Report only the keyword that triggered it — not the assigned value.
        kw = re.split(r"\s*[:=]", m.group(0), maxsplit=1)[0].strip()
        return f"looks like an assigned credential ({kw})"
    return None


def assert_no_secret(text: str, where: str) -> None:
    """Raise :class:`SecretDetected` if ``text`` looks like it carries a secret."""
    reason = find_secret(text)
    if reason is not None:
        raise SecretDetected(
            f"{where} {reason}; refusing to persist a secret to durable memory. "
            "Re-state the fact without the credential."
        )
