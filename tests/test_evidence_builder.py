"""End-to-end evidence gathering, against real captured upstream data.

The main subject is LP #2167691, the genuine curl/upki FFe filed on 2026-09-18:
a core seeded package, enormous blast radius, late in an interim cycle, with no
PPA or test evidence in the bug. It was approved, and it then regressed with an
FTBFS in nheko -- which makes it a useful measure of whether the evidence the
system gathers would have given a reviewer the right things to worry about.
"""

from __future__ import annotations

from pathlib import Path

from conftest import Route, default_routes
from ffe.evidence.builder import build, unavailable_sources
from ffe.evidence.fingerprint import explain_change, fingerprint, review_key, semantic_subset
from ffe.models import EvidenceState, FactStatus, FlavourImpact, ReleaseType
from ffe.sources.launchpad import client
from ffe.sources.seeds import load_flavours
from ffe.util.clock import frozen_at

SERIES = ("stonking", "resolute", "questing")
FLAVOURS = load_flavours()


def _build(ctx, bug_id: int = 2167691):  # type: ignore[no-untyped-def]
    lp = client(ctx)
    bug = lp.fetch_bug(bug_id)
    assert bug.value is not None
    return build(
        ctx,
        bug.value,
        launchpad=lp,
        flavours=FLAVOURS,
        known_series=SERIES,
        default_series="stonking",
    )


# --------------------------------------------------------------------------- #
# The whole picture
# --------------------------------------------------------------------------- #


def test_bundle_for_the_real_curl_ffe(make_ctx) -> None:  # type: ignore[no-untyped-def]
    with frozen_at("2026-09-20T12:00:00Z"):
        result = _build(make_ctx())
    bundle = result.bundle

    assert bundle.bug.id == 2167691
    assert bundle.subject.packages == ("curl", "upki")
    assert bundle.subject.target_series == "stonking"

    context = bundle.release_context.value
    assert context is not None
    assert context.release_type is ReleaseType.INTERIM
    assert context.phase == "UI_FREEZE"
    assert context.days_to_release == 25


