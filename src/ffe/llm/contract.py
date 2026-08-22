"""Getting a trustworthy structure out of a probabilistic system.

Schema validation is the easy half. The half that matters is semantic: a
response can be perfectly well-formed JSON, validate against the schema, and
still assert something the evidence does not support. These checks are what
stand between "the model said so" and "the record says so".

The rules enforced here are the ones a Release Team member would otherwise
have to check by hand, every time:

- every reasoning point cites evidence that actually exists
- no URL appears that was not in the evidence
- confidence does not exceed what the available evidence supports
- a deterministic floor is not quietly overridden
- NEEDS_INFORMATION comes with something actionable, and nothing else does

A response failing these is not patched up. It is retried once or twice with
the errors, and then abandoned -- the record is published with no assessment,
which is honest, rather than with a coerced one, which is not.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import jsonschema

from ffe.errors import ContractError
from ffe.models import (
    Assessment,
    Confidence,
    Decision,
    ReasoningItem,
    RiskAssessment,
    clamp_confidence,
    to_jsonable,
)
from ffe.risk.score import permissiveness

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f‪-‮⁦-⁩]")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    assessment: Assessment | None
    violations: tuple[str, ...] = ()
    clamped_confidence: bool = False

    @property
    def ok(self) -> bool:
        return self.assessment is not None


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def extract_json(text: str) -> dict[str, Any]:
    """Pull one JSON object out of a model response.

    Tried in order of trustworthiness: the whole response, then a fenced block,
    then the outermost balanced braces. Models are asked for bare JSON and
    mostly comply, but a stray sentence of preamble should not throw away an
    otherwise good review.
    """
    stripped = text.strip()
    if not stripped:
        raise ContractError("model returned an empty response")

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = _FENCED_JSON.findall(stripped)
    if fenced:
        try:
            parsed = json.loads(fenced[-1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ContractError("no JSON object could be extracted from the response")


# --------------------------------------------------------------------------- #
# Semantic checks
# --------------------------------------------------------------------------- #


def _resolve_pointer(document: Any, pointer: str) -> bool:
    """Whether an RFC 6901 JSON pointer resolves in the evidence.

    A prefix match counts: /evidence/packages is a reasonable citation even
    though packages is a list, and demanding an exact leaf would reject
    perfectly good references.
    """
    if not pointer.startswith("/"):
        return False

    current = document
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            # Tolerate the leading "evidence/" that the prompt uses for
            # readability even though the bundle is passed as the root.
            if token in current:
                current = current[token]
                continue
            if token == "evidence":
                continue
            return False
        if isinstance(current, list):
            if token.isdigit() and int(token) < len(current):
                current = current[int(token)]
                continue
            return False
        return False
    return True


def _evidence_urls(document: Any) -> set[str]:
    return set(_URL.findall(json.dumps(document)))


def check_semantics(
    payload: dict[str, Any],
    *,
    evidence: Any,
    risk: RiskAssessment,
    known_precedents: frozenset[str] = frozenset(),
) -> list[str]:
    """Every rule the schema cannot express. Returns violations, empty if clean."""
    violations: list[str] = []
    decision = Decision(payload["decision"])
    missing = payload.get("missing_information") or []

    # Actionability: NEEDS_INFORMATION must say what is needed, and the other
    # decisions must not, since a reviewer would not know what to do with a
    # list of gaps attached to an approval.
    if decision is Decision.NEEDS_INFORMATION and not missing:
        violations.append(
            "decision is NEEDS_INFORMATION but missing_information is empty; "
            "say specifically what would unblock the review"
        )
    if decision is not Decision.NEEDS_INFORMATION and missing:
        violations.append(
            f"decision is {decision.value} but missing_information is non-empty; "
            "either the information is needed, in which case say NEEDS_INFORMATION, or it is not"
        )

    # Citations. The rule that stops assertions floating free of the evidence.
    evidence_doc = to_jsonable(evidence)
    for index, item in enumerate(payload.get("reasoning", [])):
        refs = item.get("evidence_refs") or []
        if not refs:
            violations.append(
                f"reasoning[{index}] cites no evidence; every point must cite what it rests on"
            )
            continue
        for ref in refs:
            if not _resolve_pointer(evidence_doc, ref):
                violations.append(
                    f"reasoning[{index}] cites {ref!r}, which does not resolve in the evidence"
                )

    # No invented links. Blocks both fabricated citations and anything that
    # would put an attacker-chosen URL onto the dashboard.
    allowed = _evidence_urls(evidence_doc)
    for url in _URL.findall(json.dumps(payload)):
        if url.rstrip(".,);") not in {a.rstrip(".,);") for a in allowed}:
            violations.append(f"output contains {url!r}, which does not appear in the evidence")

    # Deterministic floors hold. Disagreement is allowed, silence is not.
    floor = risk.decision_floor
    overrides_floor = floor is not None and permissiveness(decision) < permissiveness(floor)
    if overrides_floor and not (payload.get("disagreement_with_deterministic") or "").strip():
        violations.append(
            f"decision {decision.value} is more permissive than the deterministic floor "
            f"{floor.value if floor else ''}; if you disagree, say why in "
            "disagreement_with_deterministic"
        )

    for precedent in payload.get("precedents_applied", []):
        if known_precedents and precedent not in known_precedents:
            violations.append(f"precedents_applied names {precedent!r}, which was not supplied")

    # Text hygiene, since these strings are rendered on a shared dashboard.
    for field_name, value in payload.items():
        if isinstance(value, str) and _CONTROL.search(value):
            violations.append(f"{field_name} contains control or bidirectional characters")

    return violations


# --------------------------------------------------------------------------- #
# The whole pipeline
# --------------------------------------------------------------------------- #


def validate(
    text: str,
    *,
    schema: dict[str, Any],
    evidence: Any,
    risk: RiskAssessment,
    known_precedents: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Extract, schema-check, semantically check, and clamp confidence."""
    try:
        payload = extract_json(text)
    except ContractError as exc:
        return ValidationResult(assessment=None, violations=(str(exc),))

    schema_errors = [
        f"{'/'.join(str(p) for p in error.path) or '(root)'}: {error.message}"
        for error in jsonschema.Draft202012Validator(schema).iter_errors(payload)
    ]
    if schema_errors:
        return ValidationResult(assessment=None, violations=tuple(schema_errors))

    violations = check_semantics(
        payload, evidence=evidence, risk=risk, known_precedents=known_precedents
    )
    if violations:
        return ValidationResult(assessment=None, violations=tuple(violations))

    # Confidence is clamped rather than rejected. Overconfidence is a matter of
    # degree, the recommendation itself may be perfectly sound, and the clamp
    # is recorded so the dashboard can show that it happened.
    claimed = Confidence(payload["confidence"])
    allowed = clamp_confidence(claimed, risk.confidence_ceiling)

    return ValidationResult(
        assessment=Assessment(
            decision=Decision(payload["decision"]),
            confidence=allowed,
            summary=payload["summary"].strip(),
            reasoning=tuple(
                ReasoningItem(
                    point=item["point"].strip(),
                    evidence_refs=tuple(item.get("evidence_refs") or ()),
                )
                for item in payload["reasoning"]
            ),
            missing_information=tuple(payload.get("missing_information") or ()),
            conditions=tuple(payload.get("conditions") or ()),
            flags=tuple(payload.get("flags") or ()),
            precedents_applied=tuple(payload.get("precedents_applied") or ()),
            disagreement_with_deterministic=payload.get("disagreement_with_deterministic") or None,
            confidence_clamped_from=claimed if allowed is not claimed else None,
        ),
        clamped_confidence=allowed is not claimed,
    )


def repair_prompt(violations: tuple[str, ...], previous: str) -> str:
    """Ask for a correction, without resending anything.

    Deliberately terse. The evidence is not repeated -- it is already in the
    conversation's system prompt and resending it doubles the cost -- and the
    untrusted bug text is never sent twice, which keeps the injection surface
    to a single exposure per review.
    """
    listed = "\n".join(f"- {v}" for v in violations)
    return (
        "Your previous response did not satisfy the output contract.\n\n"
        f"Problems:\n{listed}\n\n"
        "Previous response:\n"
        f"{previous[:4000]}\n\n"
        "Return one corrected JSON object. Do not restate the evidence."
    )
