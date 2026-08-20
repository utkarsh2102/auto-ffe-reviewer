"""Tests for developer-supplied testing evidence.

The property these exist to protect: a bug can say whatever it likes, and
saying it never amounts to having done it.
"""

from __future__ import annotations

from ffe.evidence.testing import collect, extract
from ffe.models import BugComment, BugFacts, EvidenceState


def _bug(description: str, comments: tuple[str, ...] = ()) -> BugFacts:
    return BugFacts(
        id=1,
        url="",
        title="[FFe] something",
        description=description,
        description_sha="x",
        reporter="dev",
        comments=tuple(
            BugComment(index=i + 1, author="dev", date_created="", content=c, content_sha=f"h{i}")
            for i, c in enumerate(comments)
        ),
    )


class _StubLaunchpad:
    """Answers PPA lookups from a set of PPAs that 'exist'."""

    def __init__(self, existing: set[tuple[str, str]]) -> None:
        self.existing = existing
        self.looked_up: list[tuple[str, str]] = []

    def verify_ppa(self, owner: str, name: str):  # type: ignore[no-untyped-def]
        from ffe.models import Fact, FactStatus, Provenance, SourceMethod
        from ffe.sources.launchpad import PpaInfo

        self.looked_up.append((owner, name))
        return Fact(
            value=PpaInfo(
                owner=owner,
                name=name,
                exists=(owner, name) in self.existing,
                url=f"https://launchpad.net/~{owner}/+archive/ubuntu/{name}",
            ),
            status=FactStatus.OK,
            provenance=Provenance("lp.ppa", SourceMethod.LP_API, "u", "2026-09-20T00:00:00Z"),
        )


# --------------------------------------------------------------------------- #
# Claims are not evidence
# --------------------------------------------------------------------------- #


def test_prose_claim_does_not_corroborate_anything() -> None:
    """The headline property.

    "I have tested this in a PPA" contains the word PPA and the word tested,
    and establishes neither.
    """
    evidence = collect(_bug("I have tested this in a PPA and it builds fine with no regressions."))

    assert evidence.corroborated is False
    assert evidence.ppa is EvidenceState.ABSENT
    assert evidence.unverified_claims != ()


def test_claims_are_still_reported_to_the_reviewer() -> None:
    """They may well be true, and they tell a reviewer what to ask about."""
    evidence = collect(_bug("I tested this locally on amd64."))
    assert any("tested this locally" in c for c in evidence.unverified_claims)


def test_several_claims_are_captured_but_capped() -> None:
    text = ". ".join(f"I have tested this on arch{i}" for i in range(12))
    assert len(collect(_bug(text)).unverified_claims) <= 5


# --------------------------------------------------------------------------- #
# Links are checkable, and get checked
# --------------------------------------------------------------------------- #


def test_ppa_link_is_found_and_verified() -> None:
    lp = _StubLaunchpad({("utkarsh", "ffe-test")})
    evidence = collect(
        _bug("Built in https://launchpad.net/~utkarsh/+archive/ubuntu/ffe-test"),
        launchpad=lp,  # type: ignore[arg-type]
    )

    assert lp.looked_up == [("utkarsh", "ffe-test")]
    assert evidence.ppa is EvidenceState.FOUND_VERIFIED
    assert evidence.corroborated is True


def test_a_linked_ppa_that_does_not_exist_is_downgraded_to_a_claim() -> None:
    """A link to nothing is worth no more than a sentence saying the same.

    This is the case that would otherwise read as strong evidence while being
    empty, so it must not be allowed to satisfy the testing requirement.
    """
    lp = _StubLaunchpad(existing=set())
    evidence = collect(
        _bug("Built in https://launchpad.net/~someone/+archive/ubuntu/gone"),
        launchpad=lp,  # type: ignore[arg-type]
    )

    assert evidence.ppa is EvidenceState.CLAIMED_ONLY
    assert evidence.corroborated is False
    assert any("does not exist" in i.detail for i in evidence.items)


