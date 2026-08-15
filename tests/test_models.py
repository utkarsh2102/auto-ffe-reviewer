"""Tests for the core shapes: bucketing, clamping, fact semantics, serialisation."""

from __future__ import annotations

import json

import pytest

from ffe.models import (
    CacheState,
    Confidence,
    EvidenceState,
    Fact,
    FactStatus,
    Provenance,
    RiskBand,
    SeedInfo,
    SourceMethod,
    TestingEvidence,
    TestingItem,
    clamp_confidence,
    days_to_release_bucket,
    rdep_bucket,
    risk_at_least,
    to_jsonable,
)


def _prov() -> Provenance:
    return Provenance(
        source_id="test",
        method=SourceMethod.COMPUTED,
        locator="computed:test",
        checked_at="2026-09-20T00:00:00Z",
    )


# --------------------------------------------------------------------------- #
# Bucketing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0"),
        (1, "1-5"),
        (5, "1-5"),
        (6, "6-20"),
        (20, "6-20"),
        (21, "21-100"),
        (100, "21-100"),
        (101, "101-500"),
        (149, "101-500"),  # curl, as measured live
        (500, "101-500"),
        (501, "500+"),
        (10_000, "500+"),
    ],
)
def test_rdep_bucket_boundaries(count: int, expected: str) -> None:
    assert rdep_bucket(count) == expected


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (-1, "post-release"),
        (0, "3-0"),
        (3, "3-0"),
        (4, "7-4"),
        (7, "7-4"),
        (8, "14-8"),
        (14, "14-8"),
        (15, "28-15"),
        (25, "28-15"),  # stonking on 2026-09-20
        (28, "28-15"),
        (29, "56-29"),
        (56, "56-29"),
        (57, ">56"),
    ],
)
def test_days_bucket_boundaries(days: int, expected: str) -> None:
    assert days_to_release_bucket(days) == expected


def test_days_bucket_is_stable_within_a_band() -> None:
    """The anti-churn property: a day passing must not move the bucket.

    If it did, the change-detection fingerprint would shift daily and we would
    pay for a fresh LLM review roughly every hour for no new information.
    """
    assert days_to_release_bucket(25) == days_to_release_bucket(24)
    assert days_to_release_bucket(28) != days_to_release_bucket(29)


# --------------------------------------------------------------------------- #
# Ordering helpers
# --------------------------------------------------------------------------- #


def test_confidence_clamps_down_never_up() -> None:
    assert clamp_confidence(Confidence.HIGH, Confidence.MEDIUM) is Confidence.MEDIUM
    assert clamp_confidence(Confidence.HIGH, Confidence.LOW) is Confidence.LOW
    # A ceiling never promotes a modest claim.
    assert clamp_confidence(Confidence.LOW, Confidence.HIGH) is Confidence.LOW
    assert clamp_confidence(Confidence.MEDIUM, Confidence.MEDIUM) is Confidence.MEDIUM


def test_risk_floor_takes_the_more_severe() -> None:
    assert risk_at_least(RiskBand.LOW, RiskBand.HIGH) is RiskBand.HIGH
    assert risk_at_least(RiskBand.SEVERE, RiskBand.LOW) is RiskBand.SEVERE


# --------------------------------------------------------------------------- #
# Fact semantics
# --------------------------------------------------------------------------- #


def test_unavailable_is_not_the_same_as_false() -> None:
    """The distinction the whole evidence layer rests on."""
    checked_false: Fact[bool] = Fact(value=False, status=FactStatus.OK, provenance=_prov())
    could_not_check: Fact[bool] = Fact(
        value=None, status=FactStatus.UNAVAILABLE, provenance=_prov(), note="upstream timed out"
    )

    assert checked_false.ok is True
    assert could_not_check.ok is False
    assert could_not_check.unwrap(default=True) is True
    assert checked_false.unwrap(default=True) is False


def test_fact_with_ok_status_but_no_value_is_not_ok() -> None:
    empty: Fact[str] = Fact(value=None, status=FactStatus.OK, provenance=_prov())
    assert empty.ok is False


# --------------------------------------------------------------------------- #
# Testing evidence
# --------------------------------------------------------------------------- #


def test_prose_claims_never_count_as_corroboration() -> None:
    """A developer saying "I tested it" is not evidence that anything was tested."""
    claimed = TestingEvidence(
        items=(
            TestingItem(
                kind="claim",
                state=EvidenceState.CLAIMED_ONLY,
                detail="'I have tested this in a PPA'",
                source="description",
            ),
        ),
        ppa=EvidenceState.CLAIMED_ONLY,
        unverified_claims=("I have tested this in a PPA",),
    )
    assert claimed.corroborated is False


def test_a_verified_ppa_does_corroborate() -> None:
    verified = TestingEvidence(ppa=EvidenceState.FOUND_VERIFIED)
    assert verified.corroborated is True
    # Even an unverified-but-present link is checkable by a human, so it counts.
    assert TestingEvidence(build=EvidenceState.FOUND_UNVERIFIED).corroborated is True


def test_absent_evidence_is_not_corroborated() -> None:
    assert TestingEvidence().corroborated is False


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def test_to_jsonable_round_trips_through_json() -> None:
    fact: Fact[SeedInfo] = Fact(
        value=SeedInfo(
            flavours=("kubuntu", "ubuntustudio"),
            images=(("kubuntu", "daily-live"), ("ubuntustudio", "daily-live")),
            is_core=False,
            binaries_seeded=("kate",),
            binaries_checked=("kate",),
        ),
        status=FactStatus.OK,
        provenance=Provenance(
            source_id="seeds.ubuntuwire",
            method=SourceMethod.HTTP_GET,
            locator="http://qa.ubuntuwire.org/ubuntu-seeded-packages/seeded.json.gz",
            checked_at="2026-09-20T12:00:00Z",
            cache=CacheState.FRESH,
            duration_ms=312,
        ),
    )

    encoded = json.dumps(to_jsonable(fact), sort_keys=True)
    decoded = json.loads(encoded)

    assert decoded["status"] == "OK"
    assert decoded["provenance"]["method"] == "http_get"
    assert decoded["provenance"]["cache"] == "fresh"
    assert decoded["value"]["flavours"] == ["kubuntu", "ubuntustudio"]
    # kate is a genuinely multi-flavour package, which is what drives the
    # cross-flavour acknowledgement rule.
    assert decoded["value"]["is_core"] is False
    assert decoded["value"]["images"] == [
        ["kubuntu", "daily-live"],
        ["ubuntustudio", "daily-live"],
    ]


def test_to_jsonable_is_stable_for_hashing() -> None:
    """The same object must always serialise identically, or fingerprints churn."""
    fact: Fact[int] = Fact(value=149, status=FactStatus.OK, provenance=_prov())
    assert json.dumps(to_jsonable(fact), sort_keys=True) == json.dumps(
        to_jsonable(fact), sort_keys=True
    )
