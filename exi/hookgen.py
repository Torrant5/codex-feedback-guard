"""Safe, idempotent hook installers/generators for Claude, Copilot VS Code, and
Copilot CLI.

Each generator is a pure ``existing dict -> merged dict`` deep-copy + append:
every pre-existing key/entry is carried over unchanged, only this project's
feedback/memory hooks are added (each exactly once), and re-running never
duplicates. ``install`` backs the target up to ``<path>.bak`` exactly once (on
the first install, before the first overwrite) and writes atomically. ``--dry-run``
returns the merged result without touching disk.

Only the shared feedback/memory enforcement hooks are installed on Claude and
Copilot — never the Codex budget/quota guard (that stays Codex-only, in
``hookmerge``). Commands are the single ``exi-hook`` console script so a wheel
install on Windows needs no ``bin/`` launcher and no POSIX ``shlex`` quoting.

Surface formats:

* Claude — ``settings.json`` ``hooks`` object: event -> [ {matcher, hooks:[{type,command}]} ].
* Copilot VS Code — ``{version: 1, hooks: {PascalCase event: [...]}}``.
* Copilot CLI — ``{version: 1, hooks: {camelCase event: [...]}}``.
"""
from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path

from . import feedback_core
from .hookmerge import _atomic_write, _atomic_write_bytes

DEFAULT_EXE = "exi-hook"

# Neutral feedback events each surface installs (surface event names).
CLAUDE_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
VSCODE_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
# Copilot CLI: userPromptTransformed for injection (userPromptSubmitted output
# is dropped by the CLI), then the tool/stop events.
CLI_EVENTS = ["userPromptTransformed", "preToolUse", "postToolUse", "agentStop"]


def _quote(token: str) -> str:
    """Quote a single command token if it contains whitespace.

    Uses double quotes (valid in POSIX shells, cmd.exe, and PowerShell) rather
    than ``shlex.quote`` single quotes, which cmd.exe/PowerShell do not treat as
    quoting. The default executable (``exi-hook``) has no spaces, so no quoting
    happens in the common case.
    """
    if not token or any(c in token for c in ('\n', '\r', '"')):
        raise ValueError("--exe must be a non-empty command token without quotes or newlines")
    return f'"{token}"' if any(c.isspace() for c in token) else token


def command_string(provider: str, event: str, exe: str = DEFAULT_EXE) -> str:
    """Wheel-installable, cross-platform hook command for a surface event."""
    return f"{_quote(exe)} {provider} {event}"


# ---------------------------------------------------------------------------
# Claude settings.json (matcher-group shape)
# ---------------------------------------------------------------------------
def _ensure_claude_command(hooks: dict, event: str, command: str) -> None:
    groups = hooks.setdefault(event, [])
    target = None
    for g in groups:
        if isinstance(g, dict) and g.get("matcher", "") == "":
            target = g
            break
    if target is None:
        target = {"matcher": "", "hooks": []}
        groups.append(target)
    entries = target.setdefault("hooks", [])
    if not any(isinstance(h, dict) and h.get("command", "") == command for h in entries):
        entries.append({"type": "command", "command": command})


def merge_claude(existing: dict, exe: str = DEFAULT_EXE) -> dict:
    result = copy.deepcopy(existing) if existing else {}
    hooks = result.setdefault("hooks", {})
    for event in CLAUDE_EVENTS:
        _ensure_claude_command(hooks, event, command_string(feedback_core.PROVIDER_CLAUDE, event, exe))
    return result


# ---------------------------------------------------------------------------
# Direct-array shape (Copilot VS Code + Copilot CLI)
# ---------------------------------------------------------------------------
def _ensure_array_command(doc: dict, event: str, entry: dict) -> None:
    arr = doc.setdefault(event, [])
    cmd = entry.get("command")
    if not any(isinstance(h, dict) and h.get("command") == cmd for h in arr):
        arr.append(entry)


