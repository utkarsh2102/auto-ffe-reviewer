"""Tests for deterministic scoring and gates.

These encode the review criteria, so they read as statements about how the
Release Team weighs a request. Nothing here involves a model: the whole layer
must produce a usable assessment on its own.
"""

from __future__ import annotations

import pytest

from ffe.models import (
    Confidence,
    Decision,
    FfeKind,
    FlavourImpact,
    ReleaseType,
    RiskBand,
)
from ffe.risk.gates import evaluate
from ffe.risk.score import assess, score
from ffe.risk.signals import Signals


def _signals(**overrides: object) -> Signals:
    """A plausible middle-of-the-road request, adjusted per test."""
    base = {
        "release_type": ReleaseType.INTERIM,
        "phase": "FEATURE_FREEZE",
        "days_bucket": "56-29",
        "days_to_release": 40,
        "freeze_elapsed_pct": 30,
        "is_core": False,
        "flavour_impact": FlavourImpact.UNSEEDED,
        "seeds_known": True,
        "rdeps_known": True,
        "max_rdeps": 0,
        "max_build_rdeps": 0,
        "in_main": False,
        "in_archive": True,
        "packages": ("example",),
        "packages_known": True,
        "ffe_kind": FfeKind.NEW_UPSTREAM,
        "testing_corroborated": True,
        "has_ppa": True,
        "has_autopkgtest": True,
    }
    base.update(overrides)
    return Signals(**base)  # type: ignore[arg-type]


def _full(**overrides: object):  # type: ignore[no-untyped-def]
    signals = _signals(**overrides)
    return signals, evaluate(signals, assess(signals))


def _component(assessment, name: str):  # type: ignore[no-untyped-def]
    return next(c for c in assessment.components if c.name == name)


def _gate(assessment, gate_id: str):  # type: ignore[no-untyped-def]
    return next(g for g in assessment.gates if g.id == gate_id)


# --------------------------------------------------------------------------- #
# 4.1 LTS vs interim
# --------------------------------------------------------------------------- #


def test_lts_scores_higher_than_interim_for_the_same_change() -> None:
    """An LTS is supported for a decade; the same change costs more there."""
    _, interim = _full(release_type=ReleaseType.INTERIM)
    _, lts = _full(release_type=ReleaseType.LTS)

    assert _component(lts, "release_posture").points > _component(interim, "release_posture").points


def test_lts_early_in_the_window_is_still_treated_as_early() -> None:
    """The LTS weighting scales the timing score; it does not swamp it."""
    _, early = _full(release_type=ReleaseType.LTS, phase="FEATURE_FREEZE")
    _, late = _full(release_type=ReleaseType.LTS, phase="FINAL_FREEZE")
    assert _component(early, "release_posture").points < _component(late, "release_posture").points


def test_unknown_release_type_is_not_treated_as_safe() -> None:
    _, unknown = _full(release_type=None)
    assert _component(unknown, "release_posture").points > 0
    assert "unknown" in _component(unknown, "release_posture").rationale


# --------------------------------------------------------------------------- #
# 4.2 Position in the cycle
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("FEATURE_FREEZE", "UI_FREEZE"),
        ("UI_FREEZE", "BETA_FREEZE"),
        ("BETA", "KERNEL_FREEZE"),
        ("KERNEL_FREEZE", "FINAL_FREEZE"),
    ],
)
def test_later_in_the_freeze_scores_higher(earlier: str, later: str) -> None:
    _, a = _full(phase=earlier)
    _, b = _full(phase=later)
    assert _component(b, "release_posture").points > _component(a, "release_posture").points


def test_before_feature_freeze_no_exception_is_needed() -> None:
    _, assessment = _full(phase="PRE_FEATURE_FREEZE", before_feature_freeze=True)
    gate = _gate(assessment, "before_feature_freeze")
    assert gate.triggered
    assert "may not be needed" in gate.rationale


def test_after_final_freeze_floors_at_needs_information() -> None:
    _, assessment = _full(phase="FINAL_FREEZE", after_final_freeze=True)
    assert _gate(assessment, "after_final_freeze").triggered
    assert assessment.decision_floor is Decision.NEEDS_INFORMATION


# --------------------------------------------------------------------------- #
# 4.3 / 4.4 Criticality and seeding
# --------------------------------------------------------------------------- #


def test_core_seeding_scores_maximum_reach() -> None:
    _, assessment = _full(is_core=True, flavour_impact=FlavourImpact.CORE)
    component = _component(assessment, "seeded_reach")
    assert component.points == component.max_points
    assert "default install" in component.rationale


def test_unseeded_scores_no_reach() -> None:
    _, assessment = _full(flavour_impact=FlavourImpact.UNSEEDED)
    assert _component(assessment, "seeded_reach").points == 0


