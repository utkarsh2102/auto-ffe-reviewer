"""Deciding when a review is worth redoing.

Running the model on every bug every half hour would cost a great deal and
change nothing: the overwhelming majority of runs find a bug exactly as it was.
So a fingerprint is taken over the facts that could change a recommendation,
and the model is consulted only when that fingerprint moves.

Choosing what goes in is the whole design. Two rules:

**Bucket continuous values.** Days-until-release is the trap. Include the raw
number and the fingerprint changes at every midnight, so every open bug gets
re-reviewed daily for no new information -- and twice over, since nothing about
the request actually changed. The bucket boundaries are where the Release
Team's tolerance genuinely shifts, so crossing one is a real event and the days
in between are not.

**Exclude anything that moves on its own.** Timestamps, durations, cache
states, raw-blob references and exact dependency counts all change without the
request changing. A package going from 148 to 149 reverse dependencies is not
news; going from a handful to hundreds is, and the bucket catches that.

The fingerprint deliberately does *not* include our own output. It describes
the request and its evidence, so that re-reviewing the same facts yields the
same key however the model answered last time.
"""

from __future__ import annotations

from typing import Any

from ffe.models import EvidenceBundle, FactStatus
from ffe.util.hashing import digest

# Fingerprint layout version. Bumping this re-reviews every open bug, which is
# the right thing when the notion of "materially changed" itself changes.
FINGERPRINT_VERSION = "fp/1"


def semantic_subset(bundle: EvidenceBundle) -> dict[str, Any]:
    """The facts that could plausibly change a recommendation.

    Returned as a plain structure so it can be inspected and diffed when a
    re-review fires unexpectedly -- "why did this run again" should be
    answerable by comparing two of these, not by reading the hash function.
    """
    bug = bundle.bug

    return {
        "version": FINGERPRINT_VERSION,
        "bug": {
            "id": bug.id,
            "title": " ".join(bug.title.split()),
            # The description by hash: its content matters, its formatting does not.
            "description": bug.description_sha,
            "tags": sorted(bug.tags),
            # Status transitions are how the team signals a decision.
            "tasks": sorted((t.target, t.status) for t in bug.tasks),
            # Comments by content hash, excluding index 0, which merely
            # restates the description and would otherwise count twice.
            "comments": sorted(c.content_sha for c in bug.comments if c.index > 0),
            "release_team_subscribed": bug.release_team_subscribed,
        },
        "subject": {
            "packages": sorted(bundle.subject.packages),
            "series": bundle.subject.target_series,
            "kind": bundle.subject.ffe_kind.value,
        },
        "release": _release_subset(bundle),
        "packages": [_package_subset(p) for p in sorted(bundle.packages, key=lambda p: p.name)],
        "testing": {
            # Evidence states, not the URLs: a developer re-uploading to the
            # same PPA does not change what we know about it.
            "ppa": bundle.testing.ppa.value,
            "build": bundle.testing.build.value,
            "autopkgtest": bundle.testing.autopkgtest.value,
            "test_output": bundle.testing.test_output.value,
            "corroborated": bundle.testing.corroborated,
        },
        "flavours": {
            "impact": bundle.flavours.impact.value,
            "affected": sorted(bundle.flavours.affected_flavours),
            # Who acknowledged, not when or in which comment.
            "acks": sorted((a.flavour, a.author) for a in bundle.flavours.acks),
            "ack_required_from": sorted(bundle.flavours.ack_required_from),
        },
        "injection": sorted({s.pattern_id for s in bundle.injection_signals}),
        # Only sources with no cached value at all. A stale-but-served source
        # has not changed what we know, and including it would let a flapping
        # upstream trigger re-reviews on its own.
        "unavailable": sorted({u.source_id for u in bundle.unavailable}),
    }


def _release_subset(bundle: EvidenceBundle) -> dict[str, Any]:
    fact = bundle.release_context
    if not fact.ok or fact.value is None:
        return {"status": fact.status.value}

    context = fact.value
    return {
        "series": context.series,
        "release_type": context.release_type.value,
        # Phase and bucket only. The raw day count would churn nightly.
        "phase": context.phase,
        "days_bucket": context.days_bucket,
    }


def _package_subset(package: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": package.name}

    archive = package.archive
    if archive.ok and archive.value is not None:
        out["archive"] = {
            "in_archive": archive.value.in_archive,
            "component": archive.value.component,
            # Version matters: a new upload is a different thing to review.
            "version": archive.value.version,
        }
    else:
        out["archive"] = {"status": archive.status.value}

    seeds = package.seeds
    if seeds.ok and seeds.value is not None:
        out["seeds"] = {
            "flavours": sorted(seeds.value.flavours),
            "is_core": seeds.value.is_core,
        }
    else:
        # UNAVAILABLE is itself material: it caps our confidence, and the
        # review should be redone once the source recovers.
        out["seeds"] = {"status": seeds.status.value}

    for key, fact in (("rdepends", package.rdepends), ("build_rdepends", package.build_rdepends)):
        if fact.ok and fact.value is not None:
            out[key] = {
                # Bucket, not count. 148 to 149 is not news.
                "bucket": fact.value.bucket,
                "seeded_rdeps": sorted(fact.value.seeded_rdeps),
            }
        elif fact.status is FactStatus.NOT_APPLICABLE:
            out[key] = {"status": "NOT_APPLICABLE"}
        else:
            out[key] = {"status": fact.status.value}

    return out


def fingerprint(bundle: EvidenceBundle) -> str:
    """Digest of everything that could change a recommendation."""
    return digest(semantic_subset(bundle))


def review_key(
    *,
    evidence_fingerprint: str,
    policy_hash: str,
    lessons_hash: str,
    risk_algorithm_version: str,
    harness_id: str,
    model: str,
) -> str:
    """The full key deciding whether the model needs to run.

    Wider than the evidence alone, because a recommendation can go stale
    without the bug changing: editing the rubric, learning a precedent, or
    switching model should all produce a fresh review of the open queue.
    """
    return digest(
        {
            "evidence": evidence_fingerprint,
            "policy": policy_hash,
            "lessons": lessons_hash,
            "risk": risk_algorithm_version,
            "harness": harness_id,
            "model": model,
        }
    )


def explain_change(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, ...]:
    """Name the top-level sections that differ between two subsets.

    So "why did this re-review" has an answer a human can read, rather than two
    hashes that happen not to match.
    """
    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return tuple(changed)
