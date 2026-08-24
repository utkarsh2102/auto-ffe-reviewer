"""Tests that keep the documentation honest.

Docs drift. These check the claims that would mislead someone if they went
stale -- commands that no longer exist, links that go nowhere, and the
statements about what the system will and will not do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
README = ROOT / "README.md"


def _all_markdown() -> list[Path]:
    return [README, *sorted(DOCS.glob("*.md")), ROOT / "web" / "ATTRIBUTION.md"]


def _flat(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Links resolve
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("document", _all_markdown(), ids=lambda p: p.name)
def test_relative_links_resolve(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    for match in re.finditer(r"\[[^\]]+\]\(([^)#]+)\)", text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (document.parent / target).resolve()
        assert resolved.exists(), f"{document.name} links to {target}, which does not exist"


def test_readme_links_to_every_document() -> None:
    """A document nobody can find may as well not exist."""
    readme = README.read_text(encoding="utf-8")
    for document in DOCS.glob("*.md"):
        assert f"docs/{document.name}" in readme, f"README does not link to {document.name}"


# --------------------------------------------------------------------------- #
# Documented commands exist
# --------------------------------------------------------------------------- #


def _documented_commands() -> set[str]:
    found: set[str] = set()
    for document in _all_markdown():
        for match in re.finditer(
            r"(?:^|\s|`)ffe ([a-z][a-z-]+)", document.read_text(encoding="utf-8")
        ):
            found.add(match.group(1))
    return found


def test_every_documented_command_exists() -> None:
    from ffe.cli import build_parser

    actions = build_parser()._subparsers._group_actions[0]  # type: ignore[union-attr]
    available = set(actions.choices)  # type: ignore[attr-defined]

    unknown = _documented_commands() - available
    assert not unknown, f"documentation refers to commands that do not exist: {unknown}"


def test_every_command_is_documented() -> None:
    from ffe.cli import build_parser

    actions = build_parser()._subparsers._group_actions[0]  # type: ignore[union-attr]
    available = set(actions.choices)  # type: ignore[attr-defined]

    undocumented = available - _documented_commands()
    assert not undocumented, f"commands exist but are undocumented: {undocumented}"


def test_documented_environment_variables_are_real() -> None:
    from ffe.config import load_settings

    settings = load_settings(ROOT / "config.toml", env={})
    text = " ".join(d.read_text(encoding="utf-8") for d in _all_markdown())

    for name in set(re.findall(r"\bFFE_[A-Z_]+\b", text)):
        if name in {
            "FFE_LLM_API_KEY",
            "FFE_LLM_BASE_URL",
            "FFE_CACHE_DIR",
            "FFE_STATE_DIR",
            "FFE_OFFLINE",
        }:
            continue  # handled outside the section machinery
        section, _, key = name.removeprefix("FFE_").partition("_")
        assert hasattr(settings, section.lower()), f"{name} names no settings section"
        assert hasattr(getattr(settings, section.lower()), key.lower()), f"{name} names no setting"


# --------------------------------------------------------------------------- #
# The claims that matter
# --------------------------------------------------------------------------- #


def test_the_advisory_boundary_is_stated_where_people_will_see_it() -> None:
    assert "Advisory only" in README.read_text(encoding="utf-8")
    boundary = _flat((DOCS / "automation-boundary.md").read_text(encoding="utf-8"))
    assert "never decides anything" in boundary


def test_the_no_launchpad_credentials_claim_is_true() -> None:
    """Documented as a security property, so it had better hold."""
    text = " ".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "ffe").rglob("*.py"))
    assert "login_with" not in text
    assert "LP_CREDENTIALS_FILE" not in text
    # Every Launchpad call is a GET through the bounded HTTP client.
    launchpad = (ROOT / "src" / "ffe" / "sources" / "launchpad.py").read_text(encoding="utf-8")
    assert ".post(" not in launchpad
    assert "ws.op=searchTasks" in launchpad or "searchTasks" in launchpad


def test_the_no_gate_can_approve_claim_is_true() -> None:
    boundary = _flat((DOCS / "automation-boundary.md").read_text(encoding="utf-8"))
    assert "No deterministic gate can produce" in boundary

    gates = (ROOT / "src" / "ffe" / "risk" / "gates.py").read_text(encoding="utf-8")
    assert "FLOOR_APPROVE" not in gates


def test_the_swappable_harness_claim_matches_the_registry() -> None:
    from ffe.llm.registry import HARNESSES

    harnesses = _flat((DOCS / "adding-a-harness.md").read_text(encoding="utf-8"))
    assert "openai-compat" in harnesses
    assert "openai-compat" in HARNESSES
    assert "claude-code" in HARNESSES


def test_the_policy_file_list_is_complete() -> None:
    """The criteria document indexes policy/, so it must not fall behind."""
    criteria = (DOCS / "review-criteria.md").read_text(encoding="utf-8")
    for part in (ROOT / "policy" / "rubric").glob("*.md"):
        assert part.name in criteria, f"review-criteria.md does not mention {part.name}"


def test_documented_workflow_commands_match_the_workflow() -> None:
    workflow = (ROOT / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8")
    claude_doc = (DOCS / "configuring-claude-code.md").read_text(encoding="utf-8")
    assert "@anthropic-ai/claude-code" in workflow
    assert "@anthropic-ai/claude-code" in claude_doc


def test_the_claude_code_flags_documented_are_the_flags_used() -> None:
    harness = (ROOT / "src" / "ffe" / "llm" / "claude_code.py").read_text(encoding="utf-8")
    documented = (DOCS / "configuring-claude-code.md").read_text(encoding="utf-8")
    for flag in (
        "--bare",
        "--system-prompt-file",
        "--allowed-tools",
        "--strict-mcp-config",
        "--max-turns",
    ):
        assert flag in harness, f"{flag} is documented but not used"
        assert flag in documented, f"{flag} is used but not documented"


def test_the_degraded_index_incident_is_recorded() -> None:
    """The most instructive thing found while building this; worth not losing."""
    security = _flat((DOCS / "security.md").read_text(encoding="utf-8"))
    assert "5,865" in security
    assert "10,232" in security
    assert "must not be read as authoritative about absence" in security
