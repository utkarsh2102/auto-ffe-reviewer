"""Tests for the curated data files and the record schema.

The data files are the places where human judgement is allowed to override
derived evidence, so their limits matter: they may raise how a package is
treated, never lower it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jsonschema
import pytest

from conftest import Route, default_routes
from ffe.config import load_settings
from ffe.llm.prompt import load_policy
from ffe.llm.replay import ReplayHarness
from ffe.models import EvidenceBundle, SubjectFacts
from ffe.pipeline import Pipeline
from ffe.risk.criticality import Criticality, load_criticality
from ffe.risk.gates import evaluate as apply_gates
from ffe.risk.score import assess as score_risk
from ffe.risk.signals import derive as derive_signals
from ffe.sources.launchpad import client
from ffe.sources.seeds import load_flavours
from ffe.sources.watchlist import load_watchlist
from ffe.store.repo import Store
from ffe.util.clock import frozen_at

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SCHEMA = json.loads((ROOT / "schemas" / "review.v1.schema.json").read_text())
ASSESSMENT_SCHEMA = json.loads((ROOT / "schemas" / "assessment.v1.schema.json").read_text())
LP = Path(__file__).parent / "fixtures" / "raw" / "launchpad"


# --------------------------------------------------------------------------- #
# Criticality overrides
# --------------------------------------------------------------------------- #


def test_the_shipped_overrides_load() -> None:
    criticality = load_criticality()
    assert "grub2" in criticality.always_core
    assert "linux" in criticality.always_core
    assert "shim" in criticality.unrecoverable


def test_every_override_group_states_a_reason() -> None:
    """So a future reader can tell whether it still applies."""
    raw = tomllib.loads((ROOT / "data" / "criticality.toml").read_text(encoding="utf-8"))
    for name, group in raw.items():
        assert group.get("reason", "").strip(), f"[{name}] has no reason"


def test_overrides_only_ever_raise_the_floor() -> None:
    """There is no mechanism for declaring a package less important.

    If the derived evidence overstates something, the evidence is the thing to
    fix. A file that could mark packages as safe would be a way to quietly
    lower the bar.
    """
    raw = tomllib.loads((ROOT / "data" / "criticality.toml").read_text(encoding="utf-8"))
    assert set(raw) <= {"always_core", "unrecoverable_failure"}


def test_an_override_makes_a_package_core_despite_the_seed_index() -> None:
    """The seed index has been seen serving incomplete data; this is insurance."""
    bundle = EvidenceBundle(
        bug=_bug(),
        subject=SubjectFacts(packages=("grub2",)),
        release_context=_missing_fact(),
    )
    signals = derive_signals(bundle)

    assert signals.core_by_override is True
    assert signals.is_core is True


def test_a_package_not_listed_is_unaffected() -> None:
    bundle = EvidenceBundle(
        bug=_bug(), subject=SubjectFacts(packages=("hello",)), release_context=_missing_fact()
    )
    signals = derive_signals(bundle)
    assert signals.core_by_override is False


def test_unrecoverable_failure_is_flagged_not_floored() -> None:
    """The change may still be right; the team should know the kind of risk."""
    bundle = EvidenceBundle(
        bug=_bug(), subject=SubjectFacts(packages=("shim",)), release_context=_missing_fact()
    )
    signals = derive_signals(bundle)
    risk = apply_gates(signals, score_risk(signals))

    gate = next(g for g in risk.gates if g.id == "unrecoverable_failure_mode")
    assert gate.triggered
    assert "no SRU can reach" in gate.rationale
    assert gate.effect.value == "flag"


def test_an_ordinary_package_does_not_trip_it() -> None:
    bundle = EvidenceBundle(
        bug=_bug(), subject=SubjectFacts(packages=("curl",)), release_context=_missing_fact()
    )
    risk = apply_gates(derive_signals(bundle), score_risk(derive_signals(bundle)))
    assert not next(g for g in risk.gates if g.id == "unrecoverable_failure_mode").triggered


def test_a_missing_overrides_file_is_not_an_error() -> None:
    assert load_criticality(Path("/nonexistent.toml")).always_core == frozenset()


def test_a_malformed_overrides_file_degrades_quietly(tmp_path: Path) -> None:
    """Derived evidence still stands on its own."""
    broken = tmp_path / "criticality.toml"
    broken.write_text("this is not = = toml")
    assert load_criticality(broken) == Criticality()


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #


def test_the_shipped_watchlist_is_empty() -> None:
    """It is an escape hatch, not the mechanism. Entries should be temporary."""
    assert load_watchlist() == ()


def test_watchlist_entries_are_read(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.toml"
    path.write_text("[watchlist]\nbugs = [2164597, 2167484]\n")
    assert load_watchlist(path) == (2164597, 2167484)


def test_watchlist_entries_are_deduplicated_and_sanitised(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.toml"
    path.write_text('[watchlist]\nbugs = [1, 1, -5, 0, "nonsense", 2]\n')
    assert load_watchlist(path) == (1, 2)


def test_a_malformed_watchlist_does_not_stop_the_queue(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.toml"
    path.write_text("not = = toml")
    assert load_watchlist(path) == ()


def test_a_watchlisted_bug_joins_the_queue(make_ctx, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """And is marked as having arrived that way, not through the queue."""
    watchlist = tmp_path / "watchlist.toml"
    watchlist.write_text("[watchlist]\nbugs = [2167691]\n")
    monkeypatch.setattr("ffe.pipeline.load_watchlist", lambda: load_watchlist(watchlist))

    routes = [
        Route(r"searchTasks", LP / "searchtasks-ubuntu-release-queue.json"),
        *default_routes(),
    ]
    ctx = make_ctx(routes)
    settings = load_settings(Path("/nonexistent.toml"), env={})
    ctx.settings = settings
    store = Store(tmp_path / "state")

    pipeline = Pipeline(
        settings=settings,
        ctx=ctx,
        store=store,
        launchpad=client(ctx),
        harness=None,
        policy=load_policy(),
        schema=ASSESSMENT_SCHEMA,
        flavours=load_flavours(),
        run_id="watchlist-test",
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run()

    # The queue itself was empty; the watchlist supplied the work.
    assert summary.queue_size == 1
    assert summary.reviewed == [2167691]
    record = store.latest_record(2167691)
    assert record is not None
    assert record["evidence"]["bug"]["discovered_via"] == "watchlist"


# --------------------------------------------------------------------------- #
# The record schema
# --------------------------------------------------------------------------- #


def test_the_review_schema_is_valid() -> None:
    jsonschema.Draft202012Validator.check_schema(REVIEW_SCHEMA)


def test_a_real_record_validates(make_ctx, tmp_path: Path) -> None:
    """The schema describes what is actually written, not an aspiration."""
    routes = [
        Route(r"searchTasks.*ubuntu-release", LP / "searchtasks-juliank.json"),
        *default_routes(),
    ]
    ctx = make_ctx(routes)
    settings = load_settings(Path("/nonexistent.toml"), env={})
    ctx.settings = settings
    store = Store(tmp_path / "state")

    response = json.dumps(
        {
            "decision": "NEEDS_INFORMATION",
            "confidence": "HIGH",
            "summary": "Core seeded package with no testing evidence in the bug.",
            "reasoning": [
                {"point": "curl is seeded on core images.", "evidence_refs": ["/evidence/packages"]}
            ],
            "missing_information": ["A PPA build for stonking."],
        }
    )
    pipeline = Pipeline(
        settings=settings,
        ctx=ctx,
        store=store,
        launchpad=client(ctx),
        harness=ReplayHarness(responses=[response]),
        policy=load_policy(),
        schema=ASSESSMENT_SCHEMA,
        flavours=load_flavours(),
        run_id="schema-test",
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    record = store.latest_record(2167691)
    assert record is not None
    errors = list(jsonschema.Draft202012Validator(REVIEW_SCHEMA).iter_errors(record))
    assert not errors, f"a real record does not match review.v1: {[e.message for e in errors][:3]}"


def test_an_evidence_only_record_validates(make_ctx, tmp_path: Path) -> None:
    """A record with no assessment is still complete, and the schema says so."""
    routes = [
        Route(r"searchTasks.*ubuntu-release", LP / "searchtasks-juliank.json"),
        *default_routes(),
    ]
    ctx = make_ctx(routes)
    settings = load_settings(Path("/nonexistent.toml"), env={})
    ctx.settings = settings
    store = Store(tmp_path / "state")

    pipeline = Pipeline(
        settings=settings,
        ctx=ctx,
        store=store,
        launchpad=client(ctx),
        harness=None,
        policy=load_policy(),
        schema=ASSESSMENT_SCHEMA,
        flavours=load_flavours(),
        run_id="schema-test",
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    record = store.latest_record(2167691)
    assert record is not None
    assert record["assessment"] is None
    assert not list(jsonschema.Draft202012Validator(REVIEW_SCHEMA).iter_errors(record))


def test_the_schema_forbids_a_gate_flooring_at_approve() -> None:
    """Encoded in the schema as well as the code, since it is the core boundary."""
    floor = REVIEW_SCHEMA["properties"]["risk"]["properties"]["decision_floor"]
    assert "APPROVE" not in floor["enum"]


def test_the_schema_keeps_unavailable_distinct_from_false() -> None:
    states = REVIEW_SCHEMA["$defs"]["fact"]["properties"]["status"]["enum"]
    assert {"OK", "UNAVAILABLE", "NOT_APPLICABLE", "ERROR"} == set(states)


def test_the_schema_requires_rationale_on_every_risk_component() -> None:
    component = REVIEW_SCHEMA["properties"]["risk"]["properties"]["components"]["items"]
    assert "rationale" in component["required"]


@pytest.mark.parametrize("version_key", ["policy_hash", "review_key", "policy_version"])
def test_provenance_is_required(version_key: str) -> None:
    assert version_key in REVIEW_SCHEMA["properties"]["provenance"]["required"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _bug():  # type: ignore[no-untyped-def]
    from ffe.models import BugFacts

    return BugFacts(id=1, url="", title="t", description="", description_sha="x", reporter="dev")


def _missing_fact():  # type: ignore[no-untyped-def]
    from ffe.models import Fact, FactStatus, Provenance, SourceMethod

    return Fact(
        value=None,
        status=FactStatus.UNAVAILABLE,
        provenance=Provenance("calendar", SourceMethod.COMPUTED, "x", "2026-09-20T00:00:00Z"),
    )
