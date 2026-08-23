"""Finding cases worth learning from.

A disagreement is where our recommendation and the team's decision differ. It
is the only thing that produces a precedent: agreement teaches nothing beyond
"keep doing that", and recording it would bury the cases that matter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ffe.evidence.human import agrees
from ffe.models import HumanDecision
from ffe.store.repo import Store

# Comments long enough to plausibly contain reasoning. A one-word "approved"
# is a decision, not an explanation, and a precedent drawn from it would be
# invented rather than learned.
MIN_EXPLANATION_CHARS = 40


@dataclass(frozen=True, slots=True)
class Disagreement:
    """A decided bug where the team and the reviewer differed."""

    bug_id: int
    title: str
    url: str
    our_decision: str
    human_decision: str
    situation: dict[str, Any]
    comments: tuple[dict[str, str], ...] = field(default_factory=tuple)

    @property
    def has_explanation(self) -> bool:
        return any(len(c["content"]) >= MIN_EXPLANATION_CHARS for c in self.comments)


def _facets(record: dict[str, Any]) -> dict[str, Any]:
    """The situation, reduced to the facets a precedent can be matched on."""
    evidence = record.get("evidence") or {}
    release = ((evidence.get("release_context") or {}).get("value")) or {}
    flavours = evidence.get("flavours") or {}
    risk = record.get("our_last_risk") or {}

    seeded: bool | None = None
    is_core: bool | None = None
    for package in evidence.get("packages") or []:
        seeds = (package.get("seeds") or {}).get("value")
        if seeds:
            seeded = bool(seeded) or bool(seeds.get("flavours"))
            is_core = bool(is_core) or bool(seeds.get("is_core"))

    facets = {
        "ffe_kind": (evidence.get("subject") or {}).get("ffe_kind"),
        "seeded": seeded,
        "is_core": is_core,
        "is_lts": release.get("release_type") == "LTS" if release.get("release_type") else None,
        "flavour_impact": flavours.get("impact"),
        "risk_band": risk.get("band"),
        "phase": release.get("phase"),
        "packages": (evidence.get("subject") or {}).get("packages") or [],
        "testing_corroborated": (evidence.get("testing") or {}).get("corroborated"),
        "rdepends": max(
            (
                int(((p.get("rdepends") or {}).get("value") or {}).get("total") or 0)
                for p in (evidence.get("packages") or [])
            ),
            default=0,
        ),
    }
    return {k: v for k, v in facets.items() if v is not None}


def _release_team_comments(record: dict[str, Any]) -> tuple[dict[str, str], ...]:
    """Comments by Release Team members, which are the only admissible input."""
    comments = ((record.get("evidence") or {}).get("bug") or {}).get("comments") or []
    return tuple(
        {
            "author": str(c.get("author", "")),
            "content": str(c.get("content", "")).strip(),
            "date": str(c.get("date_created", "")),
        }
        for c in comments
        if c.get("author_is_release_team") and str(c.get("content", "")).strip()
    )


def find_disagreements(
    store: Store, *, already_learned: frozenset[int] = frozenset()
) -> list[Disagreement]:
    """Archived bugs where the team decided differently from our recommendation."""
    found: list[Disagreement] = []

    for record in store.archived():
        outcome = record.get("human") or {}
        if not outcome.get("decided"):
            continue

        bug = (record.get("evidence") or {}).get("bug") or {}
        bug_id = bug.get("id")
        if bug_id is None or int(bug_id) in already_learned:
            continue

        ours = ((record.get("our_last_assessment") or {}) or {}).get("decision")
        theirs = outcome.get("decision")
        if not ours or not theirs:
            continue  # Nothing to compare: we never recommended anything.

        try:
            matched = agrees(ours, HumanDecision(theirs))
        except ValueError:
            continue
        if matched is not False:
            continue

        found.append(
            Disagreement(
                bug_id=int(bug_id),
                title=str(bug.get("title", "")),
                url=str(bug.get("url", "")),
                our_decision=str(ours),
                human_decision=str(theirs),
                situation=_facets(record),
                comments=_release_team_comments(record),
            )
        )

    return found


def learnable(disagreements: list[Disagreement]) -> list[Disagreement]:
    """Those where a Release Team member actually explained themselves.

    A disagreement with no explanation is still worth counting in the agreement
    metric, but there is nothing to learn from it, and inventing a reason would
    be worse than leaving it alone.
    """
    return [d for d in disagreements if d.has_explanation]
