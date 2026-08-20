"""Assemble everything known about one FFe request.

Order matters here, because later lookups depend on earlier ones. The archive
publication comes first, since it yields the binary names without which the
seed index cannot be queried at all -- that index is keyed by binary, and a
source rarely shares a name with the binary that matters. Looking up only
"curl" would miss libcurl4t64, which is what the archive actually depends on.

Every step is allowed to fail. A bundle with three of five facts is still worth
reviewing, and the missing ones are listed with what they cost us, so the
deterministic layer can lower the confidence ceiling accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ffe.evidence import injection
from ffe.evidence.parse_bug import parse_subject
from ffe.evidence.testing import collect as collect_testing
from ffe.models import (
    BugFacts,
    EvidenceBundle,
    Fact,
    FactStatus,
    FlavourAck,
    FlavourFacts,
    FlavourImpact,
    PackageFacts,
    RdepInfo,
    SeedInfo,
    SubjectFacts,
)
from ffe.sources import rdepends as rdeps_source
from ffe.sources import seeds as seeds_source
from ffe.sources.base import SourceContext, not_applicable, unavailable
from ffe.sources.launchpad import LaunchpadClient
from ffe.sources.release_calendar import release_context
from ffe.sources.seeds import FlavourConfig, classify_impact, load_flavours

# Cap on packages investigated per bug. A transition FFe can name dozens; past
# a handful the reviewer is reading a transition plan, not a package summary,
# and the extra lookups cost far more than they inform.
MAX_PACKAGES = 6


@dataclass(frozen=True, slots=True)
class BuildResult:
    bundle: EvidenceBundle
    notes: tuple[str, ...] = ()


def build(
    ctx: SourceContext,
    bug: BugFacts,
    *,
    launchpad: LaunchpadClient,
    flavours: FlavourConfig | None = None,
    known_series: tuple[str, ...] = (),
    default_series: str | None = None,
) -> BuildResult:
    """Gather all evidence for one bug."""
    flavours = flavours or load_flavours()
    notes: list[str] = []

    parsed = parse_subject(bug, known_series=known_series, default_series=default_series)
    subject = parsed.subject
    notes.extend(parsed.notes)

    calendar = release_context(ctx, series=subject.target_series)
    series = subject.target_series or (
        calendar.value.series if calendar.ok and calendar.value else ""
    )

    packages, seed_union = _package_facts(
        ctx, subject, series=series, launchpad=launchpad, flavours=flavours, notes=notes
    )

    testing = collect_testing(bug, launchpad=launchpad)
    flavour_facts = _flavour_facts(bug, seed_union, flavours=flavours, launchpad=launchpad)

    bundle = EvidenceBundle(
        bug=bug,
        subject=subject,
        release_context=calendar,
        packages=packages,
        testing=testing,
        flavours=flavour_facts,
        injection_signals=injection.scan_bug(bug),
        unavailable=tuple(ctx.unavailable),
    )
    return BuildResult(bundle=bundle, notes=tuple(notes))


def _package_facts(
    ctx: SourceContext,
    subject: SubjectFacts,
    *,
    series: str,
    launchpad: LaunchpadClient,
    flavours: FlavourConfig,
    notes: list[str],
) -> tuple[tuple[PackageFacts, ...], SeedInfo | None]:
    """Gather per-package evidence, and the union of seeding across them."""
    if not subject.packages:
        return (), None

    chosen = subject.packages[:MAX_PACKAGES]
    if len(subject.packages) > MAX_PACKAGES:
        notes.append(
            f"{len(subject.packages)} packages are affected; evidence was gathered for the "
            f"first {MAX_PACKAGES} ({', '.join(chosen)})"
        )

    facts: list[PackageFacts] = []
    all_flavours: set[str] = set()
    all_images: set[tuple[str, str]] = set()
    all_seeded: set[str] = set()
    any_core = False
    saw_seeds = False

    for name in chosen:
        archive = (
            launchpad.published_source(name, series)
            if series
            else unavailable("launchpad.archive", name, "target series is unknown")
        )

        # The seed index is keyed by binary, so the archive's binary list is
        # what makes this lookup meaningful. Falling back to the source name is
        # better than nothing for a package not yet published.
        binaries = list(archive.value.binaries) if archive.ok and archive.value else []
        if not binaries:
            binaries = [name]

        seeds = seeds_source.seed_info(ctx, binaries, flavours=flavours)
        if seeds.ok and seeds.value:
            saw_seeds = True
            all_flavours.update(seeds.value.flavours)
            all_images.update(seeds.value.images)
            all_seeded.update(seeds.value.binaries_seeded)
            any_core = any_core or seeds.value.is_core

        binary_rdeps: Fact[RdepInfo]
        build_rdeps: Fact[RdepInfo]
        if archive.ok and archive.value and not archive.value.in_archive:
            # Nothing can depend on a package that is not published yet, so
            # this is a real answer rather than a failed lookup.
            reason = f"{name} is not published in {series}"
            binary_rdeps = not_applicable(f"{rdeps_source.SOURCE_ID}.binary", name, reason)
            build_rdeps = not_applicable(f"{rdeps_source.SOURCE_ID}.build", name, reason)
        else:
            seed_value = seeds.value if seeds.ok else None
            binary_rdeps = rdeps_source.binary_rdepends(ctx, name, series=series, seeded=seed_value)
            build_rdeps = rdeps_source.build_rdepends(ctx, name, series=series, seeded=seed_value)

        facts.append(
            PackageFacts(
                name=name,
                archive=archive,
                seeds=seeds,
                rdepends=binary_rdeps,
                build_rdepends=build_rdeps,
            )
        )

    union = (
        SeedInfo(
            flavours=tuple(sorted(all_flavours)),
            images=tuple(sorted(all_images)),
            is_core=any_core,
            binaries_seeded=tuple(sorted(all_seeded)),
            binaries_checked=tuple(chosen),
        )
        if saw_seeds
        else None
    )
    return tuple(facts), union


def _flavour_facts(
    bug: BugFacts,
    seeded: SeedInfo | None,
    *,
    flavours: FlavourConfig,
    launchpad: LaunchpadClient,
) -> FlavourFacts:
    """Work out whose product this touches, and who has said yes.

    A change confined to one flavour's image is that flavour's call. Once two
    or more are affected it is nobody's call alone, and the teams that have not
    spoken are named so the reviewer can see who is missing rather than having
    to work it out.
    """
    if seeded is None:
        return FlavourFacts()

    impact = classify_impact(seeded, flavours)
    affected = tuple(f for f in seeded.flavours if not flavours.is_core(f))

    acks: list[FlavourAck] = []
    commenters = {c.author for c in bug.comments if c.index > 0 and c.author}
    requesting: str | None = None

    for flavour in affected:
        team = flavours.teams.get(flavour, "")
        if not team:
            # No team configured, so an acknowledgement cannot be attributed.
            # Reported as unverified rather than silently accepted.
            continue
        members = launchpad.team_members(team)
        if not members:
            continue
        if bug.reporter in members:
            requesting = requesting or flavour
        for author in sorted(commenters & members):
            comment = next(c for c in bug.comments if c.author == author and c.index > 0)
            acks.append(
                FlavourAck(
                    flavour=flavour,
                    author=author,
                    message_url=comment.url,
                    author_in_flavour_team=True,
                )
            )

    acknowledged = {a.flavour for a in acks} | ({requesting} if requesting else set())
    # Only a multi-flavour change needs sign-off from anyone else. A single
    # flavour's own request needs nobody's permission but their own.
    required = (
        tuple(sorted(f for f in affected if f not in acknowledged))
        if impact is FlavourImpact.MULTI_FLAVOUR
        else ()
    )

    return FlavourFacts(
        impact=impact,
        affected_flavours=affected,
        requesting_flavour=requesting,
        requester_is_flavour_lead=bool(requesting) if affected else None,
        acks=tuple(acks),
        ack_required_from=required,
    )


def with_fingerprint(bundle: EvidenceBundle, fingerprint: str) -> EvidenceBundle:
    """Attach a computed fingerprint to a bundle."""
    return replace(bundle, fingerprint=fingerprint)


def unavailable_sources(bundle: EvidenceBundle) -> tuple[str, ...]:
    """Source ids we could not consult, for the confidence ceiling."""
    names = {u.source_id for u in bundle.unavailable}
    for package in bundle.packages:
        for fact in (package.archive, package.seeds, package.rdepends, package.build_rdepends):
            if fact.status is FactStatus.UNAVAILABLE:
                names.add(fact.provenance.source_id)
    if bundle.release_context.status is FactStatus.UNAVAILABLE:
        names.add(bundle.release_context.provenance.source_id)
    return tuple(sorted(names))
