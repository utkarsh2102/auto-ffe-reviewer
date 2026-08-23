"""Command-line entry point.

Subcommands are separable on purpose. A Release Team member investigating one
bug should be able to run the evidence gathering alone and read the result,
without spending a model call or touching stored state -- and the CI job should
be able to do the same stages in sequence. Debugging a bad review means
re-running one stage, not the whole thing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ffe.config import Settings, load_settings
from ffe.errors import ConfigError, FfeError
from ffe.evidence.builder import build as build_evidence
from ffe.evidence.fingerprint import fingerprint as compute_fingerprint
from ffe.evidence.fingerprint import semantic_subset
from ffe.llm.prompt import load_policy
from ffe.llm.registry import build_harness
from ffe.models import to_jsonable
from ffe.pipeline import Pipeline, RunSummary
from ffe.risk.gates import evaluate as apply_gates
from ffe.risk.score import assess as score_risk
from ffe.risk.signals import derive as derive_signals
from ffe.sources.base import SourceContext
from ffe.sources.launchpad import client as launchpad_client
from ffe.sources.release_calendar import development_series, load_series, release_context
from ffe.sources.seeds import load_flavours
from ffe.store.repo import Store
from ffe.util.cache import Cache
from ffe.util.http import HttpClient, RequestsTransport

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "assessment.v1.schema.json"


def build_context(settings: Settings) -> SourceContext:
    return SourceContext(
        http=HttpClient(
            transport=RequestsTransport(settings.launchpad.user_agent),
            default_timeout=settings.timeouts.default,
        ),
        cache=Cache(settings.cache_dir),
        settings=settings,
    )


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _pipeline(settings: Settings, *, use_llm: bool) -> Pipeline:
    ctx = build_context(settings)
    return Pipeline(
        settings=settings,
        ctx=ctx,
        store=Store(settings.state_dir),
        launchpad=launchpad_client(ctx),
        harness=build_harness(settings) if use_llm else None,
        policy=load_policy(),
        schema=_schema(),
        flavours=load_flavours(),
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_discover(args: argparse.Namespace, settings: Settings) -> int:
    """Show the review queue, and anything that looks like an FFe but is not in it."""
    ctx = build_context(settings)
    lp = launchpad_client(ctx)

    queue = lp.review_queue()
    if not queue.ok or queue.value is None:
        print(f"could not read the queue: {queue.note}", file=sys.stderr)
        return 1

    team = settings.launchpad.release_team
    if not queue.value:
        print(f"~{team} is subscribed to no open bugs: nothing is awaiting review.")
    else:
        print(f"{len(queue.value)} bug(s) awaiting review by ~{team}:\n")
        for ref in queue.value:
            print(f"  LP #{ref.id}  [{ref.status}]  {ref.title[:80]}")
            print(f"      {ref.web_link}")

    if settings.discovery.secondary_sweep:
        devel = development_series()
        sweep = lp.ffe_sweep(created_since=devel.created.isoformat() if devel else None)
        if sweep.ok and sweep.value:
            known = {ref.id for ref in queue.value}
            gaps = [ref for ref in sweep.value if ref.id not in known]
            if gaps:
                print(
                    f"\n{len(gaps)} bug(s) look like FFe requests but ~{team} is not "
                    "subscribed, so they are in nobody's queue:\n"
                )
                for ref in gaps[: args.limit]:
                    print(f"  LP #{ref.id}  [{ref.status}]  {ref.title[:80]}")
    return 0


def cmd_evidence(args: argparse.Namespace, settings: Settings) -> int:
    """Gather evidence for one bug and print it, with no model involved."""
    ctx = build_context(settings)
    lp = launchpad_client(ctx)

    bug = lp.fetch_bug(args.bug)
    if not bug.ok or bug.value is None:
        print(f"could not read bug {args.bug}: {bug.note}", file=sys.stderr)
        return 1

    devel = development_series()
    result = build_evidence(
        ctx,
        bug.value,
        launchpad=lp,
        flavours=load_flavours(),
        known_series=tuple(s.series for s in load_series()),
        default_series=devel.series if devel else None,
    )
    bundle = result.bundle
    signals = derive_signals(bundle)
    risk = apply_gates(signals, score_risk(signals))

    if args.json:
        print(
            json.dumps(
                {
                    "evidence": to_jsonable(bundle),
                    "risk": to_jsonable(risk),
                    "fingerprint": compute_fingerprint(bundle),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    _print_evidence(bundle, risk, result.notes)
    return 0


def _note(fact: Any, *, indent: int) -> None:
    """Print a fact's caveat, if it carries one.

    Facts come with qualifications -- served from a stale cache, drawn from a
    seed index that looks incomplete -- and those belong next to the value,
    not buried in JSON. A number a reviewer cannot fully trust should say so
    where they read it.
    """
    if getattr(fact, "note", None):
        print(f"{' ' * indent}({fact.note})")


def _print_evidence(bundle: Any, risk: Any, notes: tuple[str, ...]) -> None:
    """A human-readable summary, shaped like what the dashboard shows."""
    bug = bundle.bug
    print(f"LP #{bug.id}  {bug.title}")
    print(f"  {bug.url}\n")

    context = bundle.release_context.value
    if context:
        print(
            f"  Release   {context.series} ({context.version}) {context.release_type.value}, "
            f"phase {context.phase}, {context.days_to_release} days to release"
        )
        _note(bundle.release_context, indent=12)
    print(f"  Packages  {', '.join(bundle.subject.packages) or '(none identified)'}")
    print(f"  Kind      {bundle.subject.ffe_kind.value}")

    for package in bundle.packages:
        seeds = package.seeds.value
        seeded = ", ".join(seeds.flavours) if seeds and seeds.flavours else "not seeded"
        if not package.seeds.ok:
            seeded = f"unknown ({package.seeds.status.value})"
        rdeps = (
            package.rdepends.value.total if package.rdepends.ok and package.rdepends.value else "?"
        )
        builds = (
            package.build_rdepends.value.total
            if package.build_rdepends.ok and package.build_rdepends.value
            else "?"
        )
        print(f"\n  {package.name}")
        print(f"    seeded on   {seeded}")
        _note(package.seeds, indent=16)
        print(f"    rdepends    {rdeps} runtime, {builds} build")
        _note(package.rdepends, indent=16)
        _note(package.build_rdepends, indent=16)

    testing = bundle.testing
    print(
        f"\n  Testing   ppa={testing.ppa.value} build={testing.build.value} "
        f"tests={testing.autopkgtest.value} output={testing.test_output.value}"
    )
    if testing.unverified_claims:
        print("    claimed but not checkable:")
        for claim in testing.unverified_claims:
            print(f"      - {claim}")

    print(f"\n  Flavours  {bundle.flavours.impact.value}")
    if bundle.flavours.ack_required_from:
        print(f"    awaiting acknowledgement from: {', '.join(bundle.flavours.ack_required_from)}")

    print(f"\n  Risk      {risk.score}/100 {risk.band.value}")
    for component in risk.components:
        print(
            f"    {component.points:>3}/{component.max_points:<3} {component.name}: {component.rationale}"
        )

    triggered = [g for g in risk.gates if g.triggered]
    if triggered:
        print("\n  Gates")
        for gate in triggered:
            print(f"    [{gate.effect.value}] {gate.id}: {gate.rationale}")

    if risk.decision_floor:
        print(f"\n  Decision floor: {risk.decision_floor.value}")
    print(f"  Confidence ceiling: {risk.confidence_ceiling.value}")

    if bundle.injection_signals:
        print("\n  Suspicious passages in the bug text:")
        for signal in bundle.injection_signals:
            print(f"    [{signal.pattern_id}] {signal.location}: {signal.excerpt[:100]}")

    if bundle.unavailable:
        print("\n  Could not consult:")
        for item in bundle.unavailable:
            print(f"    {item.source_id}: {item.reason}")
            print(f"      -> {item.impact}")

    for note in notes:
        print(f"\n  Note: {note}")


def cmd_review(args: argparse.Namespace, settings: Settings) -> int:
    """Review specific bugs, or everything in the queue."""
    pipeline = _pipeline(settings, use_llm=not args.no_llm)
    summary = pipeline.run(force=args.force, only=tuple(args.bug or ()))
    _print_summary(summary)
    return 1 if summary.errors and not summary.reviewed else 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """One full cycle, as the scheduled job runs it."""
    pipeline = _pipeline(settings, use_llm=not args.no_llm)
    summary = pipeline.run(force=args.force)
    _print_summary(summary)

    from ffe.publish.render import publish

    written = publish(Store(settings.state_dir), settings)
    print(f"\nwrote {written} dashboard file(s) to {settings.state_dir / 'site'}")
    return 0


def _print_summary(summary: RunSummary) -> None:
    print(f"run {summary.run_id}")
    print(f"  queue            {summary.queue_size}")
    print(f"  reviewed         {len(summary.reviewed)} {summary.reviewed or ''}")
    print(f"  unchanged        {len(summary.skipped_unchanged)}")
    print(f"  decided by team  {len(summary.archived)} {summary.archived or ''}")
    print(f"  model calls      {summary.llm_calls}")
    if summary.lessons_learned:
        print(
            f"  precedents       {len(summary.lessons_learned)} learned {summary.lessons_learned}"
        )
    if summary.deferred_budget:
        print(f"  deferred         {summary.deferred_budget} (budget)")
    if summary.process_gaps:
        print(
            f"  process gaps     {len(summary.process_gaps)} FFe-shaped bugs with nobody subscribed"
        )
    if summary.unavailable_sources:
        print(f"  unavailable      {', '.join(summary.unavailable_sources)}")
    for error in summary.errors:
        print(f"  error            {error}")


def cmd_publish(args: argparse.Namespace, settings: Settings) -> int:
    from ffe.publish.render import publish

    written = publish(Store(settings.state_dir), settings)
    print(f"wrote {written} file(s) to {settings.state_dir / 'site'}")
    return 0


def cmd_calendar(args: argparse.Namespace, settings: Settings) -> int:
    """Show where a release is in its cycle."""
    ctx = build_context(settings)
    fact = release_context(ctx, series=args.series)
    if not fact.ok or fact.value is None:
        print(f"could not resolve the release calendar: {fact.note}", file=sys.stderr)
        return 1

    context = fact.value
    print(f"{context.series} ({context.version}) {context.release_type.value}")
    print(f"  derived from    {context.derivation}")
    print(f"  feature freeze  {context.feature_freeze}")
    print(f"  release         {context.release_date}")
    print(f"  phase           {context.phase}")
    print(f"  days to release {context.days_to_release} ({context.days_bucket})")
    if context.freeze_window_elapsed_pct is not None:
        print(f"  freeze window   {context.freeze_window_elapsed_pct}% elapsed")
    if fact.note:
        print(f"  note            {fact.note}")
    print("\n  milestones")
    for milestone in context.milestones:
        print(f"    {milestone.date}  {milestone.name}")
    return 0


def cmd_policy_hash(args: argparse.Namespace, settings: Settings) -> int:
    """Print the policy version and hash, for policy/manifest.toml."""
    policy = load_policy()
    print(f"version     {policy.version}")
    print(f"policy_hash {policy.policy_hash}")
    print(f"parts       {len(policy.parts)}")
    print(f"characters  {len(policy.text)}")
    return 0


def cmd_learn(args: argparse.Namespace, settings: Settings) -> int:
    """Extract precedents from decisions that differed from our advice."""
    pipeline = _pipeline(settings, use_llm=True)
    store = Store(settings.state_dir)

    from ffe.learning.detect import find_disagreements, learnable

    already = frozenset(
        int(lesson.get("source_bug", 0)) for lesson in store.lessons(active_only=False)
    )
    disagreements = find_disagreements(store, already_learned=already)
    candidates = learnable(disagreements)

    print(f"{len(disagreements)} disagreement(s) not yet learned from")
    print(f"{len(candidates)} with an explanation from a Release Team member")
    if args.dry_run:
        for item in candidates:
            print(
                f"  LP #{item.bug_id}: we said {item.our_decision}, team said {item.human_decision}"
            )
        return 0

    learned = pipeline.learn(limit=args.limit)
    if not learned:
        print("no precedents extracted")
        return 0
    for lesson in learned:
        print(f"\nlearned {lesson['lesson_id']} from LP #{lesson['source_bug']}")
        print(f"  {lesson['lesson']}")
        print(f'  {lesson["rationale_author"]}: "{lesson["rationale_quote"]}"')
    return 0


def cmd_lessons(args: argparse.Namespace, settings: Settings) -> int:
    """List, or retire, learned precedents."""
    store = Store(settings.state_dir)

    if args.retire:
        if store.retire_lesson(args.retire, args.reason or "retired by hand"):
            print(f"retired {args.retire}")
            return 0
        print(f"no such lesson: {args.retire}", file=sys.stderr)
        return 1

    lessons = store.lessons(active_only=not args.all)
    if not lessons:
        print("no lessons learned yet")
        return 0

    for lesson in lessons:
        print(
            f"{lesson.get('lesson_id')}  [{lesson.get('status')}]  LP #{lesson.get('source_bug')}"
        )
        print(f"  situation  {lesson.get('situation')}")
        print(f"  we said    {lesson.get('our_decision')}")
        print(f"  team said  {lesson.get('human_decision')}")
        print(f"  lesson     {lesson.get('lesson')}")
        print(f'  {lesson.get("rationale_author")}: "{lesson.get("rationale_quote")}"\n')
    return 0


def cmd_fingerprint(args: argparse.Namespace, settings: Settings) -> int:
    """Show what a bug's change-detection fingerprint is made of.

    For answering "why did this get re-reviewed": diff two of these rather
    than comparing two hashes that happen not to match.
    """
    ctx = build_context(settings)
    lp = launchpad_client(ctx)
    bug = lp.fetch_bug(args.bug)
    if not bug.ok or bug.value is None:
        print(f"could not read bug {args.bug}: {bug.note}", file=sys.stderr)
        return 1

    devel = development_series()
    bundle = build_evidence(
        ctx,
        bug.value,
        launchpad=lp,
        flavours=load_flavours(),
        known_series=tuple(s.series for s in load_series()),
        default_series=devel.series if devel else None,
    ).bundle
    print(json.dumps(semantic_subset(bundle), indent=2, sort_keys=True))
    print(f"\nfingerprint: {compute_fingerprint(bundle)}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ffe",
        description=(
            "Gather evidence for Ubuntu Feature Freeze Exception requests and produce "
            "advisory recommendations. Every decision remains a human one."
        ),
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--state-dir", type=Path, help="where reviews are stored")
    parser.add_argument("--cache-dir", type=Path, help="where fetched evidence is cached")
    parser.add_argument("--offline", action="store_true", help="use cached evidence only")

    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="show the review queue")
    discover.add_argument("--limit", type=int, default=20)
    discover.set_defaults(func=cmd_discover)

    evidence = sub.add_parser("evidence", help="gather evidence for one bug, without a model")
    evidence.add_argument("--bug", type=int, required=True)
    evidence.add_argument("--json", action="store_true")
    evidence.set_defaults(func=cmd_evidence)

    review = sub.add_parser("review", help="review the queue, or specific bugs")
    review.add_argument("--bug", type=int, action="append")
    review.add_argument("--no-llm", action="store_true", help="deterministic assessment only")
    review.add_argument("--force", action="store_true", help="review even if nothing changed")
    review.set_defaults(func=cmd_review)

    run = sub.add_parser("run", help="one full cycle: review, then publish")
    run.add_argument("--no-llm", action="store_true")
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=cmd_run)

    publish = sub.add_parser("publish", help="regenerate the dashboard")
    publish.set_defaults(func=cmd_publish)

    calendar = sub.add_parser("calendar", help="show where a release is in its cycle")
    calendar.add_argument("--series", help="codename or version; default is the devel series")
    calendar.set_defaults(func=cmd_calendar)

    learn = sub.add_parser("learn", help="extract precedents from Release Team decisions")
    learn.add_argument("--limit", type=int, default=5)
    learn.add_argument("--dry-run", action="store_true", help="show candidates without extracting")
    learn.set_defaults(func=cmd_learn)

    lessons = sub.add_parser("lessons", help="list or retire learned precedents")
    lessons.add_argument("--all", action="store_true", help="include retired lessons")
    lessons.add_argument("--retire", metavar="LESSON_ID")
    lessons.add_argument("--reason")
    lessons.set_defaults(func=cmd_lessons)

    fingerprint = sub.add_parser("fingerprint", help="explain a bug's change-detection fingerprint")
    fingerprint.add_argument("--bug", type=int, required=True)
    fingerprint.set_defaults(func=cmd_fingerprint)

    policy_hash = sub.add_parser("policy-hash", help="print the policy version and hash")
    policy_hash.set_defaults(func=cmd_policy_hash)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config, offline=args.offline)
        if args.state_dir:
            settings = _with(settings, state_dir=args.state_dir)
        if args.cache_dir:
            settings = _with(settings, cache_dir=args.cache_dir)
        return int(args.func(args, settings))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except FfeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _with(settings: Settings, **overrides: Any) -> Settings:
    from dataclasses import replace

    return replace(settings, **overrides)


if __name__ == "__main__":
    raise SystemExit(main())
