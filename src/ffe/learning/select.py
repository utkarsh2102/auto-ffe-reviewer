"""Choosing which precedents to put in front of a review.

Relevance first, recency second, and a hard cap. An unbounded precedent list
would grow the prompt without limit as the system learns, and a review carrying
forty loosely-related precedents is worse than one carrying three apt ones --
the apt ones stop standing out.
"""

from __future__ import annotations

from typing import Any

from ffe.risk.signals import Signals

# Facets weighted by how much they change the shape of a request. Seeding and
# flavour ownership decide who the decision belongs to; the kind of change and
# where we are in the cycle decide how much latitude there is.
FACET_WEIGHTS: dict[str, int] = {
    "flavour_impact": 5,
    "is_core": 5,
    "ffe_kind": 4,
    "is_lts": 3,
    "seeded": 3,
    "risk_band": 2,
    "phase": 2,
}

MIN_SCORE = 3


def facets_for(signals: Signals) -> dict[str, Any]:
    """The current request, expressed in the same vocabulary as a precedent."""
    return {
        "ffe_kind": signals.ffe_kind.value,
        "seeded": bool(signals.seeded_flavours) or bool(signals.is_core),
        "is_core": bool(signals.is_core),
        "is_lts": signals.release_type.value == "LTS" if signals.release_type else None,
        "flavour_impact": signals.flavour_impact.value,
        "phase": signals.phase,
    }


def relevance(lesson: dict[str, Any], facets: dict[str, Any]) -> int:
    """How well a precedent matches the request. Higher is better."""
    applies = lesson.get("applies_when") or {}
    if not applies:
        # A precedent claiming to apply everywhere is usually one that says
        # nothing. Admit it only weakly.
        return 1

    score = 0
    for key, wanted in applies.items():
        actual = facets.get(key)
        if actual is None:
            continue
        if actual == wanted:
            score += FACET_WEIGHTS.get(key, 1)
        else:
            # A stated facet that does not match is evidence against, not
            # merely absent: a precedent about an LTS says little about an
            # interim release.
            score -= FACET_WEIGHTS.get(key, 1)
    return score


def select(
    lessons: list[dict[str, Any]],
    signals: Signals,
    *,
    max_lessons: int = 15,
) -> list[dict[str, Any]]:
    """The precedents worth showing for this request, best first."""
    facets = facets_for(signals)

    scored = [
        (relevance(lesson, facets), str(lesson.get("created_at", "")), lesson)
        for lesson in lessons
        if lesson.get("status", "active") == "active"
    ]
    # Most relevant first; newest first among equals, since recent practice
    # is the better guide when two precedents fit equally well.
    relevant = sorted(
        (entry for entry in scored if entry[0] >= MIN_SCORE),
        key=lambda entry: (-entry[0], entry[1]),
        reverse=False,
    )
    relevant.sort(key=lambda entry: (-entry[0], _descending(entry[1])))

    return [lesson for _, _, lesson in relevant[:max_lessons]]


def _descending(created_at: str) -> tuple[int, ...]:
    """Sort key inverting an ISO timestamp, so newer sorts first."""
    return tuple(-ord(c) for c in created_at)
