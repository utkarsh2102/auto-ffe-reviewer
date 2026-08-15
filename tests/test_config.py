"""Tests for settings loading and environment overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffe.config import load_settings
from ffe.errors import ConfigError

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


def test_defaults_work_without_a_config_file() -> None:
    """A bare checkout must be usable; the dataclass defaults are complete."""
    settings = load_settings(Path("/nonexistent/config.toml"), env={})
    assert settings.llm.harness == "claude-code"
    assert settings.launchpad.release_team == "ubuntu-release"
    assert settings.timeouts.connect > 0


def test_repo_config_parses_and_matches_expectations() -> None:
    settings = load_settings(REPO_CONFIG, env={})
    assert settings.launchpad.distribution == "ubuntu"
    assert settings.launchpad.release_team == "ubuntu-release"
    # Claude Code is the default harness, per the design decision to iterate on
    # the review policy with the same tool used in CI.
    assert settings.llm.harness == "claude-code"
    assert settings.learning.enabled is True


def test_toml_lists_become_tuples_so_settings_stay_hashable() -> None:
    settings = load_settings(REPO_CONFIG, env={})
    assert isinstance(settings.launchpad.open_statuses, tuple)
    assert "Triaged" in settings.launchpad.open_statuses


def test_api_root_follows_the_instance() -> None:
    production = load_settings(REPO_CONFIG, env={})
    assert production.launchpad.api_root == "https://api.launchpad.net/devel"

    staging = load_settings(REPO_CONFIG, env={"FFE_LAUNCHPAD_INSTANCE": "qastaging"})
    assert staging.launchpad.api_root == "https://api.qastaging.launchpad.net/devel"


@pytest.mark.parametrize(
    ("env", "attr_path", "expected"),
    [
        ({"FFE_LLM_HARNESS": "openai-compat"}, ("llm", "harness"), "openai-compat"),
        ({"FFE_LLM_MODEL": "deepseek-chat"}, ("llm", "model"), "deepseek-chat"),
        ({"FFE_TIMEOUTS_READ": "99"}, ("timeouts", "read"), 99),
        ({"FFE_LLM_TEMPERATURE": "0.3"}, ("llm", "temperature"), 0.3),
        ({"FFE_LEARNING_ENABLED": "false"}, ("learning", "enabled"), False),
        ({"FFE_LEARNING_ENABLED": "1"}, ("learning", "enabled"), True),
    ],
)
def test_environment_overrides_are_typed(
    env: dict[str, str], attr_path: tuple[str, str], expected: object
) -> None:
    """Overrides are coerced to the type of the default they replace."""
    settings = load_settings(REPO_CONFIG, env=env)
    section, key = attr_path
    assert getattr(getattr(settings, section), key) == expected


def test_switching_harness_needs_no_code_change() -> None:
    """The whole point of the abstraction: model choice is configuration."""
    deepseek = load_settings(
        REPO_CONFIG,
        env={"FFE_LLM_HARNESS": "openai-compat", "FFE_LLM_MODEL": "deepseek-reasoner"},
    )
    assert (deepseek.llm.harness, deepseek.llm.model) == ("openai-compat", "deepseek-reasoner")


def test_bad_numeric_override_is_reported_clearly() -> None:
    with pytest.raises(ConfigError, match="FFE_TIMEOUTS_READ"):
        load_settings(REPO_CONFIG, env={"FFE_TIMEOUTS_READ": "not-a-number"})


def test_malformed_toml_is_reported_with_the_path(tmp_path: Path) -> None:
    broken = tmp_path / "config.toml"
    broken.write_text("this is not = = valid toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(broken, env={})


def test_offline_can_be_set_by_flag_or_environment() -> None:
    assert load_settings(REPO_CONFIG, env={}, offline=True).offline is True
    assert load_settings(REPO_CONFIG, env={"FFE_OFFLINE": "yes"}).offline is True
    assert load_settings(REPO_CONFIG, env={}).offline is False
