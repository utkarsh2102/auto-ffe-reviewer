"""A harness that refuses to answer.

Backs `--no-llm`, and is the default in tests. Its existence is a design
statement: the pipeline must produce a useful record with no model involved,
and running the whole suite against this harness is what proves it still does.
"""

from __future__ import annotations

from dataclasses import dataclass

from ffe.llm.base import HarnessError, HarnessInfo, LLMRequest, LLMResponse

HARNESS_ID = "null"


@dataclass
class NullHarness:
    def describe(self) -> HarnessInfo:
        return HarnessInfo(id=HARNESS_ID, model="none", supports_structured_output=False)

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise HarnessError(HARNESS_ID, "no language model is configured")
