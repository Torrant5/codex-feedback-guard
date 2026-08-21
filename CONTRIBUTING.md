# Contributing

Thanks for considering a contribution to Agent Steward (Python distribution name
`codex-feedback-guard`, kept for backward compatibility — see the README's
[Naming](README.md#naming) note). This is a small, unofficial,
community-maintained project — not affiliated with or endorsed by OpenAI,
GitHub, or Anthropic.

## Getting started

```bash
git clone https://github.com/Torrant5/agent-steward.git
cd agent-steward
python3 -m unittest discover -s tests -p 'test_*.py'
```

No third-party dependencies are required to run the tools or the test suite
(standard library only, Python 3.11+).

## Development guidelines

- **Standard library only.** Do not add third-party runtime dependencies.
  This is a deliberate design constraint (see the README's Safety Model
  section) — it keeps the guard's trust surface small.
- **No hidden fallbacks.** If a check can't be completed (quota unknown,
  corrupt state, etc.), surface that explicitly rather than silently
  proceeding as if everything were fine. See `exi/quota.py` and
  `exi/guard.py` for the existing pattern.
- **Fail-open vs fail-closed is intentional per module.** Feedback
  enforcement fails open on internal errors (a bug shouldn't block your
  work); the budget guard's hard-deny path and the admin-mutation gate fail
  closed. Don't change these defaults without discussing the tradeoff in
  your PR description.
- **Every new behavior needs a test.** The suite is unittest-based; put new
  tests in `tests/test_*.py` alongside the module they cover.
- **Keep the CLI surface declarative where possible.** The feedback
  enforcement spec language (`exi feedback configure`) is intentionally
  restricted to a small set of built-in, predictable conditions — there is
  no key that shells out or runs arbitrary code. Please keep it that way;
  it's a security property, not an accident.

## Running tests

```bash
# full suite
python3 -m unittest discover -s tests -p 'test_*.py'

# fail on any leaked file descriptor / unclosed resource
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py'

# a single module
python3 -m unittest tests.test_guard -v
```

Some `managed_run` tests use short real sleeps and spawn subprocesses; they
are not hermetic in the sense of being instantaneous, but they don't touch
the network or anything outside a temp directory.

## Submitting a change

1. Open an issue first for anything beyond a small fix, so we can agree on
   the approach — this project has a fairly narrow safety model and not
   every feature request fits it.
2. Keep PRs focused. Unrelated formatting/refactor changes make review
   harder.
3. Make sure `python3 -m unittest discover -s tests -p 'test_*.py'` passes.
4. Describe *why* the change is needed, not just what it does.

## Reporting bugs

Open a GitHub issue with: what you ran, what you expected, what happened
instead, and your Python version. If it's a security issue, see
[SECURITY.md](SECURITY.md) instead of filing a public issue.
