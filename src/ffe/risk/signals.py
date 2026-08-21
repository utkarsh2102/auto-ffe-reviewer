"""Reduce an evidence bundle to the handful of things that drive the decision.

Signals are deliberately coarse. The scoring below them should turn on "is this
package on a core image", not on which sixteen images exactly; the detail stays
in the bundle for the reviewer to read.

Optional booleans are three-valued throughout, and that is the point. `is_core
= None` means the seed index could not be trusted, which is a different
situation from `is_core = False`, and the two must not be allowed to collapse
into one another -- the first should make us less sure, the second more.
"""

from __future__ import annotations

from dataclasses import dataclass

from ffe.models import (
    EvidenceBundle,
    EvidenceState,
    FactStatus,
    FfeKind,
    FlavourImpact,
    ReleaseType,
)

# Phases at or after which the archive is effectively closed to new features.
LATE_PHASES = ("BETA_FREEZE", "BETA", "KERNEL_FREEZE", "FINAL_FREEZE", "FINAL_RELEASE")

# Kinds of change whose shape is inherently wider than one package.
ARCHIVE_WIDE_KINDS = (FfeKind.TRANSITION, FfeKind.SEED_CHANGE)


@dataclass(frozen=True, slots=True)
class Signals:
    """What the deterministic layer reasons about."""

    # Release posture
    release_type: ReleaseType | None = None
    phase: str = "UNKNOWN"
    days_bucket: str = "unknown"
    days_to_release: int | None = None
    freeze_elapsed_pct: int | None = None
    is_late: bool = False
    before_feature_freeze: bool = False
    after_final_freeze: bool = False

    # Reach
    is_core: bool | None = None
    flavour_impact: FlavourImpact = FlavourImpact.UNSEEDED
    seeded_flavours: tuple[str, ...] = ()
    seeds_known: bool = False

    # Dependencies
    max_rdeps: int | None = None
    max_rdeps_bucket: str = "unknown"
    max_build_rdeps: int | None = None
    seeded_rdeps: tuple[str, ...] = ()
    rdeps_known: bool = False

    # Archive standing
    in_main: bool | None = None
    in_archive: bool | None = None
    packages: tuple[str, ...] = ()
    packages_known: bool = False

    # The request itself
    ffe_kind: FfeKind = FfeKind.UNKNOWN
    archive_wide: bool = False

    # Validation
    testing_corroborated: bool = False
    has_ppa: bool = False
    has_build: bool = False
    has_autopkgtest: bool = False
    has_test_output: bool = False
    unverified_claim_count: int = 0

    # Flavour ownership
    ack_required_from: tuple[str, ...] = ()
    acked_by: tuple[str, ...] = ()
    requesting_flavour: str | None = None

    # Meta
    injection_signal_count: int = 0
    unavailable_sources: tuple[str, ...] = ()


def _checkable(state: EvidenceState) -> bool:
    """Whether a piece of evidence is something a human could go and look at."""
    return state in (EvidenceState.FOUND_VERIFIED, EvidenceState.FOUND_UNVERIFIED)


def derive(bundle: EvidenceBundle) -> Signals:
    """Reduce a bundle to its decision-bearing signals."""
    context = bundle.release_context.value if bundle.release_context.ok else None

    phase = context.phase if context else "UNKNOWN"
    after_final_freeze = phase in ("FINAL_FREEZE", "FINAL_RELEASE")

    # Reach, taken across every package in the request: an FFe touching one
    # core package and one leaf is a core change.
    is_core: bool | None = None
    seeds_known = False
    for package in bundle.packages:
        if package.seeds.status is FactStatus.OK and package.seeds.value is not None:
            seeds_known = True
            is_core = bool(is_core) or package.seeds.value.is_core

    # Dependencies: the widest reach across the packages, since that is what
    # bounds the damage.
    max_rdeps: int | None = None
    max_build: int | None = None
    bucket = "unknown"
    seeded_rdeps: set[str] = set()
    rdeps_known = False
    for package in bundle.packages:
        if package.rdepends.ok and package.rdepends.value is not None:
            rdeps_known = True
            value = package.rdepends.value
            if max_rdeps is None or value.total > max_rdeps:
                max_rdeps, bucket = value.total, value.bucket
            seeded_rdeps.update(value.seeded_rdeps)
        if package.build_rdepends.ok and package.build_rdepends.value is not None:
            value = package.build_rdepends.value
            max_build = value.total if max_build is None else max(max_build, value.total)

    in_main: bool | None = None
    in_archive: bool | None = None
    for package in bundle.packages:
        if package.archive.ok and package.archive.value is not None:
            in_main = bool(in_main) or package.archive.value.in_main
            in_archive = bool(in_archive) or package.archive.value.in_archive

    testing = bundle.testing
    return Signals(
        release_type=context.release_type if context else None,
        phase=phase,
        days_bucket=context.days_bucket if context else "unknown",
        days_to_release=context.days_to_release if context else None,
        freeze_elapsed_pct=context.freeze_window_elapsed_pct if context else None,
        is_late=phase in LATE_PHASES,
        before_feature_freeze=phase == "PRE_FEATURE_FREEZE",
        after_final_freeze=after_final_freeze,
        is_core=is_core,
        flavour_impact=bundle.flavours.impact,
        seeded_flavours=bundle.flavours.affected_flavours,
        seeds_known=seeds_known,
        max_rdeps=max_rdeps,
        max_rdeps_bucket=bucket,
        max_build_rdeps=max_build,
        seeded_rdeps=tuple(sorted(seeded_rdeps)),
        rdeps_known=rdeps_known,
        in_main=in_main,
        in_archive=in_archive,
        packages=bundle.subject.packages,
        packages_known=bool(bundle.subject.packages),
        ffe_kind=bundle.subject.ffe_kind,
        archive_wide=bundle.subject.ffe_kind in ARCHIVE_WIDE_KINDS,
        testing_corroborated=testing.corroborated,
        has_ppa=_checkable(testing.ppa),
        has_build=_checkable(testing.build),
        has_autopkgtest=_checkable(testing.autopkgtest),
        has_test_output=_checkable(testing.test_output),
        unverified_claim_count=len(testing.unverified_claims),
        ack_required_from=bundle.flavours.ack_required_from,
        acked_by=tuple(sorted({a.flavour for a in bundle.flavours.acks})),
        requesting_flavour=bundle.flavours.requesting_flavour,
        injection_signal_count=len(bundle.injection_signals),
        unavailable_sources=tuple(sorted({u.source_id for u in bundle.unavailable})),
    )
