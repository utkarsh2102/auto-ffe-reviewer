"""Tests for the review policy and its bundling.

The policy is the actual product here -- the criteria a Release Team member
would recognise as their own. These tests guard its integrity rather than its
prose: that it is complete, ordered, versioned, and that editing it is a
deliberate act.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ffe.errors import ConfigError
from ffe.llm.prompt import (
    POLICY_DIR,
    build_system_prompt,
    load_policy,
    render_precedents,
)

MANIFEST = tomllib.loads((POLICY_DIR / "manifest.toml").read_text(encoding="utf-8"))


def _flat(text: str) -> str:
    """Collapse whitespace, so content assertions do not depend on line wrapping."""
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #


def test_policy_bundles() -> None:
    policy = load_policy()
    assert policy.version
    assert policy.parts
    assert len(policy.text) > 10_000


def test_every_manifest_part_exists() -> None:
    for part in MANIFEST["parts"]:
        assert (POLICY_DIR / part).is_file(), f"manifest lists a missing part: {part}"


def test_every_rubric_file_is_in_the_manifest() -> None:
    """A rubric file not in the manifest would silently never be used."""
    listed = set(MANIFEST["parts"])
    on_disk = {f"rubric/{p.name}" for p in (POLICY_DIR / "rubric").glob("*.md")}
    assert on_disk <= listed, f"rubric files missing from manifest: {on_disk - listed}"


def test_policy_hash_matches_the_recorded_one() -> None:
    """Editing the rubric must be a versioned act.

    The policy hash feeds the review key, so an unrecorded edit would
    re-review every open bug with no record of what changed. Failing here means
    the policy was edited: bump `version` in policy/manifest.toml and update
    `policy_hash` to the value this test reports.
    """
    recorded = MANIFEST.get("policy_hash")
    actual = load_policy().policy_hash
    assert recorded == actual, (
        f"policy content changed.\n  recorded: {recorded}\n  actual:   {actual}\n"
        "Bump version and update policy_hash in policy/manifest.toml."
    )


def test_hash_changes_when_the_policy_changes(tmp_path: Path) -> None:
    (tmp_path / "rubric").mkdir()
    (tmp_path / "manifest.toml").write_text('version = "1.0.0"\nparts = ["rubric/a.md"]\n')
    (tmp_path / "rubric" / "a.md").write_text("original")
    first = load_policy(tmp_path).policy_hash

    (tmp_path / "rubric" / "a.md").write_text("amended")
    assert load_policy(tmp_path).policy_hash != first


def test_missing_manifest_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="manifest not found"):
        load_policy(tmp_path)


def test_missing_part_is_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / "manifest.toml").write_text('version = "1.0"\nparts = ["nope.md"]\n')
    with pytest.raises(ConfigError, match="missing"):
        load_policy(tmp_path)


# --------------------------------------------------------------------------- #
# Content the system depends on
# --------------------------------------------------------------------------- #


def test_criteria_live_in_the_policy_not_in_code() -> None:
    """Each numbered criterion from the brief has a home a reviewer can read."""
    text = _flat(load_policy().text).lower()
    for topic in (
        "lts",
        "interim",
        "feature freeze",
        "seeded",
        "reverse dependencies",
        "blast radius",
        "regression",
        "flavour",
        "acknowledg",
        "value",
    ):
        assert topic in text, f"the policy says nothing about {topic}"


def test_the_human_decides_boundary_is_stated() -> None:
    text = _flat(load_policy().text).lower()
    assert "you never decide" in text
    assert "never approving anything" in text or "not approving anything" in text


def test_claims_are_distinguished_from_evidence() -> None:
    text = _flat(load_policy().text)
    assert "CLAIMED_ONLY" in text
    assert "FOUND_VERIFIED" in text


def test_untrusted_input_rules_are_present() -> None:
    text = _flat(load_policy().text).lower()
    assert "never an instruction" in text
    assert "nonce" in text


def test_output_contract_names_every_flag_the_model_may_use() -> None:
    text = _flat(load_policy().text)
    for flag in (
        "CORE_PACKAGE",
        "MULTI_FLAVOUR_ACK_OUTSTANDING",
        "NO_TESTING_EVIDENCE",
        "SUSPECTED_PROMPT_INJECTION",
        "CONSIDER_SRU_INSTEAD",
    ):
        assert flag in text


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def test_system_prompt_carries_the_schema() -> None:
    prompt = _flat(build_system_prompt(load_policy(), schema={"type": "object", "title": "marker"}))
    assert "marker" in prompt
    assert "response schema" in prompt


def test_precedents_are_framed_as_observation_not_rule() -> None:
    rendered = render_precedents(
        [
            {
                "lesson_id": "L1",
                "situation": "unseeded developer tooling, no PPA",
                "our_decision": "NEEDS_INFORMATION",
                "human_decision": "APPROVED",
                "lesson": "syncs of unseeded tooling do not need a separate PPA",
                "rationale_quote": "Debian already built it; no PPA needed here.",
                "rationale_author": "utkarsh",
                "source_bug": 2167756,
            }
        ],
        max_chars=6000,
    )
    rendered = _flat(rendered)
    assert "never outrank the rubric" in rendered
    assert "utkarsh said" in rendered
    assert "2167756" in rendered


def test_precedents_are_capped() -> None:
    lessons = [
        {
            "lesson_id": f"L{i}",
            "situation": "x" * 200,
            "lesson": "y" * 200,
            "rationale_quote": "z" * 200,
            "rationale_author": "utkarsh",
            "source_bug": i,
        }
        for i in range(100)
    ]
    assert len(render_precedents(lessons, max_chars=2000)) <= 2600


def test_no_precedents_renders_nothing() -> None:
    assert render_precedents([], max_chars=6000) == ""
