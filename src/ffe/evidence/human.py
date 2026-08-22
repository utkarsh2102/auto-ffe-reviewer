"""Detecting what the Release Team actually decided.

The team's convention makes this exact rather than a matter of interpretation.
A developer subscribes ~ubuntu-release to their FFe bug; the team reviews it,
sets a status, and unsubscribes. So a bug leaving the queue *is* the decision
being made, and the status says which way it went:

    Triaged (then Fix Committed, Fix Released)  ->  approved
    Incomplete                                  ->  more information wanted
    Won't Fix, Invalid, Opinion                 ->  rejected

No comment parsing, no guessing at phrasing. Which matters: the same bug's
comments included "FFe approved" from a Release Team member and, two days
later, a reopening over an FTBFS. A regex would have had to adjudicate between
them; the status and the subscription simply say where things stand.

Once a decision is detected the review stops. The record is frozen, the bug
drops off the active dashboard, and no further tokens are spent on a question
that has been answered. What the record keeps is the pairing of our last
recommendation with the decision -- which is the ground truth the learning loop
runs on, and it costs nothing to collect.
"""

from __future__ import annotations

from dataclasses import dataclass

from ffe.models import BugFacts, Decision, HumanDecision, HumanOutcome
from ffe.sources.launchpad import STATUS_TO_DECISION
from ffe.util.clock import utc_iso

# Statuses that mean nobody has ruled yet. Listed positively so an unfamiliar
# status is treated as undecided rather than silently read as an approval.
UNDECIDED_STATUSES = frozenset({"New", "Confirmed", "Unknown", ""})


def decision_from_status(status: str) -> HumanDecision | None:
    """Map a Launchpad bug status to a Release Team decision, if it implies one."""
    if status in UNDECIDED_STATUSES:
        return None
    return STATUS_TO_DECISION.get(status)


def primary_status(bug: BugFacts) -> str:
    """The status that best represents the bug's state.

    A multi-package FFe has a task per package and they can disagree -- the
    curl/upki bug had both at Fix Committed, but a partly-landed change may
    not. The least-settled task wins, so a request is not reported as decided
    while any part of it is still open.
    """
    if not bug.tasks:
        return ""

    ranked = sorted(
        bug.tasks,
        key=lambda t: (
            0 if t.status in UNDECIDED_STATUSES else 1,
            0 if decision_from_status(t.status) is HumanDecision.NEEDS_INFORMATION else 1,
            0 if decision_from_status(t.status) is HumanDecision.REJECTED else 1,
        ),
    )
    return ranked[0].status


@dataclass(frozen=True, slots=True)
class QueueState:
    """Whether a bug is still awaiting review, and what has been decided."""

    in_queue: bool
    outcome: HumanOutcome


def assess(bug: BugFacts, *, in_queue: bool, left_queue_at: str | None = None) -> QueueState:
    """Work out whether the team has ruled on this bug.

    Subscription is the stronger signal and comes first. A bug still in the
    queue is still being worked on, whatever its status says -- a developer
    setting their own bug to Fix Committed has not thereby approved it.
    """
    status = primary_status(bug)

    if in_queue:
        return QueueState(in_queue=True, outcome=HumanOutcome(decided=False, final_status=status))

    decision = decision_from_status(status)
    if decision is None:
        # Off the queue but with nothing conclusive on it -- an unsubscribe by
        # hand, or a status we do not recognise. Not a decision we should claim.
        return QueueState(
            in_queue=False,
            outcome=HumanOutcome(decided=False, final_status=status),
        )

    return QueueState(
        in_queue=False,
        outcome=HumanOutcome(
            decided=True,
            decision=decision,
            final_status=status,
            detected_at=utc_iso(),
            left_queue_at=left_queue_at,
        ),
    )


# Our recommendation and the team's decision use different vocabularies --
# Decision.APPROVE against HumanDecision.APPROVED -- because one is advice and
# the other is what happened. Comparing them needs this mapping rather than a
# string equality that would silently read every agreement as a disagreement.
_EQUIVALENT: dict[Decision, HumanDecision] = {
    Decision.APPROVE: HumanDecision.APPROVED,
    Decision.REJECT: HumanDecision.REJECTED,
    Decision.NEEDS_INFORMATION: HumanDecision.NEEDS_INFORMATION,
}


def agrees(ours: Decision | str | None, theirs: HumanDecision | None) -> bool | None:
    """Whether our recommendation matched the decision. None if either is absent.

    NEEDS_INFORMATION is treated as its own answer rather than as a near-miss
    for either side: recommending more information when the team approved is a
    real disagreement, and the point of the learning loop is to find out why.
    """
    if ours is None or theirs is None:
        return None
    try:
        recommendation = Decision(ours)
    except ValueError:
        return None
    return _EQUIVALENT[recommendation] is theirs
