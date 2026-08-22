"""Tests for the output contract.

Schema validation is the easy half. These mostly cover the semantic checks --
the ones standing between "the model said so" and "the record says so".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffe.errors import ContractError
from ffe.llm.contract import extract_json, repair_prompt, validate
from ffe.models import (
    Confidence,
    Decision,
    Gate,
    GateEffect,
    RiskAssessment,
)

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "assessment.v1.schema.json").read_text()
)

EVIDENCE = {
    "packages": [{"name": "curl", "rdepends": {"total": 711}}],
    "testing": {"ppa": "ABSENT"},
    "release_context": {"series": "stonking"},
    "bug": {"url": "https://bugs.launchpad.net/ubuntu/+source/curl/+bug/2167691"},
}


def _risk(**overrides: object) -> RiskAssessment:
    base: dict[str, object] = {"score": 50, "confidence_ceiling": Confidence.HIGH}
    base.update(overrides)
    return RiskAssessment(**base)  # type: ignore[arg-type]


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "decision": "APPROVE",
        "confidence": "HIGH",
        "summary": "Leaf package, well tested, early in the cycle.",
        "reasoning": [
            {"point": "curl has 711 reverse dependencies.", "evidence_refs": ["/packages"]}
        ],
    }
    base.update(overrides)
    return base


def _validate(payload: dict[str, object], risk: RiskAssessment | None = None):  # type: ignore[no-untyped-def]
    return validate(json.dumps(payload), schema=SCHEMA, evidence=EVIDENCE, risk=risk or _risk())


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_bare_json_is_extracted() -> None:
    assert extract_json('{"decision": "APPROVE"}')["decision"] == "APPROVE"


def test_fenced_json_is_extracted() -> None:
    """Models sometimes wrap output despite being asked not to."""
    text = 'Here is my review:\n```json\n{"decision": "REJECT"}\n```\nHope that helps.'
    assert extract_json(text)["decision"] == "REJECT"


def test_json_embedded_in_prose_is_extracted() -> None:
    assert extract_json('Sure. {"decision": "APPROVE"} Done.')["decision"] == "APPROVE"


def test_the_last_fenced_block_wins() -> None:
    """A model reasoning aloud may emit a draft before its real answer."""
    text = '```json\n{"decision": "APPROVE"}\n```\nOn reflection:\n```json\n{"decision": "REJECT"}\n```'
    assert extract_json(text)["decision"] == "REJECT"


@pytest.mark.parametrize("text", ["", "   ", "I cannot help with that.", "{not json}"])
def test_unextractable_responses_raise(text: str) -> None:
    with pytest.raises(ContractError):
        extract_json(text)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_a_good_response_validates() -> None:
    result = _validate(_payload())
    assert result.ok
    assert result.assessment is not None
    assert result.assessment.decision is Decision.APPROVE


def test_an_invented_decision_is_rejected() -> None:
    assert not _validate(_payload(decision="PROBABLY")).ok


def test_an_invented_flag_is_rejected() -> None:
    """The flag vocabulary is closed, so the dashboard cannot grow new states."""
    assert not _validate(_payload(flags=["SEEMS_FINE_TO_ME"])).ok


def test_unknown_fields_are_rejected() -> None:
    assert not _validate(_payload(secret_field="x")).ok


def test_an_overlong_summary_is_rejected() -> None:
    assert not _validate(_payload(summary="x" * 500)).ok


# --------------------------------------------------------------------------- #
# Citations -- the rule that keeps assertions tied to evidence
# --------------------------------------------------------------------------- #


def test_an_uncited_reasoning_point_is_rejected() -> None:
    result = _validate(_payload(reasoning=[{"point": "This seems fine to me."}]))
    assert not result.ok
    assert any("cites no evidence" in v for v in result.violations)


def test_a_citation_that_does_not_resolve_is_rejected() -> None:
    """The check that stops a plausible-looking pointer standing in for a fact."""
    result = _validate(
        _payload(
            reasoning=[
                {"point": "Tests all passed.", "evidence_refs": ["/testing/autopkgtest/results"]}
            ]
        )
    )
    assert not result.ok
    assert any("does not resolve" in v for v in result.violations)


def test_the_evidence_prefix_is_tolerated() -> None:
    """The prompt shows pointers as /evidence/..., which should still resolve."""
    assert _validate(
        _payload(reasoning=[{"point": "curl is central.", "evidence_refs": ["/evidence/packages"]}])
    ).ok


# --------------------------------------------------------------------------- #
# No invented links
# --------------------------------------------------------------------------- #


def test_a_url_not_in_the_evidence_is_rejected() -> None:
    """Blocks both fabricated citations and attacker-chosen links on the dashboard."""
    result = _validate(
        _payload(
            reasoning=[
                {
                    "point": "See https://evil.example/ppa for the build.",
                    "evidence_refs": ["/packages"],
                }
            ]
        )
    )
    assert not result.ok
    assert any("does not appear in the evidence" in v for v in result.violations)


def test_a_url_from_the_evidence_is_allowed() -> None:
    assert _validate(
        _payload(
            reasoning=[
                {
                    "point": "See https://bugs.launchpad.net/ubuntu/+source/curl/+bug/2167691",
                    "evidence_refs": ["/bug"],
                }
            ]
        )
    ).ok


# --------------------------------------------------------------------------- #
# Actionability
# --------------------------------------------------------------------------- #


def test_needs_information_without_specifics_is_rejected() -> None:
    result = _validate(_payload(decision="NEEDS_INFORMATION", missing_information=[]))
    assert not result.ok
    assert any("missing_information is empty" in v for v in result.violations)


def test_an_approval_listing_missing_information_is_rejected() -> None:
    """Either the information is needed or it is not; a reviewer cannot act on both."""
    result = _validate(_payload(decision="APPROVE", missing_information=["a PPA"]))
    assert not result.ok


def test_needs_information_with_specifics_is_accepted() -> None:
    assert _validate(
        _payload(decision="NEEDS_INFORMATION", missing_information=["A PPA build for stonking."])
    ).ok


# --------------------------------------------------------------------------- #
# Deterministic floors
# --------------------------------------------------------------------------- #


def test_overriding_a_floor_silently_is_rejected() -> None:
    """A model may disagree with the rules. It may not do so quietly."""
    risk = _risk(
        decision_floor=Decision.NEEDS_INFORMATION,
        gates=(Gate("no_testing_evidence", True, GateEffect.FLOOR_NEEDS_INFORMATION, "no PPA"),),
    )
    result = _validate(_payload(decision="APPROVE"), risk)

    assert not result.ok
    assert any("more permissive than the deterministic floor" in v for v in result.violations)


def test_overriding_a_floor_with_an_explanation_is_allowed() -> None:
    risk = _risk(decision_floor=Decision.NEEDS_INFORMATION)
    result = _validate(
        _payload(
            decision="APPROVE",
            disagreement_with_deterministic="A Debian sync is already built and tested there.",
        ),
        risk,
    )
    assert result.ok


def test_a_stricter_decision_than_the_floor_is_always_fine() -> None:
    risk = _risk(decision_floor=Decision.NEEDS_INFORMATION)
    assert _validate(_payload(decision="REJECT"), risk).ok


# --------------------------------------------------------------------------- #
# Confidence ceiling
# --------------------------------------------------------------------------- #


def test_confidence_is_clamped_not_rejected() -> None:
    """Overconfidence is a matter of degree; the recommendation may still be sound."""
    result = _validate(_payload(confidence="HIGH"), _risk(confidence_ceiling=Confidence.MEDIUM))

    assert result.ok
    assert result.assessment is not None
    assert result.assessment.confidence is Confidence.MEDIUM
    assert result.assessment.confidence_clamped_from is Confidence.HIGH
    assert result.clamped_confidence is True


def test_modest_confidence_is_left_alone() -> None:
    result = _validate(_payload(confidence="LOW"), _risk(confidence_ceiling=Confidence.HIGH))
    assert result.assessment is not None
    assert result.assessment.confidence is Confidence.LOW
    assert result.assessment.confidence_clamped_from is None


# --------------------------------------------------------------------------- #
# Hygiene and repair
# --------------------------------------------------------------------------- #


def test_control_characters_are_rejected() -> None:
    assert not _validate(_payload(summary="Looks fine‮reversed text here")).ok


def test_an_unknown_precedent_is_rejected() -> None:
    result = validate(
        json.dumps(_payload(precedents_applied=["L-does-not-exist"])),
        schema=SCHEMA,
        evidence=EVIDENCE,
        risk=_risk(),
        known_precedents=frozenset({"L1"}),
    )
    assert not result.ok
    assert any("was not supplied" in v for v in result.violations)


def test_repair_prompt_does_not_resend_the_evidence() -> None:
    """Keeps the cost down, and keeps untrusted text to one exposure per review."""
    prompt = repair_prompt(("reasoning[0] cites no evidence",), '{"decision": "APPROVE"}')

    assert "cites no evidence" in prompt
    assert "Do not restate the evidence" in prompt
    assert "711" not in prompt
    assert "curl" not in prompt