def test_unknown_seeding_is_scored_cautiously_not_as_unseeded() -> None:
    """A seed index we could not read must not make a package look like a leaf."""
    _, unknown = _full(seeds_known=False)
    _, unseeded = _full(seeds_known=True, flavour_impact=FlavourImpact.UNSEEDED)

    assert _component(unknown, "seeded_reach").points > _component(unseeded, "seeded_reach").points
    assert "cautiously" in _component(unknown, "seeded_reach").rationale


def test_main_carries_more_weight_than_universe() -> None:
    _, main = _full(in_main=True)
    _, universe = _full(in_main=False)
    assert (
        _component(main, "archive_standing").points
        > _component(universe, "archive_standing").points
    )


# --------------------------------------------------------------------------- #
# 4.5 / 4.6 Blast radius
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rdeps", "expected_minimum"),
    [(0, 0), (3, 1), (50, 10), (150, 18), (711, 24)],
)
def test_dependency_reach_scales_with_rdeps(rdeps: int, expected_minimum: int) -> None:
    _, assessment = _full(max_rdeps=rdeps, max_build_rdeps=rdeps)
    assert _component(assessment, "dependency_reach").points >= expected_minimum


def test_a_leaf_package_says_so_plainly() -> None:
    _, assessment = _full(max_rdeps=0, max_build_rdeps=0)
    assert (
        "nothing in the archive depends on" in _component(assessment, "dependency_reach").rationale
    )


def test_seeded_dependents_weigh_more_than_bare_counts() -> None:
    """Two dependents on the desktop matter more than a hundred nobody ships."""
    _, plain = _full(max_rdeps=20)
    _, seeded = _full(max_rdeps=20, seeded_rdeps=("kubuntu-desktop", "kde-standard"))

    assert (
        _component(seeded, "dependency_reach").points > _component(plain, "dependency_reach").points
    )
    assert "themselves seeded" in _component(seeded, "dependency_reach").rationale


def test_unknown_rdeps_are_scored_cautiously() -> None:
    _, unknown = _full(rdeps_known=False)
    _, leaf = _full(rdeps_known=True, max_rdeps=0)
    assert (
        _component(unknown, "dependency_reach").points > _component(leaf, "dependency_reach").points
    )


# --------------------------------------------------------------------------- #
# 4.8 Testing evidence -- the most consequential gate
# --------------------------------------------------------------------------- #


def test_no_testing_evidence_floors_at_needs_information() -> None:
    _, assessment = _full(
        testing_corroborated=False,
        has_ppa=False,
        has_autopkgtest=False,
        is_core=True,
        flavour_impact=FlavourImpact.CORE,
    )
    assert _gate(assessment, "no_testing_evidence").triggered
    assert assessment.decision_floor is Decision.NEEDS_INFORMATION


def test_prose_claims_do_not_clear_the_gate() -> None:
    """Saying "I tested it" must not satisfy the testing requirement."""
    _, assessment = _full(
        testing_corroborated=False,
        has_ppa=False,
        has_autopkgtest=False,
        unverified_claim_count=3,
        is_core=True,
    )
    gate = _gate(assessment, "no_testing_evidence")
    assert gate.triggered
    assert "claim(s) of testing are made in prose" in gate.rationale


def test_linked_evidence_clears_the_gate() -> None:
    _, assessment = _full(testing_corroborated=True, has_ppa=True, has_autopkgtest=True)
    assert not _gate(assessment, "no_testing_evidence").triggered
    assert assessment.decision_floor is None


@pytest.mark.parametrize("kind", [FfeKind.SYNC, FfeKind.MERGE])
def test_syncs_and_merges_are_exempt_from_the_testing_gate(kind: FfeKind) -> None:
    """Debian has already built and tested these, and the archive rebuilds them."""
    _, assessment = _full(
        ffe_kind=kind, testing_corroborated=False, has_ppa=False, has_autopkgtest=False
    )
    gate = _gate(assessment, "no_testing_evidence")
    assert not gate.triggered
    assert "Debian has already built" in gate.rationale


def test_a_change_reaching_nothing_is_exempt() -> None:
    _, assessment = _full(
        testing_corroborated=False,
        has_ppa=False,
        has_autopkgtest=False,
        max_rdeps=0,
        flavour_impact=FlavourImpact.UNSEEDED,
        is_core=False,
    )
    assert not _gate(assessment, "no_testing_evidence").triggered


def test_more_linked_evidence_lowers_the_risk() -> None:
    _, one = _full(has_ppa=True, has_build=False, has_autopkgtest=False, has_test_output=False)
    _, several = _full(has_ppa=True, has_build=True, has_autopkgtest=True, has_test_output=True)
    assert (
        _component(several, "unvalidated_change").points
        < _component(one, "unvalidated_change").points
    )


# --------------------------------------------------------------------------- #
# Section 5: flavours
# --------------------------------------------------------------------------- #


