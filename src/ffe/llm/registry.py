"""Selecting a harness from configuration.

The whole swappability requirement reduces to this function. Everything above
it -- the policy, the evidence, the contract -- is identical whichever harness
comes back.
"""

from __future__ import annotations

from ffe.config import Settings
from ffe.errors import ConfigError
from ffe.llm.base import LLMHarness
from ffe.llm.claude_code import ClaudeCodeHarness
from ffe.llm.null import NullHarness
from ffe.llm.openai_compat import OpenAICompatHarness
from ffe.llm.replay import ReplayHarness

HARNESSES = ("claude-code", "openai-compat", "null", "replay")


def build_harness(settings: Settings) -> LLMHarness:
    """Instantiate the configured harness."""
    name = settings.llm.harness.strip().lower()
    model = settings.llm.model.strip()

    if name == "claude-code":
        return ClaudeCodeHarness(model=model)
    if name == "openai-compat":
        return OpenAICompatHarness(model=model)
    if name == "null":
        return NullHarness()
    if name == "replay":
        return ReplayHarness()
    raise ConfigError(f"unknown LLM harness {name!r}; expected one of {', '.join(HARNESSES)}")
