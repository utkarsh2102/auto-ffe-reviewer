"""Scoring risk, with every point attributable.

The score is a triage aid, not a verdict. Its job is to sort a queue so the
Release Team looks at the alarming things first, and to give the model a
grounded starting point rather than a blank page. Nothing is approved or
rejected because of a number.

Two properties matter more than the exact weights:

**Every component says why.** A band of HIGH is useless on its own; "25/25 for
being on a core image, 20/25 for 711 reverse dependencies" is something a
reviewer can agree or disagree with in seconds, and can overrule with reasons.

**Unknown is not zero.** Where a fact could not be established the component
scores at a cautious midpoint and says so, rather than scoring low and quietly
implying safety. A seed index we could not read must not make a package look
like a leaf.

Weights sum to 100. They encode the review criteria in the order the Release
Team weighs them: where we are in the cycle, how far the change reaches, how
much depends on it, and whether anyone has checked it works.
"""

from __future__ import annotations

from ffe.models import (
    Decision,
    FfeKind,
    FlavourImpact,
    ReleaseType,
    RiskAssessment,
    RiskBand,
    RiskComponent,
)
from ffe.risk.signals import Signals

RELEASE_POSTURE_MAX = 20
SEEDED_REACH_MAX = 25
DEPENDENCY_REACH_MAX = 25
ARCHIVE_STANDING_MAX = 10
CHANGE_SHAPE_MAX = 5
UNVALIDATED_MAX = 15

# Band thresholds. HIGH begins at 50 because that is roughly where a request
# stops being one person's judgement call and starts wanting a second opinion.
BAND_THRESHOLDS: tuple[tuple[int, RiskBand], ...] = (
    (75, RiskBand.SEVERE),
    (50, RiskBand.HIGH),
    (25, RiskBand.MODERATE),
    (0, RiskBand.LOW),
)

# How far into the freeze each phase is, as a fraction of the maximum timing
# score. Feature Freeze itself is not alarming; Final Freeze is.
_PHASE_WEIGHT: dict[str, float] = {
    "PRE_FEATURE_FREEZE": 0.0,
    "FEATURE_FREEZE": 0.35,
    "UI_FREEZE": 0.55,
    "BETA_FREEZE": 0.70,
    "BETA": 0.75,
    "KERNEL_FREEZE": 0.85,
    "FINAL_FREEZE": 1.0,
    "FINAL_RELEASE": 1.0,
    "UNKNOWN": 0.6,
}


def _band(score: int) -> RiskBand:
    for threshold, band in BAND_THRESHOLDS:
        if score >= threshold:
            return band
    return RiskBand.LOW


def _release_posture(signals: Signals) -> RiskComponent:
    """How late we are, and how much that costs on this kind of release.

    An LTS is supported for a decade, so the same change carries more
    consequence there than on an interim release that is superseded in six
    months. This is the LTS/interim distinction having real weight rather than
    appearing as a label.
    """
    weight = _PHASE_WEIGHT.get(signals.phase, 0.6)
    points = weight * RELEASE_POSTURE_MAX

    if signals.release_type is ReleaseType.LTS:
        # Scaled rather than added, so an LTS request early in the window is
        # still treated as early.
        points *= 1.25
    elif signals.release_type is None:
        points = max(points, RELEASE_POSTURE_MAX * 0.6)

    phase_text = signals.phase.replace("_", " ").lower()
    if signals.release_type is None:
        rationale = (
            f"target release unknown; assuming an ordinary position in the freeze ({phase_text})"
        )
    else:
        kind = "LTS" if signals.release_type is ReleaseType.LTS else "interim"
        days = (
            f", {signals.days_to_release} days to release"
            if signals.days_to_release is not None
            else ""
        )
        elapsed = (
            f", {signals.freeze_elapsed_pct}% through the freeze window"
            if signals.freeze_elapsed_pct is not None
            else ""
        )
        rationale = f"{kind} release, past {phase_text}{days}{elapsed}"

    return RiskComponent(
        name="release_posture",
        points=min(RELEASE_POSTURE_MAX, round(points)),
        max_points=RELEASE_POSTURE_MAX,
        rationale=rationale,
        evidence_refs=("/evidence/release_context",),
    )