def test_seed_lookup_uses_binaries_not_the_source_name(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """Why the archive lookup has to come first.

    The seed index is keyed by binary. Looking up "curl" alone would miss
    libcurl4t64, which is the package the archive actually depends on.
    """
    with frozen_at("2026-09-20T12:00:00Z"):
        bundle = _build(make_ctx()).bundle

    curl = next(p for p in bundle.packages if p.name == "curl")
    assert curl.archive.value is not None
    assert "libcurl4t64" in curl.archive.value.binaries
    assert curl.archive.value.component == "main"
    assert curl.archive.value.in_main is True

    assert curl.seeds.value is not None
    assert set(curl.seeds.value.binaries_checked) >= {"curl", "libcurl4t64"}


def test_core_package_is_identified_as_core(make_ctx) -> None:  # type: ignore[no-untyped-def]
    with frozen_at("2026-09-20T12:00:00Z"):
        bundle = _build(make_ctx()).bundle

    curl = next(p for p in bundle.packages if p.name == "curl")
    assert curl.seeds.value is not None
    assert curl.seeds.value.is_core is True
    assert bundle.flavours.impact is FlavourImpact.CORE


def test_blast_radius_is_measured(make_ctx) -> None:  # type: ignore[no-untyped-def]
    with frozen_at("2026-09-20T12:00:00Z"):
        bundle = _build(make_ctx()).bundle

    curl = next(p for p in bundle.packages if p.name == "curl")
    assert curl.rdepends.value is not None
    assert curl.rdepends.value.total == 711
    assert curl.rdepends.value.bucket == "500+"
    assert curl.build_rdepends.value is not None
    assert curl.build_rdepends.value.total == 529


def test_the_missing_testing_evidence_is_the_headline(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """This bug carried no PPA, no build log and no test output -- and regressed.

    Whatever else the review says, a reviewer should have been told that a
    change to a package with 711 reverse dependencies arrived with nothing to
    check.
    """
    with frozen_at("2026-09-20T12:00:00Z"):
        bundle = _build(make_ctx()).bundle

    assert bundle.testing.corroborated is False
    assert bundle.testing.ppa is EvidenceState.ABSENT
    assert bundle.testing.build is EvidenceState.ABSENT
    assert bundle.testing.autopkgtest is EvidenceState.ABSENT


def test_everything_carries_provenance(make_ctx) -> None:  # type: ignore[no-untyped-def]
    with frozen_at("2026-09-20T12:00:00Z"):
        bundle = _build(make_ctx()).bundle

    curl = next(p for p in bundle.packages if p.name == "curl")
    for fact in (curl.archive, curl.seeds, curl.rdepends, curl.build_rdepends):
        assert fact.provenance.source_id
        assert fact.provenance.locator
        assert fact.provenance.checked_at
    assert bundle.release_context.provenance.locator.startswith("https://")


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def test_a_dead_source_does_not_stop_the_rest(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """The seeded-in-ubuntu outage, end to end."""
    routes = [Route(r"ubuntu-seeded-packages", 503), *default_routes()]
    with frozen_at("2026-09-20T12:00:00Z"):
        bundle = _build(make_ctx(routes)).bundle

    curl = next(p for p in bundle.packages if p.name == "curl")
    assert curl.seeds.status is FactStatus.UNAVAILABLE
    # Everything else still arrived.
    assert curl.rdepends.ok
    assert curl.archive.ok
    assert bundle.release_context.ok
    # And the gap is recorded, with what it costs.
    assert "seeds" in unavailable_sources(bundle)
    assert any("images" in u.impact for u in bundle.unavailable)


def test_degraded_seed_index_does_not_claim_unseeded(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """A partial index must not be read as evidence of absence."""
    routes = default_routes(seeds="degraded", package="ardour")
    routes = [
        Route(r"source_name=", Path("tests/fixtures/raw/launchpad/published-sources-ardour.json")),
        *routes,
    ]
    with frozen_at("2026-09-20T12:00:00Z"):
        bundle = _build(make_ctx(routes)).bundle

    for package in bundle.packages:
        if package.seeds.status is FactStatus.OK and package.seeds.value:
            continue
        assert package.seeds.status is FactStatus.UNAVAILABLE


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #


def test_same_evidence_gives_the_same_fingerprint(make_ctx) -> None:  # type: ignore[no-untyped-def]
    with frozen_at("2026-09-20T12:00:00Z"):
        first = fingerprint(_build(make_ctx()).bundle)
    with frozen_at("2026-09-20T13:30:00Z"):
        second = fingerprint(_build(make_ctx()).bundle)

    assert first == second, "an hour passing must not trigger a re-review"


def test_a_day_passing_inside_a_bucket_changes_nothing(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """The anti-churn property this design exists for.

    With a raw days-until-release in the fingerprint, every open bug would be
    re-reviewed at every midnight, for no new information.

    2026-09-18 and 2026-09-20 are 27 and 25 days from release: same bucket,
    same phase (UI Freeze, since Beta Freeze does not land until the 21st).
    """
    with frozen_at("2026-09-18T12:00:00Z"):
        earlier = fingerprint(_build(make_ctx()).bundle)
    with frozen_at("2026-09-20T12:00:00Z"):
        later = fingerprint(_build(make_ctx()).bundle)

    assert earlier == later


def test_crossing_a_milestone_does_trigger_a_re_review(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """Beta Freeze lands on 2026-09-21, and the bar moves with it.

    Two days apart, same days-to-release bucket, different phase -- so the
    fingerprint moves for a reason a reviewer would agree with.
    """
    with frozen_at("2026-09-20T12:00:00Z"):
        before = semantic_subset(_build(make_ctx()).bundle)
    with frozen_at("2026-09-22T12:00:00Z"):
        after = semantic_subset(_build(make_ctx()).bundle)

    assert before["release"]["days_bucket"] == after["release"]["days_bucket"]
    assert before["release"]["phase"] == "UI_FREEZE"
    assert after["release"]["phase"] == "BETA_FREEZE"
    assert explain_change(before, after) == ("release",)


def test_crossing_a_bucket_boundary_does_trigger_a_re_review(make_ctx) -> None:  # type: ignore[no-untyped-def]
    """25 days out and 10 days out are genuinely different situations."""
    with frozen_at("2026-09-20T12:00:00Z"):
        early = semantic_subset(_build(make_ctx()).bundle)
    with frozen_at("2026-10-05T12:00:00Z"):
        late = semantic_subset(_build(make_ctx()).bundle)

    assert early != late
    assert "release" in explain_change(early, late)


def test_subset_excludes_volatile_detail(make_ctx) -> None:  # type: ignore[no-untyped-def]
    with frozen_at("2026-09-20T12:00:00Z"):
        subset = semantic_subset(_build(make_ctx()).bundle)

    rendered = repr(subset)
    assert "checked_at" not in rendered
    assert "duration_ms" not in rendered
    assert "raw_ref" not in rendered
    # Buckets are kept, exact counts are not.
    assert "500+" in rendered
    assert "711" not in rendered


def test_review_key_widens_beyond_the_evidence() -> None:
    """Editing the rubric or learning a precedent must re-review the queue."""
    base = {
        "evidence_fingerprint": "sha256:aaa",
        "policy_hash": "sha256:p1",
        "lessons_hash": "sha256:l1",
        "risk_algorithm_version": "risk/1.0.0",
        "harness_id": "claude-code",
        "model": "m",
    }
    original = review_key(**base)

    assert review_key(**{**base, "policy_hash": "sha256:p2"}) != original
    assert review_key(**{**base, "lessons_hash": "sha256:l2"}) != original
    assert review_key(**{**base, "model": "other"}) != original
    assert review_key(**base) == original


def test_explain_change_names_the_section() -> None:
    before = {"bug": {"a": 1}, "testing": {"ppa": "ABSENT"}}
    after = {"bug": {"a": 1}, "testing": {"ppa": "FOUND_VERIFIED"}}
    assert explain_change(before, after) == ("testing",)