def test_single_flavour_scores_low_because_that_team_owns_it() -> None:
    """Not because it is technically safer, but because ownership is clear."""
    _, single = _full(
        flavour_impact=FlavourImpact.SINGLE_FLAVOUR, seeded_flavours=("ubuntustudio",)
    )
    _, multi = _full(
        flavour_impact=FlavourImpact.MULTI_FLAVOUR, seeded_flavours=("kubuntu", "ubuntustudio")
    )

    assert _component(single, "seeded_reach").points < _component(multi, "seeded_reach").points
    assert "owns the image and the consequence" in _component(single, "seeded_reach").rationale


def test_outstanding_cross_flavour_acknowledgement_is_flagged_not_blocked() -> None:
    """The other team may well be content; nobody should have to discover the gap."""
    _, assessment = _full(
        flavour_impact=FlavourImpact.MULTI_FLAVOUR,
        seeded_flavours=("kubuntu", "ubuntustudio"),
        ack_required_from=("kubuntu",),
    )
    gate = _gate(assessment, "flavour_ack_outstanding")
    assert gate.triggered
    assert "kubuntu" in gate.rationale
    # Flagged, not floored: this is information, not a blocker.
    assert assessment.decision_floor is None


def test_acknowledged_multi_flavour_change_is_not_flagged() -> None:
    _, assessment = _full(
        flavour_impact=FlavourImpact.MULTI_FLAVOUR,
        seeded_flavours=("kubuntu", "ubuntustudio"),
        ack_required_from=(),
        acked_by=("kubuntu", "ubuntustudio"),
    )
    assert not _gate(assessment, "flavour_ack_outstanding").triggered


# --------------------------------------------------------------------------- #
# Missing information and confidence
# --------------------------------------------------------------------------- #


def test_no_identified_package_floors_at_needs_information() -> None:
    _, assessment = _full(packages=(), packages_known=False)
    assert _gate(assessment, "no_package_identified").triggered
    assert assessment.decision_floor is Decision.NEEDS_INFORMATION


def test_confidence_ceiling_drops_with_missing_sources() -> None:
    _, complete = _full()
    _, one_missing = _full(unavailable_sources=("seeds",))
    _, two_missing = _full(unavailable_sources=("seeds", "rdepends"))

    assert complete.confidence_ceiling is Confidence.HIGH
    assert one_missing.confidence_ceiling is Confidence.MEDIUM
    assert two_missing.confidence_ceiling is Confidence.LOW


def test_core_change_without_test_evidence_caps_confidence() -> None:
    _, assessment = _full(
        is_core=True, testing_corroborated=False, has_ppa=False, has_autopkgtest=False
    )
    assert assessment.confidence_ceiling is Confidence.MEDIUM


def test_suspicious_text_is_flagged_without_changing_the_floor() -> None:
    """Reported for a human to judge; it never moves the recommendation."""
    _, clean = _full()
    _, flagged = _full(injection_signal_count=2)

    assert _gate(flagged, "suspicious_bug_text").triggered
    assert flagged.decision_floor == clean.decision_floor
    assert flagged.score == clean.score


# --------------------------------------------------------------------------- #
# Overall behaviour
# --------------------------------------------------------------------------- #


def test_no_gate_can_produce_an_approval() -> None:
    """By design. That decision always belongs to a person."""
    for kind in FfeKind:
        for core in (True, False):
            _, assessment = _full(
                ffe_kind=kind, is_core=core, testing_corroborated=False, has_ppa=False
            )
            assert assessment.decision_floor is not Decision.APPROVE


def test_score_is_bounded_and_banded() -> None:
    _, worst = _full(
        release_type=ReleaseType.LTS,
        phase="FINAL_FREEZE",
        is_core=True,
        flavour_impact=FlavourImpact.CORE,
        max_rdeps=5000,
        max_build_rdeps=5000,
        seeded_rdeps=("bash",),
        in_main=True,
        ffe_kind=FfeKind.TRANSITION,
        testing_corroborated=False,
        has_ppa=False,
        has_autopkgtest=False,
    )
    assert worst.score == 100
    assert worst.band is RiskBand.SEVERE

    _, best = _full(
        phase="FEATURE_FREEZE",
        flavour_impact=FlavourImpact.UNSEEDED,
        max_rdeps=0,
        in_main=False,
        ffe_kind=FfeKind.NEW_PACKAGE,
        has_ppa=True,
        has_build=True,
        has_autopkgtest=True,
        has_test_output=True,
    )
    assert best.band is RiskBand.LOW


def test_every_component_explains_itself() -> None:
    """A band is useless alone; a reviewer must be able to disagree specifically."""
    total, band, components = score(_signals())
    assert 0 <= total <= 100
    assert band in RiskBand
    for component in components:
        assert component.rationale
        assert component.evidence_refs
        assert 0 <= component.points <= component.max_points


def test_components_sum_to_the_score() -> None:
    total, _, components = score(_signals(is_core=True, max_rdeps=100))
    assert total == min(100, sum(c.points for c in components))
