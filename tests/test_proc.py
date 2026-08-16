"""Tests for subprocess execution with an enforced deadline.

This is the direct answer to the failure that motivated the design: the real
Ubuntu CLIs have no internal timeout and were observed hanging indefinitely.
"""

from __future__ import annotations

import time

import pytest

from ffe.errors import SourceTimeout
from ffe.util.proc import run, safe_env


def test_successful_command_captures_output() -> None:
    result = run(["echo", "seeded"], timeout=5)
    assert result.ok is True
    assert result.stdout.strip() == "seeded"
    assert result.returncode == 0


def test_failing_command_is_reported_not_raised() -> None:
    """A non-zero exit is data about the package, not an exception."""
    result = run(["false"], timeout=5)
    assert result.ok is False
    assert result.returncode != 0


def test_hanging_command_is_killed_at_the_deadline() -> None:
    """The property the whole design depends on: a hang cannot outlast its timeout."""
    started = time.monotonic()
    with pytest.raises(SourceTimeout, match="exceeded"):
        run(["sleep", "30"], timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"timeout was not enforced promptly (took {elapsed:.1f}s)"


def test_missing_binary_is_a_source_error() -> None:
    """A runner without ubuntu-dev-tools degrades, rather than crashing the run."""
    with pytest.raises(SourceTimeout, match="not installed"):
        run(["ffe-definitely-not-a-real-binary"], timeout=5)


def test_arguments_are_never_shell_interpreted() -> None:
    """Package names come from bug reports, so they must not reach a shell."""
    result = run(["echo", "; rm -rf /"], timeout=5)
    assert result.stdout.strip() == "; rm -rf /"


def test_environment_is_allowlisted() -> None:
    """A subprocess handling evidence must not see GITHUB_TOKEN or API keys."""
    env = safe_env()
    assert "GITHUB_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}


def test_extra_environment_can_be_passed_explicitly() -> None:
    assert safe_env({"FFE_TEST": "1"})["FFE_TEST"] == "1"


def test_command_is_rendered_for_provenance() -> None:
    """Provenance shows the exact command, quoted so it can be pasted and rerun."""
    result = run(["echo", "has space"], timeout=5)
    assert result.command == "echo 'has space'"


def test_stdin_is_forwarded() -> None:
    result = run(["cat"], timeout=5, stdin="piped input")
    assert result.stdout.strip() == "piped input"