def merge_vscode(existing: dict, exe: str = DEFAULT_EXE) -> dict:
    result = copy.deepcopy(existing) if existing else {}
    version = result.setdefault("version", 1)
    if version != 1:
        raise ValueError(f"unsupported Copilot hook document version: {version!r}")
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing Copilot hook document has a non-object 'hooks' field")
    for event in VSCODE_EVENTS:
        cmd = command_string(feedback_core.PROVIDER_COPILOT_VSCODE, event, exe)
        # OS-specific fields: identical here (exi-hook resolves on every OS), but
        # emitted explicitly so an installer can override per-OS and so the
        # Windows form is always present and executable-safe.
        _ensure_array_command(hooks, event, {
            "type": "command", "command": cmd, "windows": cmd,
        })
    return result


def merge_cli(existing: dict, exe: str = DEFAULT_EXE) -> dict:
    result = copy.deepcopy(existing) if existing else {}
    version = result.setdefault("version", 1)
    if version != 1:
        raise ValueError(f"unsupported Copilot hook document version: {version!r}")
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing Copilot hook document has a non-object 'hooks' field")
    for event in CLI_EVENTS:
        cmd = command_string(feedback_core.PROVIDER_COPILOT_CLI, event, exe)
        _ensure_array_command(hooks, event, {"type": "command", "command": cmd})
    return result


# ---------------------------------------------------------------------------
# Default target paths
# ---------------------------------------------------------------------------
def _userprofile() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())


def default_path(provider: str, scope: str = "user", project: str | None = None) -> Path:
    proj = Path(project) if project else Path.cwd()
    if provider == feedback_core.PROVIDER_CLAUDE:
        base = proj if scope == "project" else Path.home()
        return base / ".claude" / "settings.json"
    if provider == feedback_core.PROVIDER_COPILOT_VSCODE:
        # VS Code loads project .github/hooks/*.json; use a dedicated file so
        # unrelated hook files are never touched.
        if scope == "project":
            return proj / ".github" / "hooks" / "exi-feedback-vscode.json"
        return _userprofile() / ".copilot" / "hooks" / "exi-feedback-vscode.json"
    if provider == feedback_core.PROVIDER_COPILOT_CLI:
        if scope == "project":
            return proj / ".github" / "hooks" / "exi-feedback-cli.json"
        return _userprofile() / ".copilot" / "hooks" / "exi-feedback-cli.json"
    raise ValueError(f"unknown provider: {provider}")


_MERGERS = {
    feedback_core.PROVIDER_CLAUDE: merge_claude,
    feedback_core.PROVIDER_COPILOT_VSCODE: merge_vscode,
    feedback_core.PROVIDER_COPILOT_CLI: merge_cli,
}


def generate(provider: str, existing: dict, exe: str = DEFAULT_EXE) -> dict:
    try:
        merger = _MERGERS[provider]
    except KeyError:
        raise ValueError(f"unknown provider: {provider}")
    return merger(existing, exe)


def install(provider: str, path: Path | None = None, scope: str = "user",
            project: str | None = None, exe: str = DEFAULT_EXE, dry_run: bool = False) -> dict:
    path = Path(path) if path else default_path(provider, scope=scope, project=project)
    existing = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"refusing to overwrite malformed hook JSON at {path}: {e}") from e
    if not isinstance(existing, dict):
        raise ValueError(f"refusing to overwrite non-object hook JSON at {path}")
    merged = generate(provider, existing, exe)
    if dry_run:
        return {"provider": provider, "path": str(path), "dry_run": True,
                "result": merged, "existing": existing}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            original_mode = stat.S_IMODE(path.stat().st_mode)
            _atomic_write_bytes(backup, path.read_bytes(), mode=original_mode)
    _atomic_write(path, json.dumps(merged, ensure_ascii=False, indent=2))
    return {"provider": provider, "path": str(path), "dry_run": False,
            "result": merged, "existing": existing}
