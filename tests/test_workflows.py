"""Tests for the GitHub Actions workflows.

Workflow mistakes are expensive to find in production -- a scheduled job fails
at 03:00 on a Sunday, or a secret reaches a context it should not. These check
the properties that are easy to break by accident and hard to notice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["ci.yml", "review.yml"])
def test_workflows_are_valid_yaml(name: str) -> None:
    assert _load(name)["jobs"]


# --------------------------------------------------------------------------- #
# CI
# --------------------------------------------------------------------------- #


def test_ci_runs_on_fork_pull_requests() -> None:
    """A contributor should get the same signal without being granted access."""
    workflow = _load("ci.yml")
    # `on` parses as the boolean True in YAML 1.1, which is a classic trap.
    triggers = workflow.get("on") or workflow.get(True)
    assert "pull_request" in triggers


def test_ci_needs_no_secrets() -> None:
    """The suite blocks sockets and needs no API key; CI must reflect that."""
    raw = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "secrets." not in raw


def test_ci_is_read_only() -> None:
    assert _load("ci.yml")["permissions"] == {"contents": "read"}


def test_ci_enforces_the_policy_version() -> None:
    """An unrecorded policy edit would silently re-review the whole queue."""
    steps = _load("ci.yml")["jobs"]["test"]["steps"]
    assert any("policy" in str(step.get("name", "")).lower() for step in steps)


def test_ci_runs_lint_types_and_tests() -> None:
    run = " ".join(str(step.get("run", "")) for step in _load("ci.yml")["jobs"]["test"]["steps"])
    assert "ruff check" in run
    assert "mypy" in run
    assert "pytest" in run


# --------------------------------------------------------------------------- #
# The review run
# --------------------------------------------------------------------------- #


def test_review_is_scheduled_and_can_be_triggered_by_hand() -> None:
    triggers = _load("review.yml").get("on") or _load("review.yml").get(True)
    assert "schedule" in triggers
    # GitHub's scheduler skews and drops runs, so manual dispatch is not
    # optional -- it is how a Release Team member gets an answer now.
    assert "workflow_dispatch" in triggers


def test_review_does_not_run_on_pull_requests() -> None:
    """It holds an API key and writes a branch; neither belongs on a fork PR."""
    triggers = _load("review.yml").get("on") or _load("review.yml").get(True)
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers


def test_overlapping_runs_are_queued_not_cancelled() -> None:
    """A cancelled run could leave the state tree half-written."""
    concurrency = _load("review.yml")["concurrency"]
    assert concurrency["cancel-in-progress"] is False


def test_review_has_a_timeout() -> None:
    """Without one, a hung job occupies a runner for six hours."""
    assert _load("review.yml")["jobs"]["review"]["timeout-minutes"] <= 60


def test_bot_output_goes_to_a_separate_branch() -> None:
    """Keeping it off main means code PRs never conflict with review data."""
    raw = (WORKFLOWS / "review.yml").read_text(encoding="utf-8")
    assert "STATE_BRANCH: state" in raw
    assert "HEAD:$STATE_BRANCH" in raw


def test_state_push_retries_rather_than_failing() -> None:
    """The scheduler can overlap runs, and state is append-mostly."""
    raw = (WORKFLOWS / "review.yml").read_text(encoding="utf-8")
    assert "git rebase" in raw
    assert "for attempt in" in raw


def test_the_api_key_reaches_only_the_review_step() -> None:
    """Every other step runs without it, including the one that pushes."""
    review = _load("review.yml")["jobs"]["review"]
    with_key = [step for step in review["steps"] if "ANTHROPIC_API_KEY" in str(step.get("env", {}))]
    assert len(with_key) == 1
    assert "ffe run" in str(with_key[0]["run"])


def test_missing_api_key_degrades_to_evidence_only() -> None:
    """A repository without a key still gets a useful dashboard."""
    raw = (WORKFLOWS / "review.yml").read_text(encoding="utf-8")
    assert "!secrets.ANTHROPIC_API_KEY" in raw
    assert "--no-llm" in raw


def test_ubuntu_tooling_is_installed() -> None:
    raw = (WORKFLOWS / "review.yml").read_text(encoding="utf-8")
    assert "distro-info-data" in raw
    assert "ubuntu-dev-tools" in raw


def test_the_run_never_writes_to_launchpad() -> None:
    """v1 is read-only against Launchpad, and nothing here should suggest otherwise."""
    raw = (WORKFLOWS / "review.yml").read_text(encoding="utf-8")
    assert "LP_CREDENTIALS" not in raw
    assert "launchpadlib" not in raw


def test_pages_permissions_are_scoped_to_the_deploy_job() -> None:
    workflow = _load("review.yml")
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}


def test_the_dashboard_is_deployed_from_the_state_tree() -> None:
    steps = _load("review.yml")["jobs"]["review"]["steps"]
    upload = next(s for s in steps if "upload-pages-artifact" in str(s.get("uses", "")))
    assert upload["with"]["path"] == ".ffe-state/site"
