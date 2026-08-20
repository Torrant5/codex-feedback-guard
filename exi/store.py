"""Observation store: append-only JSONL event log + SQLite FTS5 search index.

Source of truth is ``observations.jsonl`` (one JSON event per line). Current
state is derived by replaying the log, so history/audit is never lost and a
corrupt index can always be rebuilt. The FTS5 index (``index.sqlite``) is a
disposable cache rebuilt from the log whenever the log is newer.

Observation record fields (the promotable unit of knowledge):
    id, status, scope, claim, evidence_paths, confirmed_count,
    created_at, last_verified, review_after, supersedes, triggers

`confirmed_count` == number of *distinct evidence sources*, where a source is
identified by `evidence_source_key()`: a file or URL with its trailing
`#fragment` stripped. Citing the same file at `#L10` and again at `#L20` is
still one source — fragments identify *where in* a source, not a second,
independent source. An observation is only eligible to be `confirmed` (and
thus promoted) once it has >= 2 independent (distinct-source-key) evidence
sources; with fewer it stays a `candidate`. This is enforced here, not left
to the caller — no silent promotion.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import config

MIN_INDEPENDENT_EVIDENCE = 2


def evidence_source_key(evidence: str) -> str:
    """Normalize an evidence path/URL to its distinct-source identity.

    Strips a trailing `#fragment` (e.g. `#L10`, `#section-2`) so the same
    file or URL cited at different line ranges/anchors counts as one source.
    Independence is about the underlying file or URL, not which fragment of
    it was cited.
    """
    return evidence.split("#", 1)[0]

STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_SUPERSEDED = "superseded"
STATUS_RETIRED = "retired"


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _gen_id(scope: str, claim: str, ts: float) -> str:
    h = hashlib.sha1(f"{scope}\x1f{claim}\x1f{ts:.6f}".encode("utf-8")).hexdigest()
    return h[:12]


@dataclass
class Observation:
    id: str
    status: str
    scope: str
    claim: str
    evidence_paths: list = field(default_factory=list)
    confirmed_count: int = 0
    created_at: str = ""
    last_verified: str = ""
    review_after: str = ""
    supersedes: str = ""
    triggers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "scope": self.scope,
            "claim": self.claim,
            "evidence_paths": list(self.evidence_paths),
            "confirmed_count": self.confirmed_count,
            "created_at": self.created_at,
            "last_verified": self.last_verified,
            "review_after": self.review_after,
            "supersedes": self.supersedes,
            "triggers": list(self.triggers),
        }


class Store:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir) if data_dir else config.data_dir()
        self.log_path = self.dir / "observations.jsonl"
        self.index_path = self.dir / "index.sqlite"
        self.lock_path = self.dir / "store.lock"
        self.promotions_dir = self.dir / "promotions"

    # ---- locking -------------------------------------------------------------
    @contextmanager
    def _locked(self):
        """Exclusive transaction spanning derive -> append event -> reindex.

        Serializes every mutation (`capture`/`confirm`/`verify`/`retire`) so
        two concurrent `exi` invocations can't interleave a read-then-append
        and lose an update, and so `confirm`'s independence re-check (does
        this evidence source already exist?) can't race a concurrent
        confirm/capture on the same observation. The public methods below
        acquire this lock and then call an `_..._unlocked` helper; those
        helpers assume the lock is already held and must never acquire it
        again themselves — flock is not reentrant within a process, so doing
        so would deadlock a process against itself.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    # ---- low level log I/O -------------------------------------------------
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
                    # Do not silently drop data — surface the corruption.
                    raise ValueError(
                        f"corrupt event at {self.log_path}:{lineno}: {e}"
                    ) from e
        return events

    # ---- state derivation --------------------------------------------------
    def derive(self) -> dict:
        """Replay the log into {id: Observation}."""
        state: dict = {}
        for ev in self._read_events():
            t = ev.get("type")
            if t == "capture":
                obs = Observation(
                    id=ev["id"],
                    status=ev.get("status", STATUS_CANDIDATE),
                    scope=ev.get("scope", ""),
                    claim=ev.get("claim", ""),
                    evidence_paths=list(dict.fromkeys(ev.get("evidence_paths", []))),
                    created_at=ev.get("ts", ""),
                    last_verified=ev.get("ts", ""),
                    review_after=ev.get("review_after", ""),
                    supersedes=ev.get("supersedes", ""),
                    triggers=list(ev.get("triggers", [])),
                )
                obs.confirmed_count = len({evidence_source_key(p) for p in obs.evidence_paths})
                obs.status = self._status_for(obs)
                state[obs.id] = obs
                # A capture may supersede an earlier observation.
                if obs.supersedes and obs.supersedes in state:
                    state[obs.supersedes].status = STATUS_SUPERSEDED
            elif t == "confirm":
                obs = state.get(ev["id"])
                if obs is None:
                    continue
                for p in ev.get("evidence_paths", []):
                    if p not in obs.evidence_paths:
                        obs.evidence_paths.append(p)
                obs.confirmed_count = len({evidence_source_key(p) for p in obs.evidence_paths})
                obs.last_verified = ev.get("ts", obs.last_verified)
                if ev.get("review_after"):
                    obs.review_after = ev["review_after"]
                if obs.status not in (STATUS_SUPERSEDED, STATUS_RETIRED):
                    obs.status = self._status_for(obs)
            elif t == "verify":
                obs = state.get(ev["id"])
                if obs is None:
                    continue
                obs.last_verified = ev.get("ts", obs.last_verified)
                if ev.get("review_after"):
                    obs.review_after = ev["review_after"]
            elif t == "retire":
                obs = state.get(ev["id"])
                if obs is not None:
                    obs.status = STATUS_RETIRED
        return state

    @staticmethod
    def _status_for(obs: Observation) -> str:
        if obs.status in (STATUS_SUPERSEDED, STATUS_RETIRED):
            return obs.status
        if obs.confirmed_count >= MIN_INDEPENDENT_EVIDENCE:
            return STATUS_CONFIRMED
        return STATUS_CANDIDATE

    def get(self, obs_id: str) -> Observation | None:
        return self.derive().get(obs_id)

    # ---- mutations ---------------------------------------------------------
    # Each public mutation acquires `_locked()` then delegates to an
    # `_..._unlocked` twin that does the actual read-modify-write. The twins
    # never lock themselves (see `_locked` docstring).
    def capture(
        self,
        scope: str,
        claim: str,
        evidence_paths: list,
        triggers: list | None = None,
        supersedes: str = "",
        review_after: str = "",
    ) -> Observation:
        with self._locked():
            return self._capture_unlocked(scope, claim, evidence_paths, triggers, supersedes, review_after)

    def _capture_unlocked(
        self,
        scope: str,
        claim: str,
        evidence_paths: list,
        triggers: list | None,
        supersedes: str,
        review_after: str,
    ) -> Observation:
        if not scope or not claim:
            raise ValueError("capture requires both --scope and --claim")
        if not evidence_paths:
            # No fabrication / rootless claims: an observation needs evidence.
            raise ValueError("capture requires at least one --evidence path/source")
        ts = _now()
        evidence_paths = list(dict.fromkeys(evidence_paths))
        obs_id = _gen_id(scope, claim, ts)
        if supersedes and supersedes not in self.derive():
            raise ValueError(f"--supersedes id not found: {supersedes}")
        event = {
            "type": "capture",
            "ts": _iso(ts),
            "id": obs_id,
            "status": STATUS_CANDIDATE,
            "scope": scope,
            "claim": claim,
            "evidence_paths": evidence_paths,
            "triggers": triggers or [],
            "supersedes": supersedes,
            "review_after": review_after,
        }
        self._append_event(event)
        self._reindex()
        return self.derive().get(obs_id)

    def confirm(
        self, obs_id: str, evidence_paths: list, review_after: str = ""
    ) -> Observation:
        with self._locked():
            return self._confirm_unlocked(obs_id, evidence_paths, review_after)

    def _confirm_unlocked(
        self, obs_id: str, evidence_paths: list, review_after: str
    ) -> Observation:
        state = self.derive()
        if obs_id not in state:
            raise ValueError(f"unknown observation id: {obs_id}")
        if not evidence_paths:
            raise ValueError("confirm requires at least one independent --evidence")
        # Independence re-check happens inside the lock: two concurrent
        # confirms citing the same new source must not both "win".
        existing_keys = {evidence_source_key(p) for p in state[obs_id].evidence_paths}
        seen_keys = set(existing_keys)
        new_paths = []
        for p in dict.fromkeys(evidence_paths):
            key = evidence_source_key(p)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            new_paths.append(p)
        if not new_paths:
            raise ValueError(
                "all supplied evidence sources are already recorded (same source, "
                "possibly a different #fragment) — a confirmation must cite a "
                "distinct, independent source"
            )
        self._append_event(
            {
                "type": "confirm",
                "id": obs_id,
                "evidence_paths": new_paths,
                "review_after": review_after,
            }
        )
        self._reindex()
        return self.derive().get(obs_id)

    def verify(self, obs_id: str, review_after: str = "") -> Observation:
        with self._locked():
            return self._verify_unlocked(obs_id, review_after)

    def _verify_unlocked(self, obs_id: str, review_after: str) -> Observation:
        if obs_id not in self.derive():
            raise ValueError(f"unknown observation id: {obs_id}")
        self._append_event(
            {"type": "verify", "id": obs_id, "review_after": review_after}
        )
        return self.derive().get(obs_id)

    def retire(self, obs_id: str) -> Observation:
        with self._locked():
            return self._retire_unlocked(obs_id)

    def _retire_unlocked(self, obs_id: str) -> Observation:
        if obs_id not in self.derive():
            raise ValueError(f"unknown observation id: {obs_id}")
        self._append_event({"type": "retire", "id": obs_id})
        self._reindex()
        return self.derive().get(obs_id)

    # ---- FTS5 index --------------------------------------------------------
    def _open_index(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.index_path)
        con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS obs USING fts5("
            "id UNINDEXED, status UNINDEXED, scope, claim, evidence, triggers)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)"
        )
        return con

    def _index_stale(self, con: sqlite3.Connection) -> bool:
        if not self.log_path.exists():
            return False
        row = con.execute("SELECT v FROM meta WHERE k='log_mtime'").fetchone()
        if row is None:
            return True
        try:
            return float(row[0]) < self.log_path.stat().st_mtime
        except (ValueError, OSError):
            return True

    def _reindex(self) -> None:
        """Rebuild the FTS index from current derived state."""
        state = self.derive()
        con = self._open_index()
        try:
            con.execute("DELETE FROM obs")
            for obs in state.values():
                con.execute(
                    "INSERT INTO obs (id, status, scope, claim, evidence, triggers) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        obs.id,
                        obs.status,
                        obs.scope,
                        obs.claim,
                        " ".join(obs.evidence_paths),
                        " ".join(obs.triggers),
                    ),
                )
            mtime = self.log_path.stat().st_mtime if self.log_path.exists() else 0.0
            con.execute(
                "INSERT INTO meta (k, v) VALUES ('log_mtime', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(mtime),),
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _fts_query(query: str) -> str:
        """Turn free text into a safe FTS5 MATCH string.

        Each whitespace-separated token is wrapped in double quotes (with any
        embedded quotes doubled) so characters that are FTS5 operators — '-',
        ':', '*', '(', etc. — are matched literally instead of raising. Tokens
        are implicitly ANDed. Predictable term search, no silent query errors.
        """
        tokens = [t for t in query.split() if t]
        return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)

    def search(self, query: str, limit: int = 20) -> list:
        match = self._fts_query(query)
        if not match:
            return []
        con = self._open_index()
        try:
            if self._index_stale(con):
                con.close()
                self._reindex()
                con = self._open_index()
            state = self.derive()
            rows = con.execute(
                "SELECT id FROM obs WHERE obs MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
            return [state[r[0]] for r in rows if r[0] in state]
        finally:
            con.close()

    # ---- queries -----------------------------------------------------------
    def list(self, status: str | None = None, scope: str | None = None) -> list:
        obs = list(self.derive().values())
        if status:
            obs = [o for o in obs if o.status == status]
        if scope:
            obs = [o for o in obs if scope.lower() in o.scope.lower()]
        obs.sort(key=lambda o: o.created_at)
        return obs

    def due_for_review(self, now: float | None = None) -> list:
        now_iso = _iso(now if now is not None else _now())
        out = []
        for o in self.derive().values():
            if o.status in (STATUS_SUPERSEDED, STATUS_RETIRED):
                continue
            if o.review_after and o.review_after <= now_iso:
                out.append(o)
        out.sort(key=lambda o: o.review_after)
        return out

    def promotable(self) -> list:
        """Confirmed observations eligible to be surfaced as promotion candidates."""
        return [
            o
            for o in self.derive().values()
            if o.status == STATUS_CONFIRMED
            and o.confirmed_count >= MIN_INDEPENDENT_EVIDENCE
        ]

    # ---- promotion (candidate Markdown only) -------------------------------
    def promote(self, obs_id: str | None = None) -> list:
        """Generate promotion-candidate Markdown files. Never edits AGENTS/skills.

        Returns list of written file paths. Only `confirmed` (>=2 independent
        evidence) observations are promotable.
        """
        self.promotions_dir.mkdir(parents=True, exist_ok=True)
        targets = self.promotable()
        if obs_id:
            targets = [o for o in targets if o.id == obs_id]
            if not targets:
                raise ValueError(
                    f"{obs_id} is not promotable (must be status=confirmed with "
                    f">= {MIN_INDEPENDENT_EVIDENCE} independent evidence sources)"
                )
        written = []
        for o in targets:
            path = self.promotions_dir / f"{o.id}.md"
            path.write_text(self._promotion_markdown(o), encoding="utf-8")
            written.append(path)
        return written

    @staticmethod
    def _promotion_markdown(o: Observation) -> str:
        ev = "\n".join(f"- `{p}`" for p in o.evidence_paths) or "- (none)"
        trg = ", ".join(o.triggers) if o.triggers else "(none)"
        return (
            f"# Promotion candidate: {o.id}\n\n"
            f"> **CANDIDATE — human review required.** This file is generated for "
            f"review only. It does NOT edit AGENTS.md, CLAUDE.md, or any skill. "
            f"A human decides whether/where to promote this knowledge.\n\n"
            f"- **Scope:** {o.scope}\n"
            f"- **Status:** {o.status} (confirmed_count={o.confirmed_count})\n"
            f"- **Created:** {o.created_at}\n"
            f"- **Last verified:** {o.last_verified}\n"
            f"- **Review after:** {o.review_after or '(unset)'}\n"
            f"- **Supersedes:** {o.supersedes or '(none)'}\n"
            f"- **Triggers:** {trg}\n\n"
            f"## Claim\n\n{o.claim}\n\n"
            f"## Independent evidence ({o.confirmed_count})\n\n{ev}\n"
        )
