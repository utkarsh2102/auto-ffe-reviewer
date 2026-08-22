"""End-to-end pipeline tests.

Everything runs against captured fixtures and a replay harness, so the whole
review cycle is exercised with no network and no API key -- including the
responses a real model would not produce on demand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import Route, default_routes
from ffe.config import load_settings
from ffe.llm.prompt import load_policy
from ffe.llm.replay import ReplayHarness
from ffe.models import Decision, HumanDecision, ReviewStatus
from ffe.pipeline import Pipeline
from ffe.sources.launchpad import client as launchpad_client
from ffe.sources.seeds import load_flavours
from ffe.store.repo import Store
from ffe.util.clock import frozen_at

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "assessment.v1.schema.json").read_text()
)
QUEUE_FIXTURE = Path(__file__).parent / "fixtures" / "raw" / "launchpad"

GOOD_RESPONSE = json.dumps(
    {
        "decision": "NEEDS_INFORMATION",
        "confidence": "HIGH",
        "summary": "Core seeded package with a large blast radius and no testing evidence.",
        "reasoning": [
            {
                "point": "curl is seeded on core Ubuntu images.",
                "evidence_refs": ["/evidence/packages"],
            },
            {"point": "No PPA or build log is linked.", "evidence_refs": ["/evidence/testing"]},
        ],
        "missing_information": ["A PPA build for stonking."],
        "flags": ["CORE_PACKAGE", "NO_TESTING_EVIDENCE"],
    }
)


def _queue_routes(*, queued: bool) -> list[Route]:
    """Route the queue search to either an empty or a populated response."""
    fixture = "searchtasks-juliank.json" if queued else "searchtasks-ubuntu-release-queue.json"
    return [
        Route(r"searchTasks.*ubuntu-release", QUEUE_FIXTURE / fixture),
        Route(r"searchTasks", QUEUE_FIXTURE / "searchtasks-ubuntu-release-queue.json"),
        *default_routes(),
    ]


def _pipeline(
    make_ctx: Any,
    tmp_path: Path,
    *,
    routes: list[Route] | None = None,
    responses: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Pipeline, Store, ReplayHarness | None]:
    ctx = make_ctx(routes if routes is not None else _queue_routes(queued=False))
    ctx.settings = load_settings(Path("/nonexistent.toml"), env=env or {})
    store = Store(tmp_path / "state")
    harness = ReplayHarness(responses=list(responses)) if responses is not None else None
    pipeline = Pipeline(
        settings=ctx.settings,
        ctx=ctx,
        store=store,
        launchpad=launchpad_client(ctx),
        harness=harness,
        policy=load_policy(),
        schema=SCHEMA,
        flavours=load_flavours(),
        run_id="test-run",
    )
    return pipeline, store, harness


# --------------------------------------------------------------------------- #
# The caught-up case
# --------------------------------------------------------------------------- #


def test_empty_queue_is_a_clean_run(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The live state on 2026-09-20, and it must not look like a failure."""
    pipeline, store, _ = _pipeline(make_ctx, tmp_path)
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run()

    assert summary.queue_size == 0
    assert summary.errors == []
    assert summary.llm_calls == 0
    assert store.load_state().bugs == {}


