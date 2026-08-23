"""The representative FFe cases, run end to end.

Each scenario in scenarios.toml is driven through the whole deterministic
pipeline -- evidence gathering, signals, scoring, gates -- against real
captured upstream data. Only the bug text is synthetic, because a bug that
does not exist cannot be captured.

These are the tests that would catch a change to the review criteria having an
effect nobody intended, since they assert on outcomes a Release Team member
would recognise rather than on internal structure.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from conftest import RAW, Route
from ffe.evidence.builder import build
from ffe.evidence.fingerprint import fingerprint
from ffe.models import BugComment, BugFacts, BugTask
from ffe.risk.gates import evaluate as apply_gates
from ffe.risk.score import assess as score_risk
from ffe.risk.signals import derive as derive_signals
from ffe.sources.launchpad import client
from ffe.sources.seeds import load_flavours
from ffe.util.clock import frozen_at
from ffe.util.hashing import sha256_hex

SCENARIOS = tomllib.loads(
    (Path(__file__).parent / "fixtures" / "scenarios" / "scenarios.toml").read_text(
        encoding="utf-8"
    )
)
FLAVOURS = load_flavours()
SERIES = ("stonking", "resolute", "questing", "noble")


def _routes(package: str) -> list[Route]:
    """Route every lookup to the captured data for this scenario's package."""
    return [
        Route(
            r"documentation\.ubuntu\.com/release-notes/26\.10", RAW / "schedule/stonking-26.10.html"
        ),
        Route(
            r"documentation\.ubuntu\.com/release-notes/26\.04", RAW / "schedule/resolute-26.04.html"
        ),
        Route(r"ubuntu-seeded-packages/seeded\.json\.gz", RAW / "seeds/seeded-healthy.json.gz"),
        Route(r"/rdepends/v1/\w+/any/src:", RAW / f"rdepends/src-{package}-any.json"),
        Route(r"/rdepends/v1/\w+/source/src:", RAW / f"rdepends/src-{package}-source.json"),
        Route(r"getPublishedBinaries", RAW / f"launchpad/published-binaries-{package}.json"),
        Route(r"getPublishedSources", RAW / f"launchpad/published-sources-{package}.json"),
        Route(r"~ubuntu-release/participants", RAW / "launchpad/ubuntu-release-participants.json"),
        Route(r"~kubuntu-dev/participants", RAW / "launchpad/participants-kubuntu-dev.json"),
        Route(
            r"~ubuntustudio-dev/participants", RAW / "launchpad/participants-ubuntustudio-dev.json"
        ),
    ]


def _bug(scenario: dict[str, Any]) -> BugFacts:
    package = scenario["package"]
    description = scenario["description"]
    comments = tuple(
        BugComment(
            index=index + 1,
            author=str(entry["author"]),
            date_created="2026-09-01T12:00:00Z",
            content=str(entry["content"]),
            content_sha=sha256_hex(str(entry["content"])),
        )
        for index, entry in enumerate(scenario.get("comments", []))
    )
    return BugFacts(
        id=9000000,
        url="https://bugs.launchpad.net/bugs/9000000",
        title=scenario["title"],
        description=description,
        description_sha=sha256_hex(description),
        reporter="some-developer",
        tasks=(BugTask(f"{package} (Ubuntu)", "New", "Undecided"),),
        comments=comments,
        release_team_subscribed=True,
    )


def _run(make_ctx: Any, name: str):  # type: ignore[no-untyped-def]
    scenario = SCENARIOS[name]
    ctx = make_ctx(_routes(scenario["package"]))
    with frozen_at(scenario["now"]):
        result = build(
            ctx,
            _bug(scenario),
            launchpad=client(ctx),
            flavours=FLAVOURS,
            known_series=SERIES,
            default_series=scenario.get("series", "stonking"),
        )
        signals = derive_signals(result.bundle)
        risk = apply_gates(signals, score_risk(signals))
    return scenario, result.bundle, signals, risk


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario(make_ctx, name: str) -> None:  # type: ignore[no-untyped-def]
    """Drive one representative case through the deterministic pipeline."""
    scenario, _bundle, signals, risk = _run(make_ctx, name)
    expect = scenario.get("expect", {})
    covers = scenario["covers"]

    if "risk_band" in expect:
        assert risk.band.value == expect["risk_band"], f"{covers}: risk band"

    if "decision_floor" in expect:
        floor = risk.decision_floor.value if risk.decision_floor else ""
        assert floor == expect["decision_floor"], f"{covers}: decision floor"

    if "gates" in expect:
        triggered = sorted(g.id for g in risk.gates if g.triggered)
        assert triggered == sorted(expect["gates"]), f"{covers}: gates triggered"

    if "release_type" in expect:
        assert signals.release_type is not None
        assert signals.release_type.value == expect["release_type"], f"{covers}: release type"

    if "phase" in expect:
        assert signals.phase == expect["phase"], f"{covers}: phase"

    if "flavour_impact" in expect:
        assert signals.flavour_impact.value == expect["flavour_impact"], f"{covers}: flavour impact"

    if "is_core" in expect:
        assert bool(signals.is_core) == expect["is_core"], f"{covers}: core"

    if "affected_flavours" in expect:
        assert sorted(signals.seeded_flavours) == sorted(expect["affected_flavours"]), (
            f"{covers}: affected flavours"
        )

    if "ack_required_from" in expect:
        assert sorted(signals.ack_required_from) == sorted(expect["ack_required_from"]), (
            f"{covers}: outstanding acknowledgements"
        )

    if "testing_corroborated" in expect:
        assert signals.testing_corroborated == expect["testing_corroborated"], (
            f"{covers}: testing corroborated"
        )

    if "min_rdepends" in expect:
        assert (signals.max_rdeps or 0) >= expect["min_rdepends"], f"{covers}: reverse dependencies"

    if "max_rdepends" in expect:
        assert (signals.max_rdeps or 0) <= expect["max_rdepends"], f"{covers}: reverse dependencies"

    if "min_unverified_claims" in expect:
        assert signals.unverified_claim_count >= expect["min_unverified_claims"], (
            f"{covers}: unverified claims"
        )

    if "min_injection_signals" in expect:
        assert signals.injection_signal_count >= expect["min_injection_signals"], (
            f"{covers}: injection signals"
        )


