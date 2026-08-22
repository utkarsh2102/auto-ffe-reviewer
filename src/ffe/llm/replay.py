"""A harness that replays recorded responses.

Used by the golden scenarios so review behaviour can be tested end to end
without a network or an API key -- including the responses no real model would
reliably produce on demand, such as malformed JSON, an uncited claim, or an
attempt to override a deterministic floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ffe.llm.base import HarnessError, HarnessInfo, LLMRequest, LLMResponse

HARNESS_ID = "replay"


@dataclass
class ReplayHarness:
    """Returns queued responses in order."""

    responses: list[str] = field(default_factory=list)
    model: str = "replay"
    requests: list[LLMRequest] = field(default_factory=list)

    @classmethod
    def from_files(cls, *paths: Path) -> ReplayHarness:
        return cls(responses=[Path(p).read_text(encoding="utf-8") for p in paths])

    def describe(self) -> HarnessInfo:
        return HarnessInfo(id=HARNESS_ID, model=self.model, supports_structured_output=False)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self.responses:
            raise HarnessError(HARNESS_ID, "no recorded responses remain")
        return LLMResponse(
            text=self.responses.pop(0),
            model=self.model,
            harness=HARNESS_ID,
            raw="",
        )
