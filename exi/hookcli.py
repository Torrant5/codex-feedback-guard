"""`exi-hook` — one wheel-installable entry point for hook runtime + install.

Runtime (payload on stdin, output on stdout):

    exi-hook <provider> <event>
    exi-hook codex UserPromptSubmit
    exi-hook claude PreToolUse
    exi-hook copilot-vscode Stop
    exi-hook copilot-cli userPromptTransformed

Install (generate/merge hook config for a surface, idempotent, backup, dry-run):

    exi-hook install claude --scope user
    exi-hook install copilot-vscode --scope project --project . --dry-run
    exi-hook install copilot-cli --scope user

Providers: ``codex``, ``claude``, ``copilot-vscode``, ``copilot-cli``. The Codex
runtime routes to :mod:`exi.feedback_hook` (unchanged behavior); the other three
route to :mod:`exi.feedback_adapters`. A single console script means generated
hook commands never depend on a ``bin/`` launcher or POSIX ``shlex`` quoting and
resolve identically from a wheel install on Windows.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import feedback_adapters, feedback_core, feedback_hook, hookgen

_CODEX_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
_INSTALL_PROVIDERS = (
    feedback_core.PROVIDER_CLAUDE,
    feedback_core.PROVIDER_COPILOT_VSCODE,
    feedback_core.PROVIDER_COPILOT_CLI,
)


def _run_hook(provider: str, event: str) -> int:
    if provider == feedback_core.PROVIDER_CODEX:
        if event not in _CODEX_EVENTS:
            return 0  # unknown Codex event: never block
        return feedback_hook.handle(event)
    return feedback_adapters.run(provider, event)


def _cmd_install(args) -> int:
    res = hookgen.install(
        args.provider, path=args.path, scope=args.scope,
        project=args.project, exe=args.exe, dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"DRY RUN — would write {res['path']}:")
        print(json.dumps(res["result"], ensure_ascii=False, indent=2))
        print(f"\n(existing entries preserved: {bool(res['existing'])})")
        return 0
    print(f"installed {args.provider} feedback hooks into {res['path']} "
          f"(backup at {res['path']}.bak if a prior file existed)")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Install verb.
    if argv and argv[0] == "install":
        p = argparse.ArgumentParser(prog="exi-hook install")
        p.add_argument("provider", choices=list(_INSTALL_PROVIDERS))
        p.add_argument("--scope", choices=["user", "project"], default="user")
        p.add_argument("--project", help="project directory (for --scope project)")
        p.add_argument("--path", help="explicit target file (overrides scope default)")
        p.add_argument("--exe", default=hookgen.DEFAULT_EXE,
                       help="executable token used in generated commands (default: exi-hook)")
        p.add_argument("--dry-run", action="store_true")
        try:
            return _cmd_install(p.parse_args(argv[1:]))
        except (OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    # Runtime: <provider> <event>.
    p = argparse.ArgumentParser(prog="exi-hook", description=__doc__)
    p.add_argument("provider", choices=list(feedback_core.PROVIDERS))
    p.add_argument("event", help="surface event name (e.g. UserPromptSubmit, userPromptTransformed)")
    args = p.parse_args(argv)
    return _run_hook(args.provider, args.event)


if __name__ == "__main__":
    raise SystemExit(main())