def test_every_scenario_is_documented() -> None:
    """Each one states which of the representative cases it covers."""
    assert len(SCENARIOS) == 18
    for name, scenario in SCENARIOS.items():
        assert scenario.get("covers"), f"{name} does not say what it covers"
        assert scenario.get("expect"), f"{name} asserts nothing"


# --------------------------------------------------------------------------- #
# Comparisons between scenarios, which is where the criteria really show
# --------------------------------------------------------------------------- #


def test_lts_is_judged_more_conservatively_than_interim(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """The same change, the same package, a different kind of release.

    LTS/interim is meant to be a real influence rather than a label, so the
    timing component must actually differ.
    """
    _, _, lts_signals, lts_risk = _run(make_ctx, "lts_target")
    _, _, interim_signals, interim_risk = _run(make_ctx, "interim_target_same_change")

    assert lts_signals.release_type is not None and lts_signals.release_type.value == "LTS"
    assert (
        interim_signals.release_type is not None and interim_signals.release_type.value == "INTERIM"
    )

    def timing(risk: Any) -> int:
        return next(c.points for c in risk.components if c.name == "release_posture")

    assert timing(lts_risk) > timing(interim_risk)


def test_evidence_changes_the_outcome_not_just_the_wording(make_ctx) -> None:
    """The same package and the same date, with and without evidence."""
    _, _, without, without_risk = _run(make_ctx, "missing_ppa_and_build_evidence")
    _, _, with_evidence, with_risk = _run(make_ctx, "strong_testing_evidence")

    assert without.testing_corroborated is False
    assert with_evidence.testing_corroborated is True
    assert without_risk.decision_floor is not None
    assert with_risk.decision_floor is None
    assert with_risk.score < without_risk.score


def test_prose_claims_leave_the_outcome_where_no_claim_would(make_ctx) -> None:
    """Saying "I tested it" must change nothing except what is reported."""
    _, _, silent, silent_risk = _run(make_ctx, "missing_ppa_and_build_evidence")
    _, _, claimed, claimed_risk = _run(make_ctx, "claimed_testing_only")

    assert claimed.unverified_claim_count > silent.unverified_claim_count
    assert claimed.testing_corroborated is silent.testing_corroborated is False
    assert claimed_risk.decision_floor == silent_risk.decision_floor
    assert claimed_risk.score == silent_risk.score


def test_acknowledgement_clears_the_flavour_gate(make_ctx) -> None:
    """Two real Launchpad team members saying yes is what clears it."""
    _, _, without, without_risk = _run(make_ctx, "multi_flavour_unacknowledged")
    _, bundle, acked, acked_risk = _run(make_ctx, "multi_flavour_acknowledged")

    assert without.ack_required_from == ("kubuntu", "ubuntustudio")
    assert acked.ack_required_from == ()
    assert [a.author for a in bundle.flavours.acks]
    # And the acknowledgements are attributed to verified team members.
    assert all(a.author_in_flavour_team for a in bundle.flavours.acks)

    triggered = [g.id for g in without_risk.gates if g.triggered]
    cleared = [g.id for g in acked_risk.gates if g.triggered]
    assert "flavour_ack_outstanding" in triggered
    assert "flavour_ack_outstanding" not in cleared


def test_single_flavour_scores_below_multi_flavour(make_ctx) -> None:
    """Not because it is safer, but because ownership is unambiguous."""
    _, _, _, single = _run(make_ctx, "single_flavour")
    _, _, _, multi = _run(make_ctx, "multi_flavour_unacknowledged")

    def reach(risk: Any) -> int:
        return next(c.points for c in risk.components if c.name == "seeded_reach")

    assert reach(single) < reach(multi)


def test_a_core_package_outranks_a_leaf_at_the_same_moment(make_ctx) -> None:
    _, _, _, core = _run(make_ctx, "seeded_in_core_images")
    _, _, _, leaf = _run(make_ctx, "not_seeded_anywhere")
    assert core.score > leaf.score


def test_injection_is_flagged_without_moving_the_assessment(make_ctx) -> None:
    """The flag is raised; curl is still core with no evidence, and that is the finding."""
    _, bundle, signals, risk = _run(make_ctx, "prompt_injection")

    assert signals.injection_signal_count >= 3
    assert "suspicious_bug_text" in [g.id for g in risk.gates if g.triggered]
    # The scan reports; it does not decide.
    assert risk.decision_floor is not None
    assert bundle.injection_signals[0].excerpt


def test_scenarios_are_reproducible(make_ctx) -> None:
    """The same inputs must give the same fingerprint, or nothing downstream holds."""
    _, first, _, _ = _run(make_ctx, "high_impact_core_package")
    _, second, _, _ = _run(make_ctx, "high_impact_core_package")
    assert fingerprint(first) == fingerprint(second)
