"""Tests for human-decision detection and the state store."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffe.evidence.human import agrees, assess, decision_from_status, primary_status
from ffe.models import (
    BugFacts,
    BugTask,
    Decision,
    EvidenceBundle,
    Fact,
    FactStatus,
    HumanDecision,
    HumanOutcome,
    Provenance,
    ReviewProvenance,
    ReviewRecord,
    ReviewStatus,
    RiskAssessment,
    SourceMethod,
    SubjectFacts,
)
from ffe.store.repo import BugState, State, Store, touch_seen
from ffe.util.clock import frozen_at


def _bug(*statuses: str, bug_id: int = 2167691) -> BugFacts:
    return BugFacts(
        id=bug_id,
        url="",
        title="[FFe] something",
        description="",
        description_sha="x",
        reporter="dev",
        tasks=tuple(BugTask(f"pkg{i} (Ubuntu)", s, "Undecided") for i, s in enumerate(statuses)),
    )


def _record(bug_id: int = 2167691, fingerprint: str = "sha256:abcdef1234567890") -> ReviewRecord:
    return ReviewRecord(
        schema_version="1.0.0",
        record_id=f"{bug_id}/{fingerprint}",
        status=ReviewStatus.EVIDENCE_ONLY,
        evidence=EvidenceBundle(
            bug=_bug("New", bug_id=bug_id),
            subject=SubjectFacts(packages=("curl",)),
            release_context=Fact(
                value=None,
                status=FactStatus.UNAVAILABLE,
                provenance=Provenance(
                    "calendar", SourceMethod.COMPUTED, "x", "2026-09-20T00:00:00Z"
                ),
            ),
            fingerprint=fingerprint,
        ),
        risk=RiskAssessment(score=42),
        provenance=ReviewProvenance(
            tool_version="0.1.0",
            policy_version="1.0.0",
            policy_hash="sha256:p",
            lessons_hash="sha256:l",
            risk_algorithm_version="risk/1.0.0",
            review_key="sha256:k",
            generated_at="2026-09-20T12:00:00Z",
        ),
    )


# --------------------------------------------------------------------------- #
# Decisions come from status plus subscription, never from comments
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Triaged", HumanDecision.APPROVED),
        ("Fix Committed", HumanDecision.APPROVED),
        ("Fix Released", HumanDecision.APPROVED),
        ("Incomplete", HumanDecision.NEEDS_INFORMATION),
        ("Won't Fix", HumanDecision.REJECTED),
        ("Invalid", HumanDecision.REJECTED),
        ("New", None),
        ("Confirmed", None),
        ("Some Future Status", None),
    ],
)
def test_status_maps_to_decision(status: str, expected: HumanDecision | None) -> None:
    assert decision_from_status(status) is expected


def test_a_bug_still_in_the_queue_is_not_decided() -> None:
    """Subscription outranks status.

    A developer setting their own bug to Fix Committed has not approved it.
    """
    state = assess(_bug("Fix Committed"), in_queue=True)
    assert state.outcome.decided is False
    assert state.outcome.decision is None


def test_leaving_the_queue_with_a_status_is_the_decision() -> None:
    """The real lifecycle: review, set status, unsubscribe."""
    with frozen_at("2026-09-20T12:00:00Z"):
        state = assess(_bug("Triaged"), in_queue=False, left_queue_at="2026-09-20T11:00:00Z")

    assert state.outcome.decided is True
    assert state.outcome.decision is HumanDecision.APPROVED
    assert state.outcome.final_status == "Triaged"
    assert state.outcome.detected_at == "2026-09-20T12:00:00Z"
    assert state.outcome.left_queue_at == "2026-09-20T11:00:00Z"


def test_leaving_the_queue_with_nothing_conclusive_claims_nothing() -> None:
    """An unsubscribe by hand is not a verdict we should invent."""
    outcome = assess(_bug("New"), in_queue=False).outcome
    assert outcome.decided is False
    assert outcome.decision is None


def test_least_settled_task_decides_a_multi_package_bug() -> None:
    """A partly-landed change is not a decided one."""
    assert primary_status(_bug("Fix Committed", "New")) == "New"
    assert primary_status(_bug("Fix Released", "Incomplete")) == "Incomplete"
    assert primary_status(_bug("Fix Committed", "Fix Committed")) == "Fix Committed"


def test_a_bug_with_no_tasks_yields_no_status() -> None:
    assert primary_status(_bug()) == ""


# --------------------------------------------------------------------------- #
# Agreement -- the ground truth the learning loop runs on
# --------------------------------------------------------------------------- #


def test_agreement_maps_between_the_two_vocabularies() -> None:
    """Our advice says APPROVE; the outcome says APPROVED. They must compare."""
    assert agrees(Decision.APPROVE, HumanDecision.APPROVED) is True
    assert agrees("APPROVE", HumanDecision.APPROVED) is True
    assert agrees(Decision.REJECT, HumanDecision.REJECTED) is True
    assert agrees("NEEDS_INFORMATION", HumanDecision.NEEDS_INFORMATION) is True


def test_needs_information_against_approved_is_a_real_disagreement() -> None:
    """Not a near miss: it is the case the learning loop exists to explain."""
    assert agrees("NEEDS_INFORMATION", HumanDecision.APPROVED) is False


def test_agreement_is_unknown_when_either_side_is_missing() -> None:
    assert agrees(None, HumanDecision.APPROVED) is None
    assert agrees("APPROVE", None) is None
    assert agrees("NONSENSE", HumanDecision.APPROVED) is None


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def test_state_round_trips(tmp_path: Path) -> None:
    store = Store(tmp_path)
    state = State()
    state.put(BugState(bug_id=2167691, fingerprint="sha256:aaa", decision="APPROVE"))
    store.save_state(state)

    reloaded = store.load_state()
    assert reloaded.get(2167691).fingerprint == "sha256:aaa"
    assert reloaded.get(2167691).decision == "APPROVE"
    assert reloaded.updated_at


def test_unknown_bug_reads_as_a_fresh_entry(tmp_path: Path) -> None:
    assert Store(tmp_path).load_state().get(999).fingerprint == ""


def test_corrupt_state_costs_one_run_not_the_run(tmp_path: Path) -> None:
    """Everything in state.json is re-derivable from Launchpad."""
    store = Store(tmp_path)
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("{not json")

    assert store.load_state().bugs == {}


def test_unrecognised_fields_drop_that_entry_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store._write_json(
        store.state_path,
        {"version": 1, "bugs": {"1": {"bug_id": 1, "removed_field": "x"}, "2": {"bug_id": 2}}},
    )
    assert set(store.load_state().bugs) == {2}


def test_records_are_keyed_by_fingerprint(tmp_path: Path) -> None:
    """Re-reviewing unchanged evidence must not create a second record."""
    store = Store(tmp_path)
    store.write_record(_record(fingerprint="sha256:aaaaaaaaaaaaaaaa"))
    store.write_record(_record(fingerprint="sha256:aaaaaaaaaaaaaaaa"))
    store.write_record(_record(fingerprint="sha256:bbbbbbbbbbbbbbbb"))

    assert len(store.records_for(2167691)) == 2


def test_latest_points_at_the_most_recent_record(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write_record(_record(fingerprint="sha256:1111111111111111"))
    latest = store.latest_record(2167691)

    assert latest is not None
    assert latest["evidence"]["fingerprint"] == "sha256:1111111111111111"


def test_json_is_written_stably_for_readable_diffs(tmp_path: Path) -> None:
    """A one-field change should show as one changed line in git."""
    store = Store(tmp_path)
    path = store.write_record(_record())
    first = path.read_text()
    store.write_record(_record())
    assert path.read_text() == first
    assert first.endswith("\n")


def test_archiving_keeps_the_whole_record(tmp_path: Path) -> None:
    """The pairing of recommendation and decision is what learning runs on."""
    store = Store(tmp_path)
    record = _record()
    store.archive_record(record)

    assert store.is_archived(2167691)
    archived = store.archived()
    assert len(archived) == 1
    assert archived[0]["evidence"]["subject"]["packages"] == ["curl"]


def test_lessons_hash_changes_as_lessons_are_learned(tmp_path: Path) -> None:
    """Part of the review key, so learning re-reviews the open queue."""
    store = Store(tmp_path)
    empty = store.lessons_hash()

    store.write_lesson(
        {"lesson_id": "L1", "status": "active", "created_at": "2026-09-20T00:00:00Z"}
    )
    with_one = store.lessons_hash()
    assert with_one != empty

    store.write_lesson(
        {"lesson_id": "L2", "status": "active", "created_at": "2026-09-21T00:00:00Z"}
    )
    assert store.lessons_hash() != with_one


def test_retiring_a_lesson_stops_it_applying_without_erasing_it(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write_lesson(
        {"lesson_id": "L1", "status": "active", "created_at": "2026-09-20T00:00:00Z"}
    )

    assert store.retire_lesson("L1", "no longer reflects practice") is True
    assert store.lessons(active_only=True) == []

    retained = store.lessons(active_only=False)
    assert len(retained) == 1
    assert retained[0]["status"] == "retired"
    assert retained[0]["retired_reason"] == "no longer reflects practice"


def test_retiring_an_unknown_lesson_is_reported(tmp_path: Path) -> None:
    assert Store(tmp_path).retire_lesson("nope", "x") is False


def test_lessons_are_newest_first(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write_lesson(
        {"lesson_id": "old", "status": "active", "created_at": "2026-01-01T00:00:00Z"}
    )
    store.write_lesson(
        {"lesson_id": "new", "status": "active", "created_at": "2026-09-01T00:00:00Z"}
    )
    assert [lesson["lesson_id"] for lesson in store.lessons()] == ["new", "old"]


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #


def test_budget_resets_on_a_new_day() -> None:
    bug = BugState(bug_id=1, llm_runs_today=6, llm_runs_date="2026-09-19")
    assert bug.llm_budget_used("2026-09-19") == 6
    assert bug.llm_budget_used("2026-09-20") == 0


def test_never_reviewed_bug_has_infinite_age() -> None:
    assert BugState(bug_id=1).seconds_since_review() == float("inf")


def test_time_since_review_is_measured() -> None:
    bug = BugState(bug_id=1, last_review_at="2026-09-20T12:00:00Z")
    with frozen_at("2026-09-20T13:00:00Z"):
        assert bug.seconds_since_review() == pytest.approx(3600, abs=1)


def test_touch_seen_records_observation_not_review() -> None:
    with frozen_at("2026-09-20T12:00:00Z"):
        updated = touch_seen(
            BugState(bug_id=1),
            date_last_updated="2026-09-20T10:00:00Z",
            message_count=4,
            date_last_message="2026-09-20T09:00:00Z",
        )
    assert updated.message_count == 4
    assert updated.last_seen_at == "2026-09-20T12:00:00Z"
    assert updated.last_review_at == ""


def test_human_outcome_defaults_to_undecided() -> None:
    assert HumanOutcome().decided is False
