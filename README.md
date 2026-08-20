# codex-feedback-guard

**Unofficial, community-maintained project. Not affiliated with, endorsed
by, or supported by OpenAI or Anthropic.** "Codex" here refers to the OpenAI
Codex CLI's hook system, which this project integrates with as an external
tool — it does not modify or redistribute Codex itself.

Two standard-library-only tools that share one design goal: make agent work
**accountable** — knowledge is shared with evidence, and long Codex runs
cannot silently burn a week of quota in a day.

- **`exi`** — an *external intelligence* store. Capture observations with
  evidence, search them (SQLite FTS5), and surface confirmed knowledge as
  human-reviewable **promotion candidates**. It never edits `AGENTS.md`,
  `CLAUDE.md`, or any skill file for you.
- **`codex-guard`** — a **budget guard** for Codex. Codex hooks watch each
  turn for runaway time, tool-call floods, tight loops, and weekly-quota
  burn, and *deny* the offending tool call before it runs. A `managed-run`
  supervisor can hard-stop a process **it launched** on time/quota overrun.

Python 3.11+, standard library only (no third-party runtime dependencies).
No network access except invoking a local `llm-quota`-compatible CLI that
*you* provide (see [Quota adapter](#quota-adapter) below).

---

## Install

```bash
git clone https://github.com/Torrant5/codex-feedback-guard.git
cd codex-feedback-guard
pip install -e .
```

This installs two console scripts, `exi` and `codex-guard`, backed by the
`exi` package. You can also run them straight from a checkout without
installing, via `bin/exi` and `bin/codex-guard` (both add the repo root to
`sys.path` themselves).

Default config and defaults are packaged inside `exi/` and per-user
config/data live under standard XDG paths (see
[Configuration](#configuration)), so a normal `pip install` — editable or
a plain wheel — works with **no `EXI_*` environment variables required**.

> Editable installs (`pip install -e .`) are recommended for development —
> `bin/codex-guard` resolves directly to your checkout. A non-editable wheel
> install works the same way; `codex-guard install-hooks` falls back to
> invoking the active interpreter with `-m exi.guardcli` instead.

---

## Architecture and features

Both tools share one package (`exi/`) and one config-loading layer
(`exi/config.py`), but are otherwise independent:

| Module | Responsibility |
|---|---|
| `exi/store.py` | Append-only observation log, FTS5 search, promotion candidates |
| `exi/exicli.py` | `exi` command line (capture/search/list/promote/audit/confirm/verify/retire + `feedback`) |
| `exi/feedback.py` | Feedback-rule store, declarative enforcement engine, severity ladder |
| `exi/feedback_hook.py` | Codex hook entry points for feedback enforcement |
| `exi/guard.py` | Budget-guard state, threshold evaluation |
| `exi/guardcli.py` | `codex-guard` command line (hook/managed-run/status/install-hooks) |
| `exi/hook.py` | Codex hook entry points for the budget guard |
| `exi/hookmerge.py` | Safe, idempotent merge of guard/feedback hooks into `hooks.json` |
| `exi/managed_run.py` | Process-group supervisor for time/quota-bounded subprocesses |
| `exi/quota.py` | Thin wrapper + cache over a local `llm-quota`-compatible CLI |

### `exi` — external intelligence store

Source of truth is an append-only JSONL event log (`data/observations.jsonl`);
current state is derived by replaying it, and the FTS5 index
(`data/index.sqlite`) is a disposable cache rebuilt whenever the log is newer.

**Observation fields:** `id, status, scope, claim, evidence_paths,
confirmed_count, created_at, last_verified, review_after, supersedes, triggers`.

`confirmed_count` is the number of **distinct** evidence sources. An
observation cannot become `confirmed` (and therefore cannot be promoted)
with fewer than **2 independent** evidence sources — enforced in the store,
not left to the caller.

```bash
# capture (requires evidence — no rootless claims). Starts as 'candidate'.
exi capture --scope "backend/deploy-pipeline" \
  --claim "Deploys must go through the deploy-pipeline wrapper" \
  --evidence "AGENTS.md#L45" --trigger deploy --trigger ci \
  --review-after "2027-01-01T00:00:00+0900"

# add an INDEPENDENT second source -> auto-promotes status to 'confirmed'
exi confirm <id> --evidence "runbook.md"

exi search "deploy-pipeline" -v   # FTS5, literal-term (special chars safe)
exi list --status confirmed
exi promote <id>                  # writes data/promotions/<id>.md ONLY
exi audit                         # review-due / weak / promotable summary
exi verify <id> --review-after …  # mark re-verified
exi retire <id>
```

`promote` **only** writes a Markdown candidate under `data/promotions/`. It
does not touch `AGENTS.md`/`CLAUDE.md`/skills — a human decides whether and
where to apply it.

### `exi feedback` — never make the user say it twice

A feedback-learning store with **graded** Codex enforcement. A human records
a piece of feedback (a *rule*) each time they have to give it; the **count**
of distinct occurrences drives how forcefully the hooks act. Nothing is ever
auto-promoted, auto-imported, or seeded — you record the rules you want.

```bash
# record an occurrence. First time => count 1. A NEW --evidence id => count+1.
exi feedback record --name no-hidden-fallback \
  --description "Do not add silent fallbacks; surface failures as failures" \
  --evidence "incident-2026-08-19" \
  --why "hidden fallbacks mask the real bug" \
  --how-to-apply "let it fail loudly; report the cause"

exi feedback record --name no-hidden-fallback --description "…" --evidence "incident-2026-08-25"  # -> count 2

exi feedback list -v
exi feedback show no-hidden-fallback
exi feedback violations [NAME]      # hook-detected violations (never change count)
exi feedback enable/disable NAME
```

`count` is **the number of distinct human occurrences only.** A duplicate
`--evidence` id is rejected so a complaint logged twice can't inflate it, and
a hook violation appends a `violation` event but **never** touches `count`.

#### Severity ladder

Each matched rule resolves to a severity — an explicit `severity` on the
spec wins, otherwise it is derived from `count`:

| count | severity | effect |
|---|---|---|
| 1–2 | **warn**  | non-blocking note added as context / stderr |
| 3–4 | **pause** | `PreToolUse` deny with an out-of-band approval path |
| ≥ 5 | **deny**  | hard block — **not** bypassable |

#### Enforcement specs (declarative only — no shell)

Attach conditions with `configure`. The spec is strictly validated against
an allow-list; **there is no key that runs a shell command or external
checker.** The engine only evaluates built-in, predictable conditions:

```bash
# forbid a dangerous command shape
exi feedback configure no-hidden-fallback \
  --spec-json '{"event":"pre_bash","when":"--sandbox danger-full-access"}'

# require a test sibling when editing a module (test files themselves excluded)
exi feedback configure ship-tests \
  --spec-json '{"event":"pre_edit","path_glob":"**/*.py","exclude_glob":"**/test_*.py","absent_sibling":"{dir}/test_{stem}.py"}'

# at Stop, fail if a changed file still has debug prints
exi feedback configure no-debug \
  --spec-json '{"event":"stop_check","forbid_regex":"print\\("}'
```

* **`event`**: `pre_bash` | `pre_edit` | `stop_check`.
* Common: `severity`, `message`, `scope` (substring of cwd), `unless` (regex
  escape hatch).
* `pre_bash`: `when` / `forbid_regex` on the command string.
* `pre_edit`: `path_glob`, `exclude_glob`, `absent_sibling`
  (`{stem}`/`{dir}`/`{suffix}`/`{name}`/`{parent}`), `require_regex`,
  `forbid_regex` over the proposed new content.
* `stop_check`: `path_glob`, `exclude_glob`, `absent_sibling`,
  `require_regex`, `forbid_regex` over the **on-disk content of files this
  session actually changed** (tracked via `PostToolUse`; Bash writes are
  never guessed). Reads are refused outside the session cwd (path-traversal
  guard). Globs use a predictable matcher where `*` never crosses `/` and
  `**` does.

#### `pause` — the Codex-`ask`-substitute

Codex's `PreToolUse` supports only `deny`/`allow` (no `ask`), and returning
an unsupported decision would let the tool run anyway — so this engine
**never** returns `ask`. A `pause` is implemented as a one-shot, out-of-band
approval:

1. `PreToolUse` matches a pause rule → **deny** with a random `nonce`,
   telling the user to reply with the exact line `ALLOW_FEEDBACK:<nonce>`.
2. Only the **`UserPromptSubmit`** hook recognizes that reply and marks the
   pending approval `approved` — the model cannot forge a user prompt, so it
   cannot self-approve. The **entire** stripped prompt must match
   `ALLOW_FEEDBACK:<16 lowercase hex>` **exactly**; any leading or trailing
   text fails to approve.
3. The **next identical** tool call (same session + tool fingerprint + rule)
   consumes the permit **exactly once** and is allowed; a further retry
   pauses again. Permits have a TTL (default 10 min) and cannot be reused
   across a different session, tool call, or rule.
4. A **hard `deny`** (`count ≥ 5` or explicit `severity:"deny"`) has no
   nonce and cannot be approved this way.

#### Administrative gate — human-gating the management CLI

Independently of any user rule, a **built-in** `PreToolUse` gate recognizes
the supported feedback-management mutations and requires the same
out-of-band human confirmation before they run:

* Gated: `exi feedback configure`, `exi feedback disable`,
  `exi feedback enable` — recognized across the common command forms
  (`bin/exi …` relative/absolute, a bare `exi` on `PATH`, and
  `python -m exi.exicli …`).
* **Not** gated: `exi feedback record` (it only adds human evidence and must
  stay usable), nor read-only `list` / `show` / `violations`.

The gate **denies** with a random `ALLOW_FEEDBACK_ADMIN:<16 lowercase hex>`
marker. Approval mirrors the pause flow but uses a **separate nonce pool**,
so an `ALLOW_FEEDBACK:` reply can never satisfy the admin gate and
vice-versa: only an entire stripped prompt matching
`ALLOW_FEEDBACK_ADMIN:<nonce>` exactly approves, same session, same exact
tool fingerprint, TTL-bound, and consumed **one-shot**. If detection ever
errors it fails **closed** (treats the command as gated) — the opposite of
every other regex path here, which fail open.

Hard denies are always evaluated **first**: an admin permit never bypasses a
feedback rule that independently resolves to a hard `deny`.

This is a belt, not a sandbox — see [Safety Model](#safety-model).

#### Stop loop-guard

The `Stop` hook blocks (`decision:"block"`) up to a small fixed number of
times (default 3) to give the agent a chance to fix the flagged files; after
that it **stops blocking** and emits a manual-confirm note, so it can never
wedge the session in an infinite stop loop. The cap is **hard-clamped to
`0..3`** no matter what `feedback.stop_max_blocks` is set to, and the
attempt counter is keyed by **session + turn only** — not by which rule
fired — so an agent cannot exceed three blocks in a turn by alternating
which configured rule trips each time. `Stop` always prints valid JSON.

`UserPromptSubmit` injects enabled rules with `count ≥ 3` (configurable)
whose `scope` (if any) matches the current cwd, highest count first, capped
at `feedback.inject_max_chars` (default 3000; lower-count rules are
shortened, then omitted with a note, before any hard cut). The injected
size **never exceeds 3000 chars** even if the config asks for more — the
config can only lower the cap.

### `codex-guard` — Codex budget guard

#### What it checks per turn

| Check | soft (warn) | hard (deny) |
|---|---|---|
| Turn wall-clock time | 45 min | 120 min |
| Weekly Codex usage increase **this turn** | +3% | +5% |
| Weekly Codex usage increase **rolling 24h** | +12% | +20% |
| Tool calls in one turn | — | > 150 |
| Same tool+input fingerprint repeated | — | ≥ 3× |

All thresholds are configurable — see [Configuration](#configuration).
Weekly consumption is measured as the **sum of positive deltas** between
usage samples, so a weekly reset (used% drops) is never mistaken for
consumption, and post-reset burn is still counted.

Quota is read **only** via the configured `llm-quota`-compatible command
(default: `llm-quota --json --providers codex` on `PATH`; main `codex`
pool). If that is unavailable or the pool can't be resolved, quota checks
are skipped and the reason is reported — the time/count/repeat guards keep
running. See [Quota adapter](#quota-adapter) for the expected JSON shape.
The result, including an `unknown` reason, is cached for 30 seconds by
default to avoid starting the quota command on every tool call. Guard state
is separated by session/turn and file-locked, so concurrent Codex sessions
do not overwrite one another's counters.

#### `managed-run` — supervise a process this tool owns

```bash
codex-guard managed-run -- your-long-running-command --with args
codex-guard managed-run --dry-run -- <cmd>   # monitor only, log the kill it would send
codex-guard managed-run --status             # list owned runs
```

Launches the command in its own process group, then on a **hard**
time/24h-quota breach escalates `SIGTERM → grace → SIGKILL` — against
**its own process group only**. It refuses to signal any pgid it did not
create, the tool's own group, or pgid ≤ 1. It never touches unrelated PIDs.

Long-running shell commands must be launched through `managed-run` to get
mid-command enforcement. A normal `PreToolUse` hook only decides *before*
the tool starts and cannot stop that same call halfway through.

#### `status`

```bash
codex-guard status            # human-readable
codex-guard status --json     # quota + turn context + findings + thresholds
```

---

## Safety model

**Read this before relying on the guard for anything.** It reduces — it
does **not** eliminate — the "an agent ran for hours and ate the weekly
quota" failure mode. It can only act at the moments Codex actually invokes
a hook:

| Situation | Covered? |
|---|---|
| A tool call is about to run (`PreToolUse`) | ✅ can **deny** it |
| A new user turn starts (`UserPromptSubmit`) | ✅ resets per-turn counters, samples quota |
| Context compaction boundary (`PreCompact`) | ✅ best-effort hard stop |
| One tool call that itself runs for hours | ❌ `PreToolUse` cannot interrupt it after launch; start it through `managed-run` |
| Hosted tools such as web search | ❌ not covered by `PreToolUse` hooks |
| The model **reasons/plans for a long time without calling any tool** | ❌ no hook fires — cannot interrupt |
| The Codex **application itself** (not the CLI agent loop) | ❌ out of scope |
| Hooks not **trusted/approved** in Codex | ❌ hooks never run → **no protection at all** |
| Quota is `unknown` (adapter down / main pool unresolved) | ⚠️ quota checks skip; time/tool-count/repeat guards stay active |
| Usage telemetry updates late | ⚠️ quota guard reacts to the latest observable value, so detection can lag |

So: the guard is a **backstop at tool boundaries**, plus an independent
`managed-run` supervisor for processes it owns. It is honest about the gaps
rather than pretending to be a hard ceiling. There is **no** fallback to a
metered API anywhere — quota exhaustion is surfaced, never worked around.

This is a **belt, not a sandbox**: arbitrary same-user tampering with the
source or data (editing these files, deleting `feedback.jsonl`, calling the
Python API directly) **cannot be made cryptographically impossible** — a
local process running as you can always do what you can do. What the gate
buys is that the *supported management CLI* path — the one an agent would
naturally reach for — is human-gated rather than silently self-serviced.

Other safety properties:

* Every feedback hook **fails open**: an internal error surfaces on stderr
  and gets out of the way rather than blocking (and `Stop` still emits
  `{}`). The budget guard's separate fail-*closed* behavior is unchanged.
  The lone fail-*closed* exception is the admin-mutation detector.
* **Broken feedback data is never silently reset** — enforcement fails open
  and the corruption is surfaced.
* **Regex matching is bounded** so a pathological rule pattern cannot hang
  the hook: every runtime regex enforces a max pattern/input length and a
  short wall-clock deadline (`SIGALRM`). A `(a+)+$`-style catastrophe raises
  and the hook fails open (no enforcement that call) instead of spinning.
  No external dependency is used.
* No secrets or conversation bodies are stored or injected; only rules,
  evidence ids, counters, hashed fingerprints, and short-lived nonces.
  State is limited to counters, **hashed** tool-call fingerprints, and
  weekly-usage percentages (`data/guard-state.json`).
* Enforcement can only act where Codex fires a hook (see the table above);
  it is a graded nudge, not a hard ceiling.

---

## Install / config / hooks workflow

1. **Install** the package (see [Install](#install)).
2. **Preview the hook merge** — writes nothing, just confirms your existing
   `Stop` hook (if any) survives:
   ```bash
   codex-guard install-hooks --dry-run
   ```
3. **Install the hooks** — merges into `~/.codex/hooks.json` by default
   (override with `--path`), backing up the original to `hooks.json.bak`
   on the very first run:
   ```bash
   codex-guard install-hooks
   ```
   The merge is safe and idempotent: it **adds** the guard's
   `UserPromptSubmit`, `PreToolUse`, and `PreCompact` entries **and** the
   feedback engine's four hooks (`UserPromptSubmit`, `PreToolUse`,
   `PostToolUse`, `Stop`) — the guard and feedback commands share event
   names without shadowing each other — while preserving every existing
   hook object (including one installed by another tool) unchanged. The
   JSON file is re-serialized, so whitespace is not byte-for-byte identical.
   The original file is backed up once; writes are atomic; re-running does
   not duplicate entries.
4. **Trust the hooks in Codex.** You must approve these hooks inside Codex
   for them to run — until then the guard provides no protection. This is a
   Codex safety property, not a bug in this project.
5. **(Optional) point at your own quota adapter** — see below.
6. **(Optional) record feedback rules** — see `exi feedback record` above.

---

## Configuration

Defaults are packaged inside the `exi` module as `exi/config.default.json`
(installed as package data, so they're present after any install — editable,
wheel, or running from a checkout). Override any subset by creating a
per-user `config.json`; it is deep-merged over the defaults. Keys:

- `guard.*` — thresholds from the table in [`codex-guard`](#codex-guard--codex-budget-guard).
- `quota.cmd` / `quota.timeout_seconds` / `quota.cache_seconds` — the quota
  adapter command (list of argv), its subprocess timeout, and cache TTL.
- `managed_run.grace_seconds` / `managed_run.poll_seconds`.
- `feedback.inject_min_count` / `feedback.inject_max_chars` /
  `feedback.approval_ttl_seconds` / `feedback.stop_max_blocks`.

**Default paths** (no environment variables needed):

| | Default location |
|---|---|
| User config (`config.json`) | `$XDG_CONFIG_HOME/codex-feedback-guard/config.json`, falling back to `~/.config/codex-feedback-guard/config.json` |
| Runtime data (`data/`) | `$XDG_DATA_HOME/codex-feedback-guard`, falling back to `~/.local/share/codex-feedback-guard` |

`EXI_CONFIG` and `EXI_DATA_DIR` environment variables are **authoritative**
overrides — when set, they relocate the config file / data directory to any
path you choose, bypassing the XDG defaults above (also used by the test
suite to isolate state).

## Quota adapter

`codex-guard` never talks to any billing API directly. It shells out to
whatever command you configure under `quota.cmd` (default: `llm-quota
--json --providers codex`, resolved via `PATH`) and parses its stdout as
JSON. You need to provide this command yourself — it's expected to know how
to read *your* Codex account's usage (however you track that).

Expected JSON shape:

```json
{
  "providers": {
    "codex": {
      "ok": true,
      "mode": "normal",
      "windows": {
        "weekly": {
          "used_percent": 42.0,
          "resets_at": "2026-08-24T00:00:00+00:00"
        }
      }
    }
  }
}
```

- `providers.codex.ok` — `false` (or the key missing) means "can't resolve
  the main pool right now"; the guard treats quota as `unknown` and keeps
  time/count/repeat checks running.
- `providers.codex.mode` — a free-form string (`normal` / `conserve` /
  `critical` are the ones this project treats as meaningful) surfaced in
  `status` output.
- `providers.codex.windows.weekly.used_percent` — required for any
  weekly-burn check to run; missing → `unknown`.
- `resets_at` — surfaced in `status` output; not required for the guard
  logic itself.

If you don't have such a command, quota-based checks simply report
`unknown` and skip — time/tool-count/repeat-loop guards are unaffected.

## Feedback hooks

See [`exi feedback`](#exi-feedback--never-make-the-user-say-it-twice) above
for the full model (severity ladder, enforcement specs, pause approval
flow, admin gate, Stop loop-guard). In short: hooks never silently teach the
model anything you didn't explicitly record with `exi feedback record`, and
enforcement specs are a closed, declarative allow-list — never arbitrary
code.

---

## Limitations

- **Not a sandbox.** See [Safety Model](#safety-model) — a same-user local
  process can always bypass this by editing files or the data store
  directly.
- **Coverage gaps at the hook level.** Long single tool calls, hosted
  tools, and model "thinking time" without a tool call are not observable
  or interruptible — see the coverage table above.
- **Quota adapter is BYO.** This project ships no quota-fetching logic of
  its own; you must have (or build) a small CLI that emits the JSON shape
  above for your account.
- **Some `managed-run` tests use short real sleeps** and spawn real
  subprocesses; they're not network-dependent but aren't instantaneous
  either.

## Testing

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Covers: normal allow, time soft/hard, per-turn quota, rolling-24h,
repeat-loop, quota-unknown (blocks on time but not on quota), weekly-reset
handling, hook merge preserving an existing `Stop` hook plus the four
feedback hooks, `managed-run` signaling only the owned process group,
state/store locking, independent evidence-source handling, and quota-cache
behavior. The feedback engine adds: count-from-humans-only and
duplicate-evidence rejection, strict spec validation, the warn/pause/deny
ladder, the nonce approval lifecycle (exact match, session/fingerprint/rule
binding, TTL, one-shot), hard-deny being non-bypassable, injection
budgeting, change tracking, the Stop block cap, fail-open on internal
error, and concurrent-write safety.

## Layout

```
codex-feedback-guard/
├── bin/{exi,codex-guard}      # dev-mode launchers (also installed as console scripts)
├── exi/                       # package: store, guard, hook, feedback, feedback_hook, managed_run, quota, …
│   └── config.default.json    # packaged defaults (package data)
├── tests/
└── pyproject.toml
```

Per-user config/data default to XDG paths outside the repo — see
[Configuration](#configuration) — and are not part of the package layout.

## License

MIT — see [LICENSE](LICENSE).
