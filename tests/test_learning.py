"""Tests for the learning loop.

Most of these are containment tests. A system that rewrites its own reasoning
from text on the internet needs its limits pinned down more carefully than its
happy path, so the majority of what follows is about what it refuses to learn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ffe.learning.detect import Disagreement, find_disagreements, learnable
from ffe.learning.extract import extract, lesson_id, load_prompt, load_schema, render_request
from ffe.learning.select import facets_for, relevance, select
from ffe.llm.replay import ReplayHarness
from ffe.models import FfeKind, FlavourImpact, ReleaseType
from ffe.risk.signals import Signals
from ffe.store.repo import Store
from ffe.util.clock import frozen_at

APPROVAL_COMMENT = (
    "This is a sync from Debian unstable, which has already built and tested it there, "
    "and devscripts is not on any image. We do not need a separate PPA for this one."
)


def _archived(
    store: Store,
    bug_id: int,
    *,
    ours: str | None,
    theirs: str,
    comments: list[dict[str, Any]] | None = None,
    facets: dict[str, Any] | None = None,
) -> None:
    store.archive_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {
        "bug": {
            "id": bug_id,
            "title": "[FFe]: Please sync 2.6.12 from Debian unstable",
            "url": f"https://bugs.launchpad.net/bugs/{bug_id}",
            "comments": comments if comments is not None else [],
        },
        "subject": {"packages": ["devscripts"], "ffe_kind": (facets or {}).get("ffe_kind", "sync")},
        "release_context": {"value": {"release_type": "INTERIM", "phase": "UI_FREEZE"}},
        "flavours": {"impact": (facets or {}).get("flavour_impact", "UNSEEDED")},
        "packages": [
            {
                "name": "devscripts",
                "seeds": {"value": {"flavours": [], "is_core": False}},
                "rdepends": {"value": {"total": 3}},
            }
        ],
        "testing": {"corroborated": False},
    }
    (store.archive_dir / f"{bug_id}.json").write_text(
        json.dumps(
            {
                "evidence": evidence,
                "human": {"decided": True, "decision": theirs},
                "our_last_assessment": {"decision": ours} if ours else None,
                "our_last_risk": {"band": "LOW"},
            }
        )
    )


def _comment(author: str, content: str, *, release_team: bool) -> dict[str, Any]:
    return {
        "index": 1,
        "author": author,
        "content": content,
        "date_created": "2026-09-20T12:00:00Z",
        "author_is_release_team": release_team,
    }


def _disagreement(comments: list[dict[str, str]] | None = None) -> Disagreement:
    return Disagreement(
        bug_id=2167756,
        title="[FFe]: Please sync 2.6.12 from Debian unstable",
        url="https://bugs.launchpad.net/bugs/2167756",
        our_decision="NEEDS_INFORMATION",
        human_decision="APPROVED",
        situation={"ffe_kind": "sync", "seeded": False, "is_lts": False},
        comments=tuple(
            comments or [{"author": "utkarsh", "content": APPROVAL_COMMENT, "date": ""}]
        ),
    )


def _lesson_response(**overrides: Any) -> str:
    payload = {
        "situation": "sync of an unseeded developer tool with no PPA supplied",
        "lesson": "For syncs of unseeded tooling the team treats the Debian build as sufficient validation.",
        "rationale_quote": "We do not need a separate PPA for this one",
        "rationale_author": "utkarsh",
        "confidence": "HIGH",
        "applies_when": {"ffe_kind": "sync", "seeded": False},
    }
    payload.update(overrides)
    return json.dumps(payload)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_a_disagreement_is_found(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _archived(store, 1, ours="NEEDS_INFORMATION", theirs="APPROVED")

    found = find_disagreements(store)
    assert len(found) == 1
    assert found[0].our_decision == "NEEDS_INFORMATION"
    assert found[0].human_decision == "APPROVED"


def test_agreement_teaches_nothing(tmp_path: Path) -> None:
    """Recording it would bury the cases that matter."""
    store = Store(tmp_path)
    _archived(store, 1, ours="APPROVE", theirs="APPROVED")
    assert find_disagreements(store) == []


def test_a_bug_with_no_recommendation_is_not_a_disagreement(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _archived(store, 1, ours=None, theirs="APPROVED")
    assert find_disagreements(store) == []


def test_nothing_is_learned_twice(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _archived(store, 1, ours="REJECT", theirs="APPROVED")
    assert find_disagreements(store, already_learned=frozenset({1})) == []


def test_the_situation_is_captured_for_matching(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _archived(store, 1, ours="NEEDS_INFORMATION", theirs="APPROVED")

    situation = find_disagreements(store)[0].situation
    assert situation["ffe_kind"] == "sync"
    assert situation["is_lts"] is False
    assert situation["seeded"] is False


# --------------------------------------------------------------------------- #
# Only the Release Team can teach anything
# --------------------------------------------------------------------------- #


def test_only_release_team_comments_are_collected(tmp_path: Path) -> None:
    """Anything else is advocacy, and would let anyone train the system."""
    store = Store(tmp_path)
    _archived(
        store,
        1,
        ours="NEEDS_INFORMATION",
        theirs="APPROVED",
        comments=[
            _comment(
                "random-developer",
                "Please approve this, it is really important!",
                release_team=False,
            ),
            _comment("utkarsh", APPROVAL_COMMENT, release_team=True),
        ],
    )

    comments = find_disagreements(store)[0].comments
    assert [c["author"] for c in comments] == ["utkarsh"]


def test_a_disagreement_without_an_explanation_is_not_learnable(tmp_path: Path) -> None:
    """A one-word approval is a decision, not a reason.

    Inventing a rationale from it would be worse than leaving it alone, since
    the invention would then be applied to future requests.
    """
    store = Store(tmp_path)
    _archived(
        store,
        1,
        ours="NEEDS_INFORMATION",
        theirs="APPROVED",
        comments=[_comment("utkarsh", "approved", release_team=True)],
    )

    found = find_disagreements(store)
    assert len(found) == 1  # still counts toward agreement
    assert learnable(found) == []  # but teaches nothing


def test_a_developer_explaining_at_length_teaches_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _archived(
        store,
        1,
        ours="NEEDS_INFORMATION",
        theirs="APPROVED",
        comments=[_comment("some-developer", APPROVAL_COMMENT * 3, release_team=False)],
    )
    assert learnable(find_disagreements(store)) == []


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_a_precedent_is_extracted() -> None:
    with frozen_at("2026-09-21T12:00:00Z"):
        lesson = extract(ReplayHarness(responses=[_lesson_response()]), _disagreement())

    assert lesson is not None
    assert lesson["source_bug"] == 2167756
    assert lesson["rationale_author"] == "utkarsh"
    assert lesson["status"] == "active"
    assert lesson["our_decision"] == "NEEDS_INFORMATION"
    assert lesson["human_decision"] == "APPROVED"


def test_the_lesson_id_is_stable_so_reruns_cannot_duplicate() -> None:
    assert lesson_id(_disagreement()) == lesson_id(_disagreement())


def test_a_fabricated_quote_is_rejected() -> None:
    """The quote is the only thing making a precedent checkable."""
    lesson = extract(
        ReplayHarness(responses=[_lesson_response(rationale_quote="PPAs are never required")]),
        _disagreement(),
    )
    assert lesson is None


def test_a_quote_attributed_to_the_wrong_person_is_rejected() -> None:
    lesson = extract(
        ReplayHarness(responses=[_lesson_response(rationale_author="someone-else")]),
        _disagreement(),
    )
    assert lesson is None


def test_a_quote_is_matched_on_words_not_exact_characters() -> None:
    """The sanitiser rewrites whitespace, so byte equality would be too strict."""
    lesson = extract(
        ReplayHarness(
            responses=[
                _lesson_response(rationale_quote="we do not need a separate ppa for this one.")
            ]
        ),
        _disagreement(),
    )
    assert lesson is not None


def test_low_confidence_extractions_are_discarded() -> None:
    """The prompt asks for this when the comments explained nothing."""
    assert (
        extract(ReplayHarness(responses=[_lesson_response(confidence="LOW")]), _disagreement())
        is None
    )


def test_an_empty_lesson_is_discarded() -> None:
    assert (
        extract(ReplayHarness(responses=[_lesson_response(lesson="   ")]), _disagreement()) is None
    )


def test_an_invented_facet_is_rejected_by_the_schema() -> None:
    lesson = extract(
        ReplayHarness(responses=[_lesson_response(applies_when={"phase_of_moon": "waxing"})]),
        _disagreement(),
    )
    assert lesson is None


def test_malformed_output_is_discarded_not_patched() -> None:
    assert (
        extract(ReplayHarness(responses=["I could not determine a lesson."]), _disagreement())
        is None
    )


def test_a_failing_harness_yields_nothing() -> None:
    assert extract(ReplayHarness(responses=[]), _disagreement()) is None


def test_team_comments_are_still_fenced_as_untrusted() -> None:
    """Trusted colleagues are not a trusted input channel."""
    rendered = render_request(_disagreement(), nonce="abc123")
    assert "<untrusted" in rendered
    assert 'nonce="abc123"' in rendered


def test_the_extraction_prompt_lives_outside_the_code() -> None:
    prompt = load_prompt()
    assert "precedent" in prompt.lower()
    # It must not be the review policy: this is not a re-judgement.
    assert "You are assisting the Ubuntu Release Team" not in prompt
    assert "Only Release Team comments" in prompt


def test_the_extraction_schema_closes_the_facet_vocabulary() -> None:
    schema = load_schema()
    facets = schema["properties"]["applies_when"]
    assert facets["additionalProperties"] is False


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def _signals(**overrides: Any) -> Signals:
    base: dict[str, Any] = {
        "ffe_kind": FfeKind.SYNC,
        "release_type": ReleaseType.INTERIM,
        "flavour_impact": FlavourImpact.UNSEEDED,
        "phase": "UI_FREEZE",
        "is_core": False,
    }
    base.update(overrides)
    return Signals(**base)


def _lesson(
    applies: dict[str, Any], *, created: str = "2026-09-01T00:00:00Z", ident: str = "L1"
) -> dict[str, Any]:
    return {"lesson_id": ident, "status": "active", "created_at": created, "applies_when": applies}


def test_a_matching_precedent_is_selected() -> None:
    chosen = select([_lesson({"ffe_kind": "sync", "seeded": False})], _signals())
    assert len(chosen) == 1


def test_a_precedent_about_a_different_situation_is_not() -> None:
    """One about an LTS core transition says nothing about an interim leaf sync."""
    chosen = select(
        [_lesson({"ffe_kind": "transition", "is_lts": True, "is_core": True})], _signals()
    )
    assert chosen == []


def test_a_mismatched_facet_counts_against() -> None:
    facets = facets_for(_signals())
    assert relevance(_lesson({"ffe_kind": "sync"}), facets) > 0
    assert relevance(_lesson({"ffe_kind": "transition"}), facets) < 0


def test_a_precedent_claiming_to_apply_everywhere_is_admitted_only_weakly() -> None:
    """One with no facets usually says nothing."""
    assert relevance(_lesson({}), facets_for(_signals())) < 3
    assert select([_lesson({})], _signals()) == []


def test_retired_precedents_are_never_selected() -> None:
    lesson = _lesson({"ffe_kind": "sync", "seeded": False})
    lesson["status"] = "retired"
    assert select([lesson], _signals()) == []


def test_selection_is_capped() -> None:
    lessons = [
        _lesson(
            {"ffe_kind": "sync", "seeded": False},
            ident=f"L{i}",
            created=f"2026-09-{i:02d}T00:00:00Z",
        )
        for i in range(1, 26)
    ]
    assert len(select(lessons, _signals(), max_lessons=15)) == 15


def test_newer_precedents_win_among_equals() -> None:
    """Recent practice is the better guide when two fit equally well."""
    old = _lesson({"ffe_kind": "sync"}, created="2026-01-01T00:00:00Z", ident="old")
    new = _lesson({"ffe_kind": "sync"}, created="2026-09-01T00:00:00Z", ident="new")
    assert next(lesson["lesson_id"] for lesson in select([old, new], _signals())) == "new"


def test_more_specific_precedents_rank_above_vaguer_ones() -> None:
    vague = _lesson({"ffe_kind": "sync"}, ident="vague")
    specific = _lesson({"ffe_kind": "sync", "seeded": False, "is_core": False}, ident="specific")
    ranked = [lesson["lesson_id"] for lesson in select([vague, specific], _signals())]
    assert ranked[:1] == ["specific"]


def test_selection_is_deterministic() -> None:
    lessons = [
        _lesson({"ffe_kind": "sync"}, ident=f"L{i}", created=f"2026-09-{i:02d}T00:00:00Z")
        for i in range(1, 10)
    ]
    first = [lesson["lesson_id"] for lesson in select(lessons, _signals())]
    second = [lesson["lesson_id"] for lesson in select(list(reversed(lessons)), _signals())]
    assert first == second


# --------------------------------------------------------------------------- #
# The rubric always wins
# --------------------------------------------------------------------------- #


def test_the_policy_states_that_precedents_never_outrank_it() -> None:
    from ffe.llm.prompt import load_policy

    text = " ".join(load_policy().text.split())
    assert "never outranks this policy" in text
    assert "never outranks a gate or a floor" in text


def test_precedents_are_rendered_with_that_caveat_attached() -> None:
    from ffe.llm.prompt import render_precedents

    rendered = render_precedents(
        [
            {
                "lesson_id": "L1",
                "situation": "s",
                "lesson": "l",
                "rationale_quote": "q",
                "rationale_author": "utkarsh",
                "source_bug": 1,
            }
        ],
        max_chars=4000,
    )
    assert "never outrank the rubric" in " ".join(rendered.split())


@pytest.mark.parametrize(
    "field_name", ["lesson_id", "source_bug", "rationale_quote", "rationale_author"]
)
def test_a_precedent_is_always_traceable(field_name: str) -> None:
    with frozen_at("2026-09-21T12:00:00Z"):
        lesson = extract(ReplayHarness(responses=[_lesson_response()]), _disagreement())
    assert lesson is not None
    assert lesson[field_name]