def test_a_run_is_always_logged(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, store, _ = _pipeline(make_ctx, tmp_path)
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run()
    assert (store.runs / "test-run.json").is_file()


def test_an_unreadable_queue_does_not_guess(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Reviewing from the sweep would mean reviewing things nobody asked us to."""
    routes = [Route(r"searchTasks", 503), *default_routes()]
    pipeline, _, _ = _pipeline(make_ctx, tmp_path, routes=routes)

    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run()

    assert summary.errors
    assert summary.reviewed == []


# --------------------------------------------------------------------------- #
# Reviewing without a model
# --------------------------------------------------------------------------- #


def test_the_pipeline_works_with_no_model_at_all(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The property the whole deterministic layer exists to guarantee."""
    pipeline, store, _ = _pipeline(make_ctx, tmp_path, routes=_queue_routes(queued=True))
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run(only=(2167691,))

    assert summary.reviewed == [2167691]
    assert summary.llm_calls == 0

    record = store.latest_record(2167691)
    assert record is not None
    assert record["status"] == ReviewStatus.EVIDENCE_ONLY.value
    assert record["assessment"] is None
    # And it is still a useful record.
    assert record["risk"]["score"] > 0
    assert record["risk"]["decision_floor"] == Decision.NEEDS_INFORMATION.value


def test_evidence_only_records_still_carry_provenance(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, store, _ = _pipeline(make_ctx, tmp_path, routes=_queue_routes(queued=True))
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    record = store.latest_record(2167691)
    assert record is not None
    assert record["provenance"]["policy_hash"].startswith("sha256:")
    assert record["provenance"]["review_key"].startswith("sha256:")
    assert record["provenance"]["run_id"] == "test-run"


# --------------------------------------------------------------------------- #
# Reviewing with a model
# --------------------------------------------------------------------------- #


def test_a_valid_response_is_recorded(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, store, _ = _pipeline(
        make_ctx, tmp_path, routes=_queue_routes(queued=True), responses=[GOOD_RESPONSE]
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run(only=(2167691,))

    assert summary.llm_calls == 1
    record = store.latest_record(2167691)
    assert record is not None
    assert record["status"] == ReviewStatus.REVIEWED.value
    assert record["assessment"]["decision"] == "NEEDS_INFORMATION"
    assert record["provenance"]["harness"]["id"] == "replay"


def test_the_prompt_separates_facts_from_bug_text(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Bug text must never reach the system prompt."""
    pipeline, _, harness = _pipeline(
        make_ctx, tmp_path, routes=_queue_routes(queued=True), responses=[GOOD_RESPONSE]
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    assert harness is not None
    request = harness.requests[0]
    # The policy is trusted content and lives in the system prompt.
    assert "Feature Freeze" in request.system
    # The bug's own words are fenced in the user message, and nowhere else.
    assert "upki finished MIR process" not in request.system
    assert "<untrusted" in request.user


def test_an_unusable_response_is_retried_then_abandoned(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Better an honest empty assessment than a coerced one."""
    pipeline, store, harness = _pipeline(
        make_ctx,
        tmp_path,
        routes=_queue_routes(queued=True),
        responses=["not json at all", "still not json", "nor this"],
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    record = store.latest_record(2167691)
    assert record is not None
    assert record["status"] == ReviewStatus.LLM_OUTPUT_INVALID.value
    assert record["assessment"] is None
    # The deterministic half survives intact.
    assert record["risk"]["score"] > 0
    assert harness is not None
    assert len(harness.requests) == 3


def test_a_repaired_response_is_accepted(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, store, harness = _pipeline(
        make_ctx,
        tmp_path,
        routes=_queue_routes(queued=True),
        responses=["nonsense", GOOD_RESPONSE],
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    record = store.latest_record(2167691)
    assert record is not None
    assert record["status"] == ReviewStatus.REVIEWED.value
    assert record["provenance"]["harness"]["attempts"] == 2
    # The retry carries the errors, not the evidence again.
    assert harness is not None
    assert "Do not restate the evidence" in harness.requests[1].user


def test_a_failing_harness_leaves_the_record_intact(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, store, _ = _pipeline(
        make_ctx, tmp_path, routes=_queue_routes(queued=True), responses=[]
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    record = store.latest_record(2167691)
    assert record is not None
    assert record["status"] == ReviewStatus.LLM_UNAVAILABLE.value
    assert record["risk"]["band"]


# --------------------------------------------------------------------------- #
# Not re-reviewing what has not changed
# --------------------------------------------------------------------------- #


def test_unchanged_evidence_costs_nothing_on_a_second_run(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Most of why this is affordable to run every half hour."""
    pipeline, _store, _harness = _pipeline(
        make_ctx, tmp_path, routes=_queue_routes(queued=True), responses=[GOOD_RESPONSE]
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        first = pipeline.run(only=(2167691,))
    with frozen_at("2026-09-20T13:00:00Z"):
        second = pipeline.run(only=(2167691,))

    assert first.llm_calls == 1
    assert second.llm_calls == 0
    assert second.skipped_unchanged == [2167691]


def test_force_overrides_the_unchanged_check(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, _, _ = _pipeline(
        make_ctx,
        tmp_path,
        routes=_queue_routes(queued=True),
        responses=[GOOD_RESPONSE, GOOD_RESPONSE],
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))
    with frozen_at("2026-09-20T13:00:00Z"):
        second = pipeline.run(only=(2167691,), force=True)

    assert second.llm_calls == 1


def test_editing_the_policy_re_reviews_the_queue(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A change to how requests are judged should change the judgements."""
    pipeline, _, _ = _pipeline(
        make_ctx,
        tmp_path,
        routes=_queue_routes(queued=True),
        responses=[GOOD_RESPONSE, GOOD_RESPONSE],
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    # Same evidence, different policy: the review key must move.
    from dataclasses import replace as dc_replace

    pipeline.policy = dc_replace(pipeline.policy, text=pipeline.policy.text + "\n\nAn amendment.")
    with frozen_at("2026-09-20T13:00:00Z"):
        second = pipeline.run(only=(2167691,))

    assert second.llm_calls == 1
    assert second.reviewed == [2167691]


def test_learning_a_lesson_re_reviews_the_queue(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, store, _ = _pipeline(
        make_ctx,
        tmp_path,
        routes=_queue_routes(queued=True),
        responses=[GOOD_RESPONSE, GOOD_RESPONSE],
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    store.write_lesson(
        {"lesson_id": "L1", "status": "active", "created_at": "2026-09-20T12:30:00Z"}
    )
    with frozen_at("2026-09-20T13:00:00Z"):
        second = pipeline.run(only=(2167691,))

    assert second.llm_calls == 1


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #


def test_a_bug_is_not_re_reviewed_within_the_minimum_interval(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Stops a comment storm burning a day's budget on one bug."""
    pipeline, store, _ = _pipeline(
        make_ctx,
        tmp_path,
        routes=_queue_routes(queued=True),
        responses=[GOOD_RESPONSE, GOOD_RESPONSE],
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    # Force the evidence to look changed, but only five minutes later.
    state = store.load_state()
    tracked = state.get(2167691)
    from dataclasses import replace as dc_replace

    state.put(dc_replace(tracked, review_key="sha256:stale"))
    store.save_state(state)

    with frozen_at("2026-09-20T12:05:00Z"):
        second = pipeline.run(only=(2167691,))

    assert second.llm_calls == 0
    assert second.deferred_budget == [2167691]
    record = store.latest_record(2167691)
    assert record is not None
    assert record["status"] == ReviewStatus.DEFERRED_BUDGET.value


def test_a_deferred_review_is_visible_not_silent(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, store, _ = _pipeline(
        make_ctx,
        tmp_path,
        routes=_queue_routes(queued=True),
        responses=[GOOD_RESPONSE],
        env={"FFE_BUDGET_MAX_LLM_RUNS_PER_RUN": "0"},
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run(only=(2167691,))

    assert summary.deferred_budget == [2167691]
    record = store.latest_record(2167691)
    assert record is not None
    assert record["status"] == ReviewStatus.DEFERRED_BUDGET.value


# --------------------------------------------------------------------------- #
# Departure: the team has decided
# --------------------------------------------------------------------------- #


def test_a_bug_leaving_the_queue_is_archived_with_the_decision(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The real lifecycle, and where the learning loop's ground truth comes from."""
    # First run: the bug is in the queue and gets reviewed.
    pipeline, store, _ = _pipeline(
        make_ctx, tmp_path, routes=_queue_routes(queued=True), responses=[GOOD_RESPONSE]
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))
    assert store.latest_record(2167691) is not None

    # Second run: the queue is empty, because the team triaged and unsubscribed.
    decided, store2, _ = _pipeline(make_ctx, tmp_path, routes=_queue_routes(queued=False))
    with frozen_at("2026-09-21T12:00:00Z"):
        summary = decided.run()

    assert summary.archived == [2167691]
    archived = store2.archived()
    assert len(archived) == 1
    assert archived[0]["human"]["decided"] is True
    # The fixture's tasks are Fix Committed, which is an approval.
    assert archived[0]["human"]["decision"] == HumanDecision.APPROVED.value
    # Our recommendation is kept beside it, which is the whole point.
    assert archived[0]["our_last_assessment"]["decision"] == "NEEDS_INFORMATION"


def test_an_archived_bug_is_not_reviewed_again(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pipeline, _store, _ = _pipeline(
        make_ctx, tmp_path, routes=_queue_routes(queued=True), responses=[GOOD_RESPONSE]
    )
    with frozen_at("2026-09-20T12:00:00Z"):
        pipeline.run(only=(2167691,))

    decided, _, _ = _pipeline(make_ctx, tmp_path, routes=_queue_routes(queued=False))
    with frozen_at("2026-09-21T12:00:00Z"):
        decided.run()
    with frozen_at("2026-09-22T12:00:00Z"):
        third = decided.run()

    assert third.archived == []
    assert third.llm_calls == 0


# --------------------------------------------------------------------------- #
# Process gaps
# --------------------------------------------------------------------------- #


def _sweep_routes() -> list[Route]:
    return [
        Route(
            r"searchTasks.*ubuntu-release", QUEUE_FIXTURE / "searchtasks-ubuntu-release-queue.json"
        ),
        Route(r"searchTasks", QUEUE_FIXTURE / "searchtasks-ffe-sweep.json"),
        *default_routes(),
    ]


def test_process_gaps_are_reported_never_acted_on(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The system never subscribes anyone to anything.

    The fixture is a live sweep from 2026-09-20: real FFe bugs filed this
    cycle that nobody had ruled on, and that ~ubuntu-release was not
    subscribed to. Those are in no one's queue and so will not be reviewed,
    which is worth telling the team -- and is all the system does about it.
    """
    pipeline, _, _ = _pipeline(make_ctx, tmp_path, routes=_sweep_routes())
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run()

    assert summary.process_gaps
    assert 2167484 in summary.process_gaps  # rename the sudo binary package
    assert 2164597 in summary.process_gaps  # Go 1.27 transition
    # Reported only: nothing reviewed, nothing subscribed, nothing spent.
    assert summary.reviewed == []
    assert summary.llm_calls == 0


def test_gaps_are_deduplicated_across_tasks(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A transition FFe carries one task per affected package.

    Bug #2164597, the Go 1.27 transition, appears 24 times in the captured
    sweep. Counting tasks rather than bugs would report it two dozen times.
    """
    pipeline, _, _ = _pipeline(make_ctx, tmp_path, routes=_sweep_routes())
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run()

    assert summary.process_gaps.count(2164597) == 1
    assert len(summary.process_gaps) == len(set(summary.process_gaps))


def test_already_decided_bugs_are_not_gaps(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A Triaged FFe with the team unsubscribed is a finished request.

    Listing those would bury the few that actually need someone's attention.
    """
    pipeline, _, _ = _pipeline(make_ctx, tmp_path, routes=_sweep_routes())
    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run()

    # Both are in the captured sweep, and both were approved before it was taken.
    assert 2167691 not in summary.process_gaps
    assert 2167756 not in summary.process_gaps


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def test_one_bad_source_does_not_end_the_run(make_ctx, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    routes = [Route(r"ubuntu-seeded-packages", 503), *_queue_routes(queued=True)]
    pipeline, store, _ = _pipeline(make_ctx, tmp_path, routes=routes, responses=[GOOD_RESPONSE])

    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run(only=(2167691,))

    assert summary.reviewed == [2167691]
    assert "seeds" in summary.unavailable_sources
    record = store.latest_record(2167691)
    assert record is not None
    # Missing evidence lowers what we are allowed to claim.
    assert record["risk"]["confidence_ceiling"] in {"MEDIUM", "LOW"}


@pytest.mark.parametrize(
    "failing", ["documentation.ubuntu.com", "rdepends/v1", "getPublishedSources"]
)
def test_each_source_can_fail_independently(make_ctx, tmp_path: Path, failing: str) -> None:  # type: ignore[no-untyped-def]
    routes = [Route(failing, 503), *_queue_routes(queued=True)]
    pipeline, store, _ = _pipeline(make_ctx, tmp_path, routes=routes)

    with frozen_at("2026-09-20T12:00:00Z"):
        summary = pipeline.run(only=(2167691,))

    assert summary.reviewed == [2167691]
    assert store.latest_record(2167691) is not None
