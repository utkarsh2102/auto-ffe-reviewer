"""Hard rules, applied before any model is consulted.

Gates exist because some conclusions do not need judgement. If a change to a
core package arrives with nothing anyone can check, the answer is "this is not
ready to review" whatever a model might say about how reasonable the request
sounds -- and the system should be able to say so without spending a single
token.

They constrain rather than decide. A gate can set a floor, meaning the
recommendation may not be *more permissive* than a given decision; it can cap
confidence; or it can simply flag something for a reviewer's attention. Nothing
here approves anything: there is no gate that can produce APPROVE, by design,
because that decision always belongs to a person.

Where a gate and the model disagree, the gate wins and the model is required to
say why it disagreed. That is checked in ffe.llm.contract.
"""

from __future__ import annotations

from ffe.models import (
    Confidence,
    Decision,
    FfeKind,
    FlavourImpact,
    Gate,
    GateEffect,
    RiskAssessment,
)
from ffe.risk.score import permissiveness
from ffe.risk.signals import Signals

# Kinds of change where missing test evidence is genuinely less alarming: a
# sync or merge has already been built and tested in Debian, and the archive
# will rebuild it before anything ships.
PREBUILT_ELSEWHERE = (FfeKind.SYNC, FfeKind.MERGE)


def evaluate(signals: Signals, assessment: RiskAssessment) -> RiskAssessment:
    """Apply every gate and fold the result into the assessment."""
    gates: list[Gate] = [
        _no_package_identified(signals),
        _no_testing_evidence(signals),
        _flavour_ack_outstanding(signals),
        _after_final_freeze(signals),
        _before_feature_freeze(signals),
        _unrecoverable_failure(signals),
        _injection_detected(signals),
        _evidence_unavailable(signals),
    ]
    triggered = [g for g in gates if g.triggered]

    floor: Decision | None = None
    for gate in triggered:
        if gate.effect is GateEffect.FLOOR_NEEDS_INFORMATION:
            floor = _stricter(floor, Decision.NEEDS_INFORMATION)
        elif gate.effect is GateEffect.FLOOR_REJECT:
            floor = _stricter(floor, Decision.REJECT)

    return RiskAssessment(
        algorithm_version=assessment.algorithm_version,
        score=assessment.score,
        band=assessment.band,
        components=assessment.components,
        gates=tuple(gates),
        decision_floor=floor,
        confidence_ceiling=_confidence_ceiling(signals),
    )


def _stricter(current: Decision | None, candidate: Decision) -> Decision:
    if current is None:
        return candidate
    return max(current, candidate, key=permissiveness)


def _confidence_ceiling(signals: Signals) -> Confidence:
    """How sure we are allowed to be, given what we could not check.

    A recommendation resting on two missing sources should not be presented
    with the same certainty as one resting on complete evidence, whatever the
    model's own sense of its reasoning.
    """
    missing = len(signals.unavailable_sources)
    if missing >= 2:
        return Confidence.LOW
    if missing == 1 or not signals.packages_known:
        return Confidence.MEDIUM
    # A core change with nothing to check is not something to be certain about.
    if signals.is_core and not signals.testing_corroborated:
        return Confidence.MEDIUM
    return Confidence.HIGH


# --------------------------------------------------------------------------- #
# Individual gates
# --------------------------------------------------------------------------- #


def _no_package_identified(signals: Signals) -> Gate:
    triggered = not signals.packages_known
    return Gate(
        id="no_package_identified",
        triggered=triggered,
        effect=GateEffect.FLOOR_NEEDS_INFORMATION,
        rationale=(
            "no source package is identified by any Launchpad task, so none of the "
            "package evidence could be gathered"
            if triggered
            else "the affected package is identified"
        ),
        evidence_refs=("/evidence/subject",),
    )


