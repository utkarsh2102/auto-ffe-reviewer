"""Turning stored reviews into what the dashboard needs.

The dashboard is a triage tool, so the index answers, in order: what needs my
attention, why, and can I open the bug. Everything else lives in the per-bug
payload, one click away.

Summaries are built here rather than in JavaScript so the page stays a renderer.
A reviewer should be able to read site/index.json and see exactly what the page
will show them, and the counts should be reproducible without a browser.
"""

from __future__ import annotations

from typing import Any

from ffe.evidence.human import agrees
from ffe.models import HumanDecision
from ffe.store.repo import Store

INDEX_VERSION = 1

# Risk bands that warrant a second pair of eyes regardless of the recommendation.
ELEVATED_BANDS = ("HIGH", "SEVERE")


def _get(payload: dict[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def summarise(record: dict[str, Any]) -> dict[str, Any]:
    """One row of the triage table."""
    bug = _get(record, "evidence", "bug", default={}) or {}
    subject = _get(record, "evidence", "subject", default={}) or {}
    release = _get(record, "evidence", "release_context", "value", default={}) or {}
    testing = _get(record, "evidence", "testing", default={}) or {}
    flavours = _get(record, "evidence", "flavours", default={}) or {}
    risk = record.get("risk") or {}
    assessment = record.get("assessment")

    packages = _get(record, "evidence", "packages", default=[]) or []
    seeded: list[str] = []
    rdeps = 0
    build_rdeps = 0
    seeds_known = False
    for package in packages:
        seed_value = _get(package, "seeds", "value")
        if seed_value:
            seeds_known = True
            seeded.extend(seed_value.get("flavours") or [])
        rdeps = max(rdeps, int(_get(package, "rdepends", "value", "total", default=0) or 0))
        build_rdeps = max(
            build_rdeps, int(_get(package, "build_rdepends", "value", "total", default=0) or 0)
        )

    return {
        "bug_id": bug.get("id"),
        "url": bug.get("url"),
        "title": bug.get("title"),
        "reporter": bug.get("reporter"),
        "packages": subject.get("packages") or [],
        "ffe_kind": subject.get("ffe_kind"),
        "series": release.get("series"),
        "version": release.get("version"),
        "release_type": release.get("release_type"),
        "phase": release.get("phase"),
        "days_to_release": release.get("days_to_release"),
        # Distinguish "on no image" from "we could not find out", because a
        # dash and a "no" mean very different things to a reviewer.
        "seeded": sorted(set(seeded)) if seeds_known else None,
        "is_core": flavours.get("impact") == "CORE",
        "flavour_impact": flavours.get("impact"),
        "ack_required_from": flavours.get("ack_required_from") or [],
        "rdepends": rdeps,
        "build_rdepends": build_rdeps,
        "testing_corroborated": bool(testing.get("corroborated")),
        "risk_score": risk.get("score"),
        "risk_band": risk.get("band"),
        "decision_floor": risk.get("decision_floor"),
        "decision": _get(record, "assessment", "decision"),
        "confidence": _get(record, "assessment", "confidence"),
        "summary": _get(record, "assessment", "summary"),
        "missing_information": _get(record, "assessment", "missing_information", default=[]) or [],
        "flags": _get(record, "assessment", "flags", default=[]) or [],
        "status": record.get("status"),
        "reviewed_at": _get(record, "provenance", "generated_at"),
        "has_assessment": assessment is not None,
        "unavailable": [
            u.get("source_id") for u in (_get(record, "evidence", "unavailable", default=[]) or [])
        ],
        "injection_signals": len(_get(record, "evidence", "injection_signals", default=[]) or []),
    }


def needs_attention(row: dict[str, Any]) -> bool:
    """Whether a reviewer should look at this now.

    Deliberately broad. Missing a request that needed a human is a worse
    failure than showing one that did not, and the sort order handles the rest.
    """
    return bool(
        row.get("decision") in (None, "NEEDS_INFORMATION", "REJECT")
        or row.get("risk_band") in ELEVATED_BANDS
        or row.get("ack_required_from")
        or row.get("injection_signals")
        or row.get("unavailable")
    )


def build_index(store: Store) -> dict[str, Any]:
    """The whole active queue, with counts."""
    state = store.load_state()
    rows: list[dict[str, Any]] = []

    for bug_id, tracked in state.bugs.items():
        if not tracked.in_queue or store.is_archived(bug_id):
            continue
        record = store.latest_record(bug_id)
        if record:
            rows.append(summarise(record))

    # Most alarming first, then the largest blast radius, then most recent.
    rows.sort(
        key=lambda r: (
            0 if needs_attention(r) else 1,
            -(r.get("risk_score") or 0),
            -(r.get("rdepends") or 0),
            str(r.get("reviewed_at") or ""),
        )
    )

    return {
        "version": INDEX_VERSION,
        "counts": _counts(rows),
        "bugs": rows,
    }


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    def tally(predicate: Any) -> int:
        return sum(1 for row in rows if predicate(row))

    return {
        "open": len(rows),
        "needs_attention": tally(needs_attention),
        "approve": tally(lambda r: r.get("decision") == "APPROVE"),
        "reject": tally(lambda r: r.get("decision") == "REJECT"),
        "needs_information": tally(lambda r: r.get("decision") == "NEEDS_INFORMATION"),
        "no_recommendation": tally(lambda r: not r.get("has_assessment")),
        "high_risk": tally(lambda r: r.get("risk_band") in ELEVATED_BANDS),
        "no_testing_evidence": tally(lambda r: not r.get("testing_corroborated")),
        "awaiting_flavour_ack": tally(lambda r: bool(r.get("ack_required_from"))),
        "evidence_incomplete": tally(lambda r: bool(r.get("unavailable"))),
    }


def build_agreement(store: Store) -> dict[str, Any]:
    """How often our recommendation matched the team's decision.

    Free ground truth, since every decided bug carries both. Published so that
    if the system drifts, or the learning loop makes things worse, it is
    visible on the dashboard rather than discovered later.
    """
    decided: list[dict[str, Any]] = []
    for record in store.archived():
        outcome = record.get("human") or {}
        if not outcome.get("decided"):
            continue
        ours = _get(record, "our_last_assessment", "decision")
        theirs = outcome.get("decision")
        try:
            theirs_enum = HumanDecision(theirs) if theirs else None
        except ValueError:
            theirs_enum = None

        decided.append(
            {
                "bug_id": _get(record, "evidence", "bug", "id"),
                "title": _get(record, "evidence", "bug", "title"),
                "url": _get(record, "evidence", "bug", "url"),
                "ours": ours,
                "theirs": theirs,
                "agreed": agrees(ours, theirs_enum),
                "risk_band": _get(record, "our_last_risk", "band"),
                "decided_at": outcome.get("detected_at"),
            }
        )

    comparable = [d for d in decided if d["agreed"] is not None]
    agreed = sum(1 for d in comparable if d["agreed"])

    return {
        "version": INDEX_VERSION,
        "decided": len(decided),
        "comparable": len(comparable),
        "agreed": agreed,
        # Only meaningful once there is something to measure; a rate over three
        # decisions would invite far more confidence than it deserves.
        "rate": round(agreed / len(comparable), 3) if len(comparable) >= 10 else None,
        "recent": sorted(decided, key=lambda d: str(d.get("decided_at") or ""), reverse=True)[:50],
    }


def build_lessons(store: Store) -> dict[str, Any]:
    """Learned precedents, with the quote each came from."""
    return {
        "version": INDEX_VERSION,
        "active": store.lessons(active_only=True),
        "retired": [
            lesson
            for lesson in store.lessons(active_only=False)
            if lesson.get("status") != "active"
        ],
    }


def build_detail(store: Store, bug_id: int) -> dict[str, Any] | None:
    """Everything known about one bug, including its review history."""
    record = store.latest_record(bug_id)
    if record is None:
        return None
    return {
        "version": INDEX_VERSION,
        "record": record,
        "history": [
            {
                "fingerprint": _get(r, "evidence", "fingerprint"),
                "generated_at": _get(r, "provenance", "generated_at"),
                "status": r.get("status"),
                "decision": _get(r, "assessment", "decision"),
                "confidence": _get(r, "assessment", "confidence"),
                "risk_score": _get(r, "risk", "score"),
            }
            for r in store.records_for(bug_id)
        ],
    }