def _seeded_reach(signals: Signals) -> RiskComponent:
    """Whether this lands on an image, and whose.

    A flavour-only change is that flavour's call to make, so it scores low --
    not because the change is technically safer, but because the ownership sits
    with the team that carries the consequence.
    """
    refs = ("/evidence/packages",)

    if not signals.seeds_known:
        return RiskComponent(
            name="seeded_reach",
            points=round(SEEDED_REACH_MAX * 0.6),
            max_points=SEEDED_REACH_MAX,
            rationale="could not establish which images ship this package; scored cautiously rather than as unseeded",
            evidence_refs=refs,
        )

    if signals.flavour_impact is FlavourImpact.CORE:
        return RiskComponent(
            name="seeded_reach",
            points=SEEDED_REACH_MAX,
            max_points=SEEDED_REACH_MAX,
            rationale="seeded on a core Ubuntu image, so a regression reaches the default install",
            evidence_refs=refs,
        )
    if signals.flavour_impact is FlavourImpact.MULTI_FLAVOUR:
        flavours = ", ".join(signals.seeded_flavours)
        return RiskComponent(
            name="seeded_reach",
            points=round(SEEDED_REACH_MAX * 0.6),
            max_points=SEEDED_REACH_MAX,
            rationale=f"seeded on {len(signals.seeded_flavours)} flavours ({flavours}), so no single flavour owns the outcome",
            evidence_refs=refs,
        )
    if signals.flavour_impact is FlavourImpact.SINGLE_FLAVOUR:
        return RiskComponent(
            name="seeded_reach",
            points=round(SEEDED_REACH_MAX * 0.25),
            max_points=SEEDED_REACH_MAX,
            rationale=f"seeded only on {signals.seeded_flavours[0]}, whose team owns the image and the consequence",
            evidence_refs=refs,
        )
    return RiskComponent(
        name="seeded_reach",
        points=0,
        max_points=SEEDED_REACH_MAX,
        rationale="not seeded on any Ubuntu image",
        evidence_refs=refs,
    )


def _dependency_reach(signals: Signals) -> RiskComponent:
    """How much of the archive is downstream of this package."""
    refs = ("/evidence/packages",)

    if not signals.rdeps_known:
        return RiskComponent(
            name="dependency_reach",
            points=round(DEPENDENCY_REACH_MAX * 0.6),
            max_points=DEPENDENCY_REACH_MAX,
            rationale="reverse dependencies could not be determined; scored cautiously rather than as a leaf",
            evidence_refs=refs,
        )

    total = signals.max_rdeps or 0
    build = signals.max_build_rdeps or 0

    # Bands rather than a curve, so the rationale can state the band and a
    # reviewer can tell at a glance which one applies.
    if total >= 500:
        share = 1.0
    elif total >= 100:
        share = 0.8
    elif total >= 20:
        share = 0.5
    elif total >= 5:
        share = 0.3
    elif total >= 1:
        share = 0.15
    else:
        share = 0.0

    # Dependents that are themselves shipped on an image matter more than
    # dependents nobody installs.
    if signals.seeded_rdeps:
        share = min(1.0, share + 0.15)

    detail = f"{total} reverse dependencies, {build} reverse build-dependencies"
    if signals.seeded_rdeps:
        shown = ", ".join(signals.seeded_rdeps[:3])
        detail += f"; {len(signals.seeded_rdeps)} of them are themselves seeded ({shown})"
    if total == 0 and build == 0:
        detail = "nothing in the archive depends on this package"

    return RiskComponent(
        name="dependency_reach",
        points=round(share * DEPENDENCY_REACH_MAX),
        max_points=DEPENDENCY_REACH_MAX,
        rationale=detail,
        evidence_refs=refs,
    )


