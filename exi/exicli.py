"""`exi` command line — capture / search / list / promote / audit (+ confirm/verify/retire).

Shares distributed knowledge with evidence, makes it searchable, and surfaces
confirmed knowledge as human-reviewable promotion candidates. It never edits
AGENTS.md/CLAUDE.md or any skill.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from . import config
from . import feedback as fb
from . import feedback_detect as fbd
from . import secretscan
from .store import (
    AUTHORITATIVE_KINDS,
    MEM_KINDS,
    MIN_INDEPENDENT_EVIDENCE,
    STATUS_ACTIVE,
    STATUS_CONFIRMED,
    Store,
)

MAX_MEMORY_CLAIM_CHARS = 500
MAX_MEMORY_SCOPE_CHARS = 200
MAX_MEMORY_TRIGGER_CHARS = 200
MAX_MEMORY_EVIDENCE_CHARS = 1000
MAX_MEMORY_TRIGGERS = 8
MAX_MEMORY_EVIDENCE = 8


def _validate_memory_fields(args) -> str | None:
    """Return a safe validation error for bounded autonomous-memory fields."""
    fields = (
        ("claim", args.claim, MAX_MEMORY_CLAIM_CHARS),
        ("scope", args.scope, MAX_MEMORY_SCOPE_CHARS),
    )
    for name, value, limit in fields:
        if not (value or "").strip():
            return f"{name} must not be empty"
        if len(value) > limit:
            return f"{name} exceeds {limit} characters"
    triggers = list(args.trigger or [])
    evidence = list(args.evidence or [])
    if len(triggers) > MAX_MEMORY_TRIGGERS:
        return f"too many triggers (maximum {MAX_MEMORY_TRIGGERS})"
    if len(evidence) > MAX_MEMORY_EVIDENCE:
        return f"too many evidence sources (maximum {MAX_MEMORY_EVIDENCE})"
    if any(not v.strip() for v in triggers):
        return "triggers must not be empty"
    if any(not v.strip() for v in evidence):
        return "evidence sources must not be empty"
    if any(len(v) > MAX_MEMORY_TRIGGER_CHARS for v in triggers):
        return f"trigger exceeds {MAX_MEMORY_TRIGGER_CHARS} characters"
    if any(len(v) > MAX_MEMORY_EVIDENCE_CHARS for v in evidence):
        return f"evidence exceeds {MAX_MEMORY_EVIDENCE_CHARS} characters"
    review_after = args.review_after or ""
    if review_after and (
        len(review_after) > 64 or not re.fullmatch(r"[0-9TZ:+.\-]+", review_after)
    ):
        return "review-after must be a short ISO-style timestamp"
    return None


def _print_obs(o, verbose=False):
    line = f"{o.id}  [{o.status:9}] {o.scope} :: {o.claim}"
    print(line)
    if verbose:
        print(f"    evidence({o.confirmed_count}): {', '.join(o.evidence_paths) or '-'}")
        if o.triggers:
            print(f"    triggers: {', '.join(o.triggers)}")
        if o.supersedes:
            print(f"    supersedes: {o.supersedes}")
        print(f"    created={o.created_at} verified={o.last_verified} review_after={o.review_after or '-'}")


def cmd_capture(args, store: Store) -> int:
    o = store.capture(
        scope=args.scope,
        claim=args.claim,
        evidence_paths=args.evidence or [],
        triggers=args.trigger or [],
        supersedes=args.supersedes or "",
        review_after=args.review_after or "",
    )
    if args.json:
        print(json.dumps(o.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"captured {o.id} (status={o.status}, evidence={o.confirmed_count})")
        if o.status != STATUS_CONFIRMED:
            print(
                f"  note: needs >= {MIN_INDEPENDENT_EVIDENCE} independent evidence "
                f"sources to become 'confirmed' (currently {o.confirmed_count}). "
                f"Use `exi confirm {o.id} --evidence <distinct-source>`."
            )
    return 0


def cmd_confirm(args, store: Store) -> int:
    o = store.confirm(args.id, evidence_paths=args.evidence or [], review_after=args.review_after or "")
    if args.json:
        print(json.dumps(o.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"confirmed {o.id} -> status={o.status}, evidence={o.confirmed_count}")
    return 0


def cmd_verify(args, store: Store) -> int:
    o = store.verify(args.id, review_after=args.review_after or "")
    print(f"verified {o.id} (last_verified={o.last_verified}, review_after={o.review_after or '-'})")
    return 0


def cmd_retire(args, store: Store) -> int:
    o = store.retire(args.id)
    print(f"retired {o.id} (status={o.status})")
    return 0


def cmd_search(args, store: Store) -> int:
    results = store.search(args.query, limit=args.limit)
    if args.json:
        print(json.dumps([o.to_dict() for o in results], ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("(no matches)")
        return 0
    for o in results:
        _print_obs(o, verbose=args.verbose)
    return 0


def cmd_list(args, store: Store) -> int:
    results = store.list(status=args.status, scope=args.scope)
    if args.json:
        print(json.dumps([o.to_dict() for o in results], ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("(empty)")
        return 0
    for o in results:
        _print_obs(o, verbose=args.verbose)
    print(f"\n{len(results)} observation(s)")
    return 0


def cmd_promote(args, store: Store) -> int:
    written = store.promote(obs_id=args.id)
    if not written:
        print(
            "(nothing promotable — need status=confirmed with "
            f">= {MIN_INDEPENDENT_EVIDENCE} independent evidence sources)"
        )
        return 0
    print("Generated promotion CANDIDATE(s) — human review required, no files edited:")
    for p in written:
        print(f"  {p}")
    return 0


def cmd_audit(args, store: Store) -> int:
    state = store.derive()
    due = store.due_for_review()
    weak = [o for o in state.values() if o.status == "candidate" and o.confirmed_count < MIN_INDEPENDENT_EVIDENCE]
    superseded = [o for o in state.values() if o.status == "superseded"]
    promotable = store.promotable()
    report = {
        "total": len(state),
        "confirmed": sum(1 for o in state.values() if o.status == STATUS_CONFIRMED),
        "active": sum(1 for o in state.values() if o.status == STATUS_ACTIVE),
        "candidate": sum(1 for o in state.values() if o.status == "candidate"),
        "superseded": len(superseded),
        "retired": sum(1 for o in state.values() if o.status == "retired"),
        "due_for_review": [o.id for o in due],
        "under_evidenced_candidates": [o.id for o in weak],
        "promotable": [o.id for o in promotable],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"observations: {report['total']} "
          f"(active={report['active']}, confirmed={report['confirmed']}, "
          f"candidate={report['candidate']}, "
          f"superseded={report['superseded']}, retired={report['retired']})")
    print(f"promotable (confirmed, >= {MIN_INDEPENDENT_EVIDENCE} evidence): "
          f"{len(promotable)} -> {', '.join(report['promotable']) or '-'}")
    print(f"due for re-verification: {len(due)} -> {', '.join(report['due_for_review']) or '-'}")
    print(f"under-evidenced candidates (<{MIN_INDEPENDENT_EVIDENCE}): "
          f"{len(weak)} -> {', '.join(report['under_evidenced_candidates']) or '-'}")
    return 0


def _print_rule(r, verbose=False):
    state = "enabled" if r.enabled else "disabled"
    sev = fb.resolve_severity(r, {})
    print(f"{r.name}  [count={r.count} {sev} {state}]  {r.description}")
    if verbose:
        if r.scope:
            print(f"    scope: {r.scope}")
        if r.why:
            print(f"    why: {r.why}")
        if r.how_to_apply:
            print(f"    how to apply: {r.how_to_apply}")
        if r.excuse:
            print(f"    not an excuse: {r.excuse}")
        print(f"    evidence({len(r.evidence)}): {', '.join(r.evidence) or '-'}")
        print(f"    specs: {len(r.specs)}  violations: {len(r.violations)}")


def cmd_fb_record(args) -> int:
    store = fb.FeedbackStore()
    before = store.get(args.name)
    r = store.record(
        name=args.name,
        description=args.description,
        evidence=args.evidence,
        scope=args.scope or "",
        why=args.why or "",
        how_to_apply=args.how_to_apply or "",
        excuse=args.excuse or "",
    )
    if args.json:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    else:
        verb = "recorded (new)" if before is None else "recorded (repeat)"
        print(f"{verb} feedback {r.name!r}: count={r.count}, default severity={fb.resolve_severity(r, {})}")
    return 0


def cmd_fb_list(args) -> int:
    store = fb.FeedbackStore()
    rules = store.list()
    if args.json:
        print(json.dumps([r.to_dict() for r in rules], ensure_ascii=False, indent=2))
        return 0
    if not rules:
        print("(no feedback rules)")
        return 0
    for r in rules:
        _print_rule(r, verbose=args.verbose)
    print(f"\n{len(rules)} feedback rule(s)")
    return 0


def cmd_fb_show(args) -> int:
    store = fb.FeedbackStore()
    r = store.get(args.name)
    if r is None:
        print(f"error: unknown feedback rule: {args.name}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
        return 0
    _print_rule(r, verbose=True)
    if r.specs:
        print("    spec detail:")
        print("      " + json.dumps(r.specs, ensure_ascii=False, indent=2).replace("\n", "\n      "))
    return 0


def cmd_fb_configure(args) -> int:
    store = fb.FeedbackStore()
    try:
        specs = json.loads(args.spec_json)
    except json.JSONDecodeError as e:
        print(f"error: --spec-json is not valid JSON: {e}", file=sys.stderr)
        return 2
    r = store.configure(args.name, specs)
    if args.json:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"configured {r.name!r} with {len(r.specs)} enforcement spec(s)")
    return 0


def cmd_fb_enable(args) -> int:
    r = fb.FeedbackStore().set_enabled(args.name, True)
    print(f"enabled {r.name!r}")
    return 0


def cmd_fb_disable(args) -> int:
    r = fb.FeedbackStore().set_enabled(args.name, False)
    print(f"disabled {r.name!r}")
    return 0


def cmd_fb_resolve(args) -> int:
    """Resolve a pending, zero-click feedback candidate into a rule occurrence.

    One-shot / idempotent / session+candidate bound / expiry-checked. The
    evidence id is derived INTERNALLY from the candidate's stored prompt hash —
    the caller never supplies one — so re-recording the same human prompt cannot
    inflate a rule's count. This path can only add a new occurrence / new rule;
    it can never disable, delete, or reconfigure an existing rule.
    """
    cfg = config.load_config()
    ttl = fb.candidate_ttl_seconds(cfg)
    now = time.time()
    fbstore = fb.FeedbackStore()
    st = fb.FeedbackState()
    with st.locked() as state:
        c = fb.get_candidate(state, args.candidate)
        if c is None:
            print(f"error: unknown or expired feedback candidate: {args.candidate}", file=sys.stderr)
            return 2
        if args.session and c.get("session_id") != args.session:
            print("error: candidate belongs to a different session", file=sys.stderr)
            return 2
        status = c.get("status")
        if status == fb.CANDIDATE_RESOLVED:
            print(f"already resolved: {args.candidate} -> {c.get('rule', '?')}")
            return 0
        if status in (fb.CANDIDATE_DISMISSED, fb.CANDIDATE_ABANDONED):
            print(f"error: candidate is {status}, cannot resolve", file=sys.stderr)
            return 2
        if now - c.get("created_at", 0) > ttl:
            fb.set_candidate_status(state, args.candidate, fb.CANDIDATE_ABANDONED)
            print("error: candidate has expired", file=sys.stderr)
            return 2
        evidence = fbd.human_evidence_id(c["hash"])
        try:
            r = fbstore.record(
                name=args.name,
                description=args.description,
                evidence=evidence,
                scope=args.scope or "",
                why=args.why or "",
                how_to_apply=args.how_to_apply or "",
                excuse=args.excuse or "",
            )
        except ValueError as e:
            if "already recorded" in str(e):
                # Same human prompt hash already counted for this rule: the
                # count must NOT inflate. Treat as an idempotent success.
                r = fbstore.get(args.name)
                if r is None:
                    raise
            else:
                raise
        fb.set_candidate_status(state, args.candidate, fb.CANDIDATE_RESOLVED, rule=args.name)
    if args.json:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"resolved candidate {args.candidate} -> {r.name!r} "
              f"(count={r.count}, default severity={fb.resolve_severity(r, {})})")
    return 0


def cmd_fb_dismiss(args) -> int:
    """Dismiss a pending feedback candidate as NOT feedback, with a reason."""
    if not (args.reason or "").strip():
        print("error: dismiss requires a non-empty --reason", file=sys.stderr)
        return 2
    now = time.time()
    st = fb.FeedbackState()
    with st.locked() as state:
        c = fb.get_candidate(state, args.candidate)
        if c is None:
            print(f"error: unknown or expired feedback candidate: {args.candidate}", file=sys.stderr)
            return 2
        if args.session and c.get("session_id") != args.session:
            print("error: candidate belongs to a different session", file=sys.stderr)
            return 2
        status = c.get("status")
        if status == fb.CANDIDATE_DISMISSED:
            print(f"already dismissed: {args.candidate}")
            return 0
        if status == fb.CANDIDATE_RESOLVED:
            print("error: candidate already resolved into a rule, cannot dismiss", file=sys.stderr)
            return 2
        # Keep the reason transient: persisting free-form text could retain a
        # quote from the raw prompt.  Only fixed lifecycle metadata goes to disk.
        fb.set_candidate_status(
            state, args.candidate, fb.CANDIDATE_DISMISSED, dismissed_at=now
        )
    print(f"dismissed candidate {args.candidate} (not feedback): {args.reason}")
    return 0


def cmd_fb_candidates(args) -> int:
    """List feedback candidates from the session cache (audit/debug; no bodies)."""
    now = time.time()
    cfg = config.load_config()
    ttl = fb.candidate_ttl_seconds(cfg)
    st = fb.FeedbackState()
    with st.locked() as state:
        fb.prune_candidates(state, now, ttl)
        cands = list(state.get("candidates", []))
    if args.json:
        print(json.dumps(cands, ensure_ascii=False, indent=2))
        return 0
    if not cands:
        print("(no feedback candidates)")
        return 0
    for c in sorted(cands, key=lambda x: x.get("created_at", 0)):
        print(f"{c.get('id')}  [{c.get('status')}]  session={c.get('session_id')} "
              f"turn={c.get('turn_id')}  cues={','.join(c.get('cues', [])) or '-'}")
    print(f"\n{len(cands)} candidate(s) (prompt bodies are never stored — hash + cues only)")
    return 0


def cmd_fb_violations(args) -> int:
    store = fb.FeedbackStore()
    rules = store.list()
    if args.name:
        rules = [r for r in rules if r.name == args.name]
        if not rules:
            print(f"error: unknown feedback rule: {args.name}", file=sys.stderr)
            return 2
    rows = []
    for r in rules:
        for v in r.violations:
            rows.append({"name": r.name, **v})
    rows.sort(key=lambda v: v.get("ts", ""))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no violations recorded)")
        return 0
    for v in rows:
        print(f"{v.get('ts', '-')}  {v['name']}  [{v.get('event', '-')}]  {v.get('detail', '')}")
    print(f"\n{len(rows)} violation event(s) (count is NOT affected by these)")
    return 0


def cmd_mem_resolve(args, store: Store) -> int:
    """Resolve a pending, autonomous durable-memory candidate into an observation.

    Candidate-bound / expiry-checked / idempotent. Preference/constraint
    provenance is derived internally from the candidate's turn hash, so the same
    turn cannot inflate it. A user-authoritative preference/constraint becomes
    retrievable immediately (status ``active``). Technical kinds require actual
    cited sources; prompt hashes never count, and >=2 independent sources are
    required for ``confirmed``. This path can only append an observation or a
    distinct evidence source. It cannot rewrite a stored claim/kind, explicitly
    set trust status, supersede, retire, or delete an observation (status changes
    only as the store derives them from honest evidence counts).
    """
    kind = args.kind
    if kind not in MEM_KINDS:
        print(f"error: --kind must be one of {list(MEM_KINDS)}", file=sys.stderr)
        return 2
    field_error = _validate_memory_fields(args)
    if field_error:
        print(f"error: {field_error}", file=sys.stderr)
        return 2
    # Never let a secret land in durable memory (rejection, not redaction).
    try:
        secretscan.assert_no_secret(args.claim, "claim")
        secretscan.assert_no_secret(args.scope, "scope")
        for t in args.trigger or []:
            secretscan.assert_no_secret(t, "trigger")
        for e in args.evidence or []:
            secretscan.assert_no_secret(e, "evidence")
    except secretscan.SecretDetected as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    cfg = config.load_config()
    ttl = fb.memory_candidate_ttl_seconds(cfg)
    now = time.time()
    claim_fp = fbd.claim_fingerprint(args.scope, kind, args.claim)
    st = fb.FeedbackState()
    with st.locked() as state:
        c = fb.get_mem_candidate(state, args.candidate)
        if c is None:
            print(f"error: unknown or expired memory candidate: {args.candidate}", file=sys.stderr)
            return 2
        if args.session and c.get("session_id") != args.session:
            print("error: candidate belongs to a different session", file=sys.stderr)
            return 2
        if c.get("status") == fb.MEM_DISMISSED:
            print("error: memory candidate is dismissed, cannot resolve", file=sys.stderr)
            return 2
        if now - c.get("created_at", 0) > ttl:
            print("error: memory candidate has expired", file=sys.stderr)
            return 2
        if fb.mem_claim_already_resolved(c, claim_fp):
            print(f"already recorded this memory from candidate {args.candidate} (no change)")
            return 0
        if len(c.get("resolved_claims", [])) >= fb.MAX_MEMORIES_PER_CANDIDATE:
            print(
                f"error: memory candidate already resolved the maximum "
                f"{fb.MAX_MEMORIES_PER_CANDIDATE} distinct items",
                file=sys.stderr,
            )
            return 2
        authoritative = kind in AUTHORITATIVE_KINDS
        if authoritative:
            # The user's own turn is the honest authority/provenance source.
            evidence = [fbd.mem_evidence_id(c["hash"])] + list(args.evidence or [])
        else:
            # A prompt hash is not technical verification.  Require real cited
            # evidence and let the store's existing >=2-source discipline decide
            # when the item becomes retrievable.
            evidence = list(args.evidence or [])
            if not evidence:
                print(
                    "error: technical memory kinds require at least one verified "
                    "--evidence source; the user turn is provenance, not evidence",
                    file=sys.stderr,
                )
                return 2
        obs, action = store.remember(
            scope=args.scope,
            claim=args.claim,
            kind=kind,
            evidence_paths=evidence,
            triggers=args.trigger or [],
            authoritative=authoritative,
            review_after=args.review_after or "",
        )
        fb.mark_mem_claim_resolved(c, claim_fp)
        fb.set_mem_candidate_status(state, args.candidate, fb.MEM_RESOLVED)
    if args.json:
        out = obs.to_dict()
        out["action"] = action
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        retrievable = "retrievable now" if obs.status in (STATUS_CONFIRMED, STATUS_ACTIVE) else (
            f"not yet retrievable (needs {MIN_INDEPENDENT_EVIDENCE} independent sources; "
            f"have {obs.confirmed_count})")
        print(f"{action} memory {obs.id} [{obs.status}] ({retrievable})")
    return 0


def cmd_mem_dismiss(args, store: Store) -> int:
    """Dismiss a pending memory candidate for the audit trail (never required)."""
    if not (args.reason or "").strip():
        print("error: dismiss requires a non-empty --reason", file=sys.stderr)
        return 2
    now = time.time()
    cfg = config.load_config()
    ttl = fb.memory_candidate_ttl_seconds(cfg)
    st = fb.FeedbackState()
    with st.locked() as state:
        c = fb.get_mem_candidate(state, args.candidate)
        if c is None:
            print(f"error: unknown or expired memory candidate: {args.candidate}", file=sys.stderr)
            return 2
        if args.session and c.get("session_id") != args.session:
            print("error: candidate belongs to a different session", file=sys.stderr)
            return 2
        if now - c.get("created_at", 0) > ttl:
            print("error: memory candidate has expired", file=sys.stderr)
            return 2
        status = c.get("status")
        if status == fb.MEM_DISMISSED:
            print(f"already dismissed: {args.candidate}")
            return 0
        if status == fb.MEM_RESOLVED:
            print("error: memory candidate already resolved, cannot dismiss", file=sys.stderr)
            return 2
        # The free-form reason is useful on stdout but must never enter the
        # persisted candidate cache: it could quote private prompt content.
        fb.set_mem_candidate_status(
            state, args.candidate, fb.MEM_DISMISSED, dismissed_at=now
        )
    print(f"dismissed memory candidate {args.candidate}: {args.reason}")
    return 0


def cmd_mem_candidates(args, store: Store) -> int:
    """List memory candidates from the session cache (audit/debug; no bodies)."""
    now = time.time()
    cfg = config.load_config()
    ttl = fb.memory_candidate_ttl_seconds(cfg)
    st = fb.FeedbackState()
    with st.locked() as state:
        fb.prune_mem_candidates(state, now, ttl)
        cands = list(state.get("mem_candidates", []))
    if args.json:
        print(json.dumps(cands, ensure_ascii=False, indent=2))
        return 0
    if not cands:
        print("(no memory candidates)")
        return 0
    for c in sorted(cands, key=lambda x: x.get("created_at", 0)):
        print(f"{c.get('id')}  [{c.get('status')}]  session={c.get('session_id')} "
              f"turn={c.get('turn_id')}  resolved={len(c.get('resolved_claims', []))}")
    print(f"\n{len(cands)} memory candidate(s) (prompt bodies are never stored — hash only)")
    return 0


def _add_memory_parser(sub, common):
    mp = sub.add_parser("memory", parents=[common],
                        help="autonomous durable-memory candidates (resolve/dismiss/candidates)")
    msub = mp.add_subparsers(dest="memcmd", required=True)

    r = msub.add_parser("resolve", parents=[common],
                        help="resolve a pending memory candidate into a durable observation "
                             "(evidence source derived internally; candidate-bound/idempotent)")
    r.add_argument("--candidate", required=True, help="internal candidate id (from the hook instruction)")
    r.add_argument("--claim", required=True, help="the concise canonical claim to remember")
    r.add_argument("--scope", required=True, help="area this applies to, e.g. 'env/gpu'")
    r.add_argument("--kind", required=True, choices=list(MEM_KINDS),
                   help="preference/constraint are user-authoritative; the rest need evidence")
    r.add_argument("--trigger", action="append", help="when this is relevant (repeatable)")
    r.add_argument("--evidence", action="append",
                   help="verified source (required for technical kinds; repeat for independent sources)")
    r.add_argument("--review-after", dest="review_after", help="ISO timestamp after which to re-verify")
    r.add_argument("--session", help="optional: assert the candidate's session id")
    r.set_defaults(func=cmd_mem_resolve)

    r = msub.add_parser("dismiss", parents=[common],
                        help="dismiss a pending memory candidate for the audit trail (optional)")
    r.add_argument("--candidate", required=True)
    r.add_argument("--reason", required=True, help="why nothing was worth remembering")
    r.add_argument("--session", help="optional: assert the candidate's session id")
    r.set_defaults(func=cmd_mem_dismiss)

    r = msub.add_parser("candidates", parents=[common],
                        help="list memory candidates (hash only, no bodies)")
    r.set_defaults(func=cmd_mem_candidates)


def _add_feedback_parser(sub, common):
    fbp = sub.add_parser("feedback", parents=[common], help="recurring-feedback rules + graded enforcement")
    # Feedback subcommands manage their own FeedbackStore, so ignore the passed Store.
    fbp.set_defaults(func=lambda args, store: args.fbfunc(args))
    fsub = fbp.add_subparsers(dest="fbcmd", required=True)

    r = fsub.add_parser("record", parents=[common],
                        help="[optional/advanced] manually record a human feedback occurrence "
                             "(count = distinct evidence). The zero-click loop uses `resolve` instead.")
    r.add_argument("--name", required=True)
    r.add_argument("--description", required=True)
    r.add_argument("--evidence", required=True, help="identifier of THIS occurrence (duplicate ids are rejected)")
    r.add_argument("--scope")
    r.add_argument("--why")
    r.add_argument("--how-to-apply", dest="how_to_apply")
    r.add_argument("--excuse", help="a non-excuse to reject explicitly")
    r.set_defaults(fbfunc=cmd_fb_record)

    r = fsub.add_parser("resolve", parents=[common],
                        help="resolve a pending auto-detected feedback candidate into a rule "
                             "(evidence id derived internally; one-shot/idempotent)")
    r.add_argument("--candidate", required=True, help="internal candidate id (from the hook instruction)")
    r.add_argument("--name", required=True, help="rule to record/merge into (existing or new)")
    r.add_argument("--description", required=True, help="the canonical rule text")
    r.add_argument("--scope")
    r.add_argument("--why")
    r.add_argument("--how-to-apply", dest="how_to_apply")
    r.add_argument("--excuse", help="a non-excuse to reject explicitly")
    r.add_argument("--session", help="optional: assert the candidate's session id")
    r.set_defaults(fbfunc=cmd_fb_resolve)

    r = fsub.add_parser("dismiss", parents=[common],
                        help="dismiss a pending feedback candidate as NOT feedback, with a reason")
    r.add_argument("--candidate", required=True)
    r.add_argument("--reason", required=True, help="why this prompt is not corrective feedback")
    r.add_argument("--session", help="optional: assert the candidate's session id")
    r.set_defaults(fbfunc=cmd_fb_dismiss)

    r = fsub.add_parser("candidates", parents=[common],
                        help="list auto-detected feedback candidates (hash + cues only, no bodies)")
    r.set_defaults(fbfunc=cmd_fb_candidates)

    r = fsub.add_parser("list", parents=[common], help="list feedback rules")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fbfunc=cmd_fb_list)

    r = fsub.add_parser("show", parents=[common], help="show one feedback rule")
    r.add_argument("name")
    r.set_defaults(fbfunc=cmd_fb_show)

    r = fsub.add_parser("configure", parents=[common], help="attach declarative enforcement spec(s) (JSON)")
    r.add_argument("name")
    r.add_argument("--spec-json", dest="spec_json", required=True, help="a JSON spec object or list of objects")
    r.set_defaults(fbfunc=cmd_fb_configure)

    r = fsub.add_parser("enable", parents=[common], help="enable a feedback rule")
    r.add_argument("name")
    r.set_defaults(fbfunc=cmd_fb_enable)

    r = fsub.add_parser("disable", parents=[common], help="disable a feedback rule")
    r.add_argument("name")
    r.set_defaults(fbfunc=cmd_fb_disable)

    r = fsub.add_parser("violations", parents=[common], help="show recorded violation events")
    r.add_argument("name", nargs="?")
    r.set_defaults(fbfunc=cmd_fb_violations)


def build_parser() -> argparse.ArgumentParser:
    # `--json` is accepted both globally and per-subcommand via a shared parent.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")

    p = argparse.ArgumentParser(prog="exi", description=__doc__, parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", parents=[common], help="record a new observation (starts as candidate)")
    c.add_argument("--scope", required=True, help="area this applies to, e.g. 'backend/deploy-pipeline'")
    c.add_argument("--claim", required=True, help="the knowledge, one clear statement")
    c.add_argument("--evidence", action="append", help="path/URL/source backing the claim (repeatable, required)")
    c.add_argument("--trigger", action="append", help="when this knowledge is relevant (repeatable)")
    c.add_argument("--supersedes", help="id of an observation this replaces")
    c.add_argument("--review-after", dest="review_after", help="ISO timestamp after which to re-verify")
    c.set_defaults(func=cmd_capture)

    c = sub.add_parser("confirm", parents=[common], help="add an INDEPENDENT evidence source to an observation")
    c.add_argument("id")
    c.add_argument("--evidence", action="append", required=True, help="distinct source (repeatable)")
    c.add_argument("--review-after", dest="review_after")
    c.set_defaults(func=cmd_confirm)

    c = sub.add_parser("verify", parents=[common], help="mark an observation re-verified now")
    c.add_argument("id")
    c.add_argument("--review-after", dest="review_after")
    c.set_defaults(func=cmd_verify)

    c = sub.add_parser("retire", parents=[common], help="retire an observation (no longer true/useful)")
    c.add_argument("id")
    c.set_defaults(func=cmd_retire)

    c = sub.add_parser("search", parents=[common], help="full-text search (SQLite FTS5)")
    c.add_argument("query")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_search)

    c = sub.add_parser("list", parents=[common], help="list observations")
    c.add_argument(
        "--status", choices=["candidate", "active", "confirmed", "superseded", "retired"]
    )
    c.add_argument("--scope")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_list)

    c = sub.add_parser("promote", parents=[common], help="generate promotion-candidate Markdown (no AGENTS/skill edits)")
    c.add_argument("id", nargs="?", help="specific observation id (default: all promotable)")
    c.set_defaults(func=cmd_promote)

    c = sub.add_parser("audit", parents=[common], help="show review-due, weak, and promotable observations")
    c.set_defaults(func=cmd_audit)

    _add_feedback_parser(sub, common)
    _add_memory_parser(sub, common)

    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    # A subparser's own --json default (False) clobbers a global --json given
    # before the subcommand; recover it by OR-ing across the raw argv.
    args.json = bool(getattr(args, "json", False)) or ("--json" in argv)
    store = Store()
    try:
        return args.func(args, store)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
