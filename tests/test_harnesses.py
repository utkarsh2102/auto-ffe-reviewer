"""Tests for the harness abstraction.

The property under test is the one the design turns on: switching model backend
is configuration, and no review logic moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffe.config import load_settings
from ffe.errors import ConfigError
from ffe.llm.base import HarnessError, LLMRequest
from ffe.llm.claude_code import ClaudeCodeHarness, _unwrap
from ffe.llm.null import NullHarness
from ffe.llm.openai_compat import OpenAICompatHarness
from ffe.llm.registry import build_harness
from ffe.llm.replay import ReplayHarness

CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


def _request() -> LLMRequest:
    return LLMRequest(system="policy", user="evidence", timeout_s=5)


# --------------------------------------------------------------------------- #
# Selection is configuration
# --------------------------------------------------------------------------- #


def test_default_harness_is_claude_code() -> None:
    harness = build_harness(load_settings(CONFIG, env={}))
    assert harness.describe().id == "claude-code"


def test_switching_to_deepseek_needs_no_code_change() -> None:
    """The whole swappability requirement, in one assertion."""
    settings = load_settings(
        CONFIG, env={"FFE_LLM_HARNESS": "openai-compat", "FFE_LLM_MODEL": "deepseek-chat"}
    )
    info = build_harness(settings).describe()

    assert info.id == "openai-compat"
    assert info.model == "deepseek-chat"


def test_unknown_harness_is_a_clear_error() -> None:
    settings = load_settings(CONFIG, env={"FFE_LLM_HARNESS": "telepathy"})
    with pytest.raises(ConfigError, match="unknown LLM harness"):
        build_harness(settings)


def test_every_harness_implements_the_same_interface() -> None:
    for name in ("claude-code", "openai-compat", "null", "replay"):
        harness = build_harness(load_settings(CONFIG, env={"FFE_LLM_HARNESS": name}))
        info = harness.describe()
        assert info.id
        assert isinstance(info.supports_structured_output, bool)
        assert callable(harness.complete)


# --------------------------------------------------------------------------- #
# Null: the pipeline must work without a model
# --------------------------------------------------------------------------- #


def test_null_harness_refuses_clearly() -> None:
    with pytest.raises(HarnessError, match="no language model"):
        NullHarness().complete(_request())


# --------------------------------------------------------------------------- #
# Replay: deterministic end-to-end testing
# --------------------------------------------------------------------------- #


def test_replay_returns_queued_responses_in_order() -> None:
    harness = ReplayHarness(responses=['{"decision": "APPROVE"}', '{"decision": "REJECT"}'])
    assert "APPROVE" in harness.complete(_request()).text
    assert "REJECT" in harness.complete(_request()).text


def test_replay_records_what_it_was_asked() -> None:
    """So tests can assert on the prompt, not just the answer."""
    harness = ReplayHarness(responses=["{}"])
    harness.complete(LLMRequest(system="the policy", user="the evidence"))

    assert harness.requests[0].system == "the policy"
    assert harness.requests[0].user == "the evidence"


def test_replay_exhaustion_is_an_error_not_a_silent_empty() -> None:
    with pytest.raises(HarnessError, match="no recorded responses"):
        ReplayHarness().complete(_request())


def test_replay_loads_from_files(tmp_path: Path) -> None:
    path = tmp_path / "response.json"
    path.write_text('{"decision": "NEEDS_INFORMATION"}')
    assert "NEEDS_INFORMATION" in ReplayHarness.from_files(path).complete(_request()).text


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #


def test_claude_code_unwraps_the_json_envelope() -> None:
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": '{"decision": "APPROVE"}',
            "usage": {"input_tokens": 9000, "output_tokens": 300},
        }
    )
    text, usage, stop = _unwrap(envelope)

    assert text == '{"decision": "APPROVE"}'
    assert usage == {"input_tokens": 9000, "output_tokens": 300}
    assert stop == "success"


def test_claude_code_reports_a_cli_error() -> None:
    envelope = json.dumps({"is_error": True, "result": "Credit balance too low"})
    with pytest.raises(HarnessError, match="Credit balance"):
        _unwrap(envelope)


def test_unexpected_envelope_shape_degrades_to_raw_text() -> None:
    """A change in the CLI's output shape should not be a hard failure.

    The contract layer will try to parse whatever comes back, which is a better
    outcome than refusing to look at it.
    """
    text, usage, _stop = _unwrap("just some text, not an envelope")
    assert text == "just some text, not an envelope"
    assert usage == {}


def test_missing_cli_is_reported_clearly() -> None:
    harness = ClaudeCodeHarness(executable="claude-definitely-not-installed")
    with pytest.raises(HarnessError, match="not on PATH"):
        harness.complete(_request())


def test_claude_code_declares_no_native_schema_enforcement() -> None:
    """So the contract layer's extraction path is exercised by the default harness."""
    assert ClaudeCodeHarness().describe().supports_structured_output is False


# --------------------------------------------------------------------------- #
# OpenAI-compatible
# --------------------------------------------------------------------------- #


def test_structured_output_support_is_inferred_from_the_endpoint() -> None:
    """The contract branches on capability, never on vendor."""
    assert (
        OpenAICompatHarness(base_url="https://api.deepseek.com/v1")
        .describe()
        .supports_structured_output
    )
    assert (
        not OpenAICompatHarness(base_url="http://localhost:11434/v1")
        .describe()
        .supports_structured_output
    )


def test_structured_output_support_can_be_declared_explicitly() -> None:
    harness = OpenAICompatHarness(base_url="http://localhost:11434/v1", structured_output=True)
    assert harness.describe().supports_structured_output is True


def test_missing_api_key_is_reported_before_any_request() -> None:
    harness = OpenAICompatHarness(model="deepseek-chat", base_url="https://api.deepseek.com/v1")
    with pytest.raises(HarnessError, match="no API key"):
        harness.complete(_request())


def test_missing_base_url_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FFE_LLM_API_KEY", "x")
    monkeypatch.delenv("FFE_LLM_BASE_URL", raising=False)
    with pytest.raises(HarnessError, match="no base URL"):
        OpenAICompatHarness(model="m").complete(_request())