def _no_testing_evidence(signals: Signals) -> Gate:
    """The most consequential gate, and the most commonly hit.

    Deliberately softer for syncs and merges, which Debian has already built
    and tested, and for changes that reach nothing. Everywhere else, a request
    with nothing to check is not ready for a reviewer's time.
    """
    exempt = signals.ffe_kind in PREBUILT_ELSEWHERE
    trivial_reach = (
        signals.seeds_known
        and signals.rdeps_known
        and not signals.is_core
        and (signals.max_rdeps or 0) == 0
        and signals.flavour_impact is FlavourImpact.UNSEEDED
    )
    triggered = not signals.testing_corroborated and not exempt and not trivial_reach

    if triggered:
        rationale = (
            "nothing in the bug demonstrates the change works: no PPA, build log or test results"
        )
        if signals.unverified_claim_count:
            rationale += (
                f" ({signals.unverified_claim_count} claim(s) of testing are made in prose, "
                "but nothing is linked to verify)"
            )
    elif exempt:
        rationale = f"a {signals.ffe_kind.value} that Debian has already built and tested"
    elif trivial_reach:
        rationale = "unseeded with no reverse dependencies, so the change reaches nothing"
    else:
        rationale = "the bug links testing evidence"

    return Gate(
        id="no_testing_evidence",
        triggered=triggered,
        effect=GateEffect.FLOOR_NEEDS_INFORMATION,
        rationale=rationale,
        evidence_refs=("/evidence/testing",),
    )


def _flavour_ack_outstanding(signals: Signals) -> Gate:
    """A change across flavours needs the other flavours to have spoken.

    This flags rather than floors. The affected team may well be content, and a
    reviewer can often tell in seconds -- but nobody should have to discover
    for themselves that a second flavour was in scope.
    """
    triggered = bool(signals.ack_required_from)
    return Gate(
        id="flavour_ack_outstanding",
        triggered=triggered,
        effect=GateEffect.FLAG,
        rationale=(
            f"seeded on {len(signals.seeded_flavours)} flavours; no acknowledgement from "
            f"{', '.join(signals.ack_required_from)}"
            if triggered
            else "no cross-flavour acknowledgement is outstanding"
        ),
        evidence_refs=("/evidence/flavours",),
    )


def _after_final_freeze(signals: Signals) -> Gate:
    triggered = signals.after_final_freeze
    return Gate(
        id="after_final_freeze",
        triggered=triggered,
        effect=GateEffect.FLOOR_NEEDS_INFORMATION,
        rationale=(
            f"the request is past {signals.phase.replace('_', ' ').lower()}, when only "
            "release-critical fixes are normally considered"
            if triggered
            else "the request is within the ordinary freeze window"
        ),
        evidence_refs=("/evidence/release_context",),
    )


def _before_feature_freeze(signals: Signals) -> Gate:
    """No exception is needed before Feature Freeze."""
    triggered = signals.before_feature_freeze
    return Gate(
        id="before_feature_freeze",
        triggered=triggered,
        effect=GateEffect.FLAG,
        rationale=(
            "the target release has not reached Feature Freeze, so an exception may not be needed at all"
            if triggered
            else "the target release is past Feature Freeze"
        ),
        evidence_refs=("/evidence/release_context",),
    )


def _unrecoverable_failure(signals: Signals) -> Gate:
    """Some failures cannot be fixed after release.

    An SRU can reach most regressions. It cannot reach a machine that will not
    boot, or an image that will not install -- the fix has no way in. Flagged
    rather than floored, because the change may still be right and the team may
    still want it; they should simply know which kind of risk they are taking.
    """
    triggered = bool(signals.unrecoverable_packages)
    return Gate(
        id="unrecoverable_failure_mode",
        triggered=triggered,
        effect=GateEffect.FLAG,
        rationale=(
            f"{', '.join(signals.unrecoverable_packages)}: a regression here can prevent "
            "booting or installing, which no SRU can reach"
            if triggered
            else "a regression here would be fixable by an SRU"
        ),
        evidence_refs=("/evidence/subject",),
    )


def _injection_detected(signals: Signals) -> Gate:
    """Report, never act. See ffe.evidence.injection."""
    triggered = signals.injection_signal_count > 0
    return Gate(
        id="suspicious_bug_text",
        triggered=triggered,
        effect=GateEffect.FLAG,
        rationale=(
            f"{signals.injection_signal_count} passage(s) in the bug resemble attempts to steer an "
            "automated reviewer; shown verbatim for a human to judge"
            if triggered
            else "nothing in the bug text resembles an attempt to steer the reviewer"
        ),
        evidence_refs=("/evidence/injection",),
    )


def _evidence_unavailable(signals: Signals) -> Gate:
    triggered = bool(signals.unavailable_sources)
    return Gate(
        id="evidence_unavailable",
        triggered=triggered,
        effect=GateEffect.CAP_CONFIDENCE,
        rationale=(
            f"could not consult {', '.join(signals.unavailable_sources)}, so this assessment "
            "rests on incomplete evidence"
            if triggered
            else "every evidence source was available"
        ),
        evidence_refs=("/evidence/unavailable",),
    )