def test_links_without_a_launchpad_client_stay_unverified() -> None:
    """Unverified is still checkable by a human, so it counts -- but is labelled."""
    evidence = collect(_bug("https://launchpad.net/~utkarsh/+archive/ubuntu/ffe-test"))
    assert evidence.ppa is EvidenceState.FOUND_UNVERIFIED
    assert evidence.corroborated is True


def test_build_log_and_autopkgtest_links_are_recognised() -> None:
    evidence = collect(
        _bug(
            "log https://launchpadlibrarian.net/12345678/buildlog_ubuntu-stonking-amd64.curl.txt.gz\n"
            "tests https://autopkgtest.ubuntu.com/packages/c/curl/stonking/amd64"
        )
    )
    assert evidence.build is EvidenceState.FOUND_UNVERIFIED
    assert evidence.autopkgtest is EvidenceState.FOUND_UNVERIFIED


def test_external_ci_counts_as_test_evidence() -> None:
    evidence = collect(_bug("CI: https://salsa.debian.org/foo/bar/-/pipelines/12345"))
    assert evidence.autopkgtest is EvidenceState.FOUND_UNVERIFIED


# --------------------------------------------------------------------------- #
# Pasted output
# --------------------------------------------------------------------------- #


def test_pasted_test_output_is_recognised() -> None:
    evidence = collect(
        _bug("Results:\n```\nautopkgtest [12:00:00]: @@@ summary\nupstream-tests PASS\n```")
    )
    assert evidence.test_output is EvidenceState.FOUND_UNVERIFIED
    assert evidence.corroborated is True


def test_a_changelog_block_is_not_test_output() -> None:
    """Debdiffs and changelogs are fenced too; the content has to say otherwise."""
    evidence = collect(
        _bug("Changelog:\n```\n* New upstream release\n* Fix typo\n* Bump standards\n```")
    )
    assert evidence.test_output is EvidenceState.ABSENT
    assert evidence.corroborated is False


# --------------------------------------------------------------------------- #
# Where the evidence came from
# --------------------------------------------------------------------------- #


def test_evidence_added_in_a_later_comment_is_found() -> None:
    """The usual sequence: the team asks for a PPA, the developer adds one.

    That update is precisely what should turn a NEEDS_INFORMATION into
    something reviewable, so comments must be searched as well as the
    description.
    """
    evidence = collect(
        _bug("No evidence yet.", comments=("Here: https://launchpad.net/~me/+archive/ubuntu/test",))
    )
    assert evidence.ppa is EvidenceState.FOUND_UNVERIFIED
    assert [i.source for i in evidence.items if i.kind == "ppa"] == ["comment #1"]


def test_the_description_is_not_double_counted_via_comment_zero() -> None:
    """Launchpad restates the description as message index 0."""
    bug = BugFacts(
        id=1,
        url="",
        title="t",
        description="https://launchpad.net/~me/+archive/ubuntu/test",
        description_sha="x",
        reporter="dev",
        comments=(
            BugComment(
                index=0,
                author="dev",
                date_created="",
                content="https://launchpad.net/~me/+archive/ubuntu/test",
                content_sha="h",
            ),
        ),
    )
    assert len([i for i in extract(bug).items if i.kind == "ppa"]) == 1


def test_duplicate_ppa_links_are_looked_up_once() -> None:
    lp = _StubLaunchpad({("me", "test")})
    url = "https://launchpad.net/~me/+archive/ubuntu/test"
    collect(_bug(f"{url}\nand again {url}"), launchpad=lp)  # type: ignore[arg-type]
    assert lp.looked_up == [("me", "test")]


# --------------------------------------------------------------------------- #
# Nothing at all
# --------------------------------------------------------------------------- #


def test_no_evidence_is_reported_as_no_evidence() -> None:
    evidence = collect(_bug("Please grant an FFe for this useful feature."))
    assert evidence.corroborated is False
    assert evidence.ppa is EvidenceState.ABSENT
    assert evidence.build is EvidenceState.ABSENT
    assert evidence.autopkgtest is EvidenceState.ABSENT
    assert evidence.items == ()


def test_empty_bug_is_handled() -> None:
    assert collect(_bug("")).corroborated is False
