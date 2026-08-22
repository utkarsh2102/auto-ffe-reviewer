"""The harness abstraction.

A harness takes a system prompt, a user message and a schema, and returns text.
That is the entire interface, and it is deliberately narrow: nothing here knows
what an FFe is, what the criteria are, or how to read the result. The review
methodology lives in policy/ and the validation lives in contract.py.

The consequence is the one the design needs: moving from Claude Code to
OpenCode with DeepSeek is a configuration change. No review logic moves,
because none of it is here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HarnessInfo:
    """What a harness can do, so the contract layer can adapt.

    `supports_structured_output` is the one that matters: where a backend can
    enforce a JSON schema itself, we let it, and where it cannot we fall back
    to extraction. The contract branches on the capability, never on the
    vendor, which is what keeps adding a backend from touching that code.
    """

    id: str
    model: str
    supports_structured_output: bool = False
    supports_system_prompt: bool = True
    max_output_tokens: int = 2000


@dataclass(frozen=True, slots=True)
class LLMRequest:
    system: str  # trusted: policy, precedents, schema
    user: str  # evidence, with any bug text nonce-fenced
    response_schema: dict[str, object] | None = None
    max_output_tokens: int = 2000
    temperature: float = 0.0
    timeout_s: int = 300
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    harness: str
    stop_reason: str = ""
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    raw: str = ""  # the untouched response, retained for provenance


class LLMHarness(Protocol):
    """Every backend implements exactly this."""

    def describe(self) -> HarnessInfo: ...

    def complete(self, request: LLMRequest) -> LLMResponse: ...


class HarnessError(RuntimeError):
    """A harness could not produce a response.

    Distinct from a response that fails validation: this one means we never got
    an answer, which is recorded as LLM_UNAVAILABLE rather than as a bad review.
    """

    def __init__(self, harness: str, reason: str) -> None:
        super().__init__(f"{harness}: {reason}")
        self.harness = harness
        self.reason = reason
