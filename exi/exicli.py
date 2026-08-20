"""`exi` command line — capture / search / list / promote / audit (+ confirm/verify/retire).

Shares distributed knowledge with evidence, makes it searchable, and surfaces
confirmed knowledge as human-reviewable promotion candidates. It never edits
AGENTS.md/CLAUDE.md or any skill.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import feedback as fb
from .store import STATUS_CONFIRMED, MIN_INDEPENDENT_EVIDENCE, Store


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
          f"(confirmed={report['confirmed']}, candidate={report['candidate']}, "
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


def _add_feedback_parser(sub, common):
    fbp = sub.add_parser("feedback", parents=[common], help="recurring-feedback rules + graded enforcement")
    # Feedback subcommands manage their own FeedbackStore, so ignore the passed Store.
    fbp.set_defaults(func=lambda args, store: args.fbfunc(args))
    fsub = fbp.add_subparsers(dest="fbcmd", required=True)

    r = fsub.add_parser("record", parents=[common], help="record a human feedback occurrence (count = distinct evidence)")
    r.add_argument("--name", required=True)
    r.add_argument("--description", required=True)
    r.add_argument("--evidence", required=True, help="identifier of THIS occurrence (duplicate ids are rejected)")
    r.add_argument("--scope")
    r.add_argument("--why")
    r.add_argument("--how-to-apply", dest="how_to_apply")
    r.add_argument("--excuse", help="a non-excuse to reject explicitly")
    r.set_defaults(fbfunc=cmd_fb_record)

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
    c.add_argument("--status", choices=["candidate", "confirmed", "superseded", "retired"])
    c.add_argument("--scope")
    c.add_argument("-v", "--verbose", action="store_true")
    c.set_defaults(func=cmd_list)

    c = sub.add_parser("promote", parents=[common], help="generate promotion-candidate Markdown (no AGENTS/skill edits)")
    c.add_argument("id", nargs="?", help="specific observation id (default: all promotable)")
    c.set_defaults(func=cmd_promote)

    c = sub.add_parser("audit", parents=[common], help="show review-due, weak, and promotable observations")
    c.set_defaults(func=cmd_audit)

    _add_feedback_parser(sub, common)

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