def _archive_standing(signals: Signals) -> RiskComponent:
    """Whether this is Canonical-supported material."""
    refs = ("/evidence/packages",)
    if signals.in_main is None:
        return RiskComponent(
            name="archive_standing",
            points=round(ARCHIVE_STANDING_MAX * 0.5),
            max_points=ARCHIVE_STANDING_MAX,
            rationale="could not determine the archive component",
            evidence_refs=refs,
        )
    if signals.in_main:
        return RiskComponent(
            name="archive_standing",
            points=ARCHIVE_STANDING_MAX,
            max_points=ARCHIVE_STANDING_MAX,
            rationale="in main or restricted, so it carries a Canonical support commitment",
            evidence_refs=refs,
        )
    return RiskComponent(
        name="archive_standing",
        points=round(ARCHIVE_STANDING_MAX * 0.3),
        max_points=ARCHIVE_STANDING_MAX,
        rationale="in universe or multiverse, which is community-supported",
        evidence_refs=refs,
    )


def _change_shape(signals: Signals) -> RiskComponent:
    """Whether the request is inherently wider than one package."""
    refs = ("/evidence/subject",)
    if signals.ffe_kind is FfeKind.TRANSITION:
        return RiskComponent(
            name="change_shape",
            points=CHANGE_SHAPE_MAX,
            max_points=CHANGE_SHAPE_MAX,
            rationale="an archive transition, which by nature touches many packages at once",
            evidence_refs=refs,
        )
    if signals.ffe_kind is FfeKind.SEED_CHANGE:
        return RiskComponent(
            name="change_shape",
            points=round(CHANGE_SHAPE_MAX * 0.8),
            max_points=CHANGE_SHAPE_MAX,
            rationale="changes what lands on an image, so it affects users who never install the package directly",
            evidence_refs=refs,
        )
    if signals.ffe_kind is FfeKind.NEW_PACKAGE:
        return RiskComponent(
            name="change_shape",
            points=0,
            max_points=CHANGE_SHAPE_MAX,
            rationale="a new package, which nothing depends on yet",
            evidence_refs=refs,
        )
    return RiskComponent(
        name="change_shape",
        points=round(CHANGE_SHAPE_MAX * 0.4),
        max_points=CHANGE_SHAPE_MAX,
        rationale=f"a {signals.ffe_kind.value.replace('-', ' ')} to an existing package",
        evidence_refs=refs,
    )


def _unvalidated(signals: Signals) -> RiskComponent:
    """Whether anyone has demonstrated the change works.

    Scored on what can be checked. Prose claims contribute nothing here -- see
    ffe.evidence.testing for why.
    """
    refs = ("/evidence/testing",)
    present = [
        label
        for label, ok in (
            ("a PPA", signals.has_ppa),
            ("a build log", signals.has_build),
            ("test results", signals.has_autopkgtest),
            ("pasted test output", signals.has_test_output),
        )
        if ok
    ]

    if not present:
        claims = signals.unverified_claim_count
        rationale = "no PPA, build log or test results are linked from the bug"
        if claims:
            rationale += (
                f"; {claims} claim(s) of testing are made in prose but nothing is linked to check"
            )
        return RiskComponent(
            name="unvalidated_change",
            points=UNVALIDATED_MAX,
            max_points=UNVALIDATED_MAX,
            rationale=rationale,
            evidence_refs=refs,
        )

    remaining = max(0, UNVALIDATED_MAX - 5 * len(present))
    return RiskComponent(
        name="unvalidated_change",
        points=remaining,
        max_points=UNVALIDATED_MAX,
        rationale=f"the bug links {', '.join(present)}",
        evidence_refs=refs,
    )


def score(signals: Signals) -> tuple[int, RiskBand, tuple[RiskComponent, ...]]:
    """Score the request, returning the total, band and every contribution."""
    components = (
        _release_posture(signals),
        _seeded_reach(signals),
        _dependency_reach(signals),
        _archive_standing(signals),
        _change_shape(signals),
        _unvalidated(signals),
    )
    total = min(100, sum(c.points for c in components))
    return total, _band(total), components


def assess(signals: Signals) -> RiskAssessment:
    """Score and band, before gates are applied."""
    total, band, components = score(signals)
    return RiskAssessment(score=total, band=band, components=components)


def permissiveness(decision: Decision) -> int:
    """Order decisions from most to least permissive, for floor comparisons."""
    return {Decision.APPROVE: 0, Decision.NEEDS_INFORMATION: 1, Decision.REJECT: 2}[decision]
