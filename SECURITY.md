# Security Policy

`codex-feedback-guard` is an unofficial, community-maintained project. It is
not affiliated with, endorsed by, or supported by OpenAI or Anthropic.

## Scope and threat model

Read the README's **Safety Model** section first — it describes, precisely,
what this tool can and cannot enforce. In short:

- It is a **backstop at Codex hook boundaries** (`PreToolUse`,
  `UserPromptSubmit`, `PreCompact`, `PostToolUse`, `Stop`), not a sandbox.
  A local process running as the same user as Codex can always bypass it by
  editing the source, deleting state files, or calling the Python API
  directly. This is a property of the design, not a bug to report.
- It provides **no protection at all** if the hooks are not trusted/approved
  inside Codex.
- The declarative feedback-enforcement spec language deliberately has no key
  that runs a shell command or arbitrary code. If you find an input that
  causes one to execute anyway, that **is** a security bug — see below.
- Regex-based matching is bounded (max pattern/input length + a wall-clock
  deadline) specifically to prevent ReDoS-style hangs from a pathological
  rule. If you find a pattern that still hangs a hook, that's a bug.

## Reporting a vulnerability

If you find a security issue — including but not limited to: enforcement
that can be bypassed in an unintended way, a ReDoS in the spec matcher, a
path-traversal in the `stop_check` file-reading logic, secrets or
conversation content ending up in stored state, or `managed-run` signaling a
process group it doesn't own — please report it privately rather than
opening a public issue.

- Open a **GitHub Security Advisory** on this repository
  (`Security` tab → `Report a vulnerability`), or
- Email the maintainer listed on the GitHub profile for **Torrant5**.

Please include: the affected file/function, a minimal reproduction, and the
impact you believe it has. We'll acknowledge reports as promptly as we can
for a project of this size; there's no formal SLA.

## Out of scope

- The Codex or Claude Code applications themselves — report those to the
  respective vendor.
- Denial-of-service via resource exhaustion of your own machine by running
  the tool as intended (e.g. `managed-run` supervising a process you told it
  to supervise).
- Anything that requires an attacker to already have arbitrary code
  execution as the same local user (see threat model above).
