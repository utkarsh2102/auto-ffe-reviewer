"""Tests for working out what an FFe asks for.

Titles are taken from real FFe bugs filed against stonking.
"""

from __future__ import annotations

import pytest

from ffe.evidence.parse_bug import (
    classify_kind,
    find_series_mention,
    packages_from_tasks,
    parse_subject,
)
from ffe.models import BugFacts, BugTask, FfeKind

SERIES = ("stonking", "resolute", "questing", "noble")


def _bug(title: str = "t", description: str = "", tasks: tuple[BugTask, ...] = ()) -> BugFacts:
    return BugFacts(
        id=1,
        url="",
        title=title,
        description=description,
        description_sha="x",
        reporter="dev",
        tasks=tasks,
    )


# --------------------------------------------------------------------------- #
# Packages come from structured task data, not from prose
# --------------------------------------------------------------------------- #


def test_packages_are_read_from_launchpad_tasks() -> None:
    bug = _bug(
        tasks=(
            BugTask("curl (Ubuntu)", "New", "Undecided"),
            BugTask("upki (Ubuntu)", "New", "Undecided"),
        )
    )
    assert packages_from_tasks(bug) == ("curl", "upki")


def test_series_scoped_task_targets_are_handled() -> None:
    bug = _bug(tasks=(BugTask("curl (Ubuntu Stonking)", "New", "Undecided"),))
    assert packages_from_tasks(bug) == ("curl",)


def test_distribution_level_task_yields_no_package() -> None:
    """Filed against 'ubuntu' with no package chosen -- normal for new packages."""
    assert packages_from_tasks(_bug(tasks=(BugTask("ubuntu", "New", "Undecided"),))) == ()


def test_package_is_never_guessed_from_the_title() -> None:
    """A wrong package name would misdirect every downstream source.

    Admitting we do not know is safer than gathering confident evidence about
    something the FFe is not even about -- and it stops bug text choosing which
    package gets investigated.
    """
    parsed = parse_subject(
        _bug(
            title="[FFe] please update openssl to 4.0",
            tasks=(BugTask("ubuntu", "New", "Undecided"),),
        ),
        known_series=SERIES,
        default_series="stonking",
    )
    assert parsed.subject.packages == ()
    assert any("no source package" in n for n in parsed.notes)


def test_duplicate_task_targets_collapse() -> None:
    bug = _bug(
        tasks=(BugTask("curl (Ubuntu)", "New", "U"), BugTask("curl (Ubuntu Stonking)", "New", "U"))
    )
    assert packages_from_tasks(bug) == ("curl",)


# --------------------------------------------------------------------------- #
# Target series
# --------------------------------------------------------------------------- #


def test_series_from_a_scoped_task_wins() -> None:
    parsed = parse_subject(
        _bug(
            description="mentions resolute", tasks=(BugTask("curl (Ubuntu Stonking)", "New", "U"),)
        ),
        known_series=SERIES,
        default_series="questing",
    )
    assert parsed.subject.target_series == "stonking"


def test_series_mentioned_in_the_text_is_used_next() -> None:
    parsed = parse_subject(
        _bug(
            title="FFe: pcmanfm-qt 2.4.1 in Stonking",
            tasks=(BugTask("pcmanfm-qt (Ubuntu)", "New", "U"),),
        ),
        known_series=SERIES,
        default_series="questing",
    )
    assert parsed.subject.target_series == "stonking"


def test_unstated_series_falls_back_to_development_and_says_so() -> None:
    parsed = parse_subject(
        _bug(tasks=(BugTask("curl (Ubuntu)", "New", "U"),)),
        known_series=SERIES,
        default_series="stonking",
    )
    assert parsed.subject.target_series == "stonking"
    assert any("assuming the development series" in n for n in parsed.notes)


def test_only_known_codenames_are_accepted() -> None:
    """An arbitrary word in a description must not be able to invent a release."""
    assert find_series_mention("targeting frobnicating ferret", SERIES) is None
    assert find_series_mention("targeting Stonking", SERIES) == "stonking"


# --------------------------------------------------------------------------- #
# Kind of change
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[FFe]: Please sync 2.6.12 from Debian unstable", FfeKind.SYNC),
        ("FFe: Please merge rdma-core 65.0-1 from Debian unstable", FfeKind.MERGE),
        ("[FFe] New package: uv 0.11.20+ds-0ubuntu1", FfeKind.NEW_PACKAGE),
        ("FFe: Go 1.27 transition - regression fixes", FfeKind.TRANSITION),
        ("[FFe] upki integration into curl, by default", FfeKind.SEED_CHANGE),
        ("FFe: resources 51.0", FfeKind.NEW_UPSTREAM),
        ("[FFe] Add dracut support to kdump-tools", FfeKind.FEATURE_CHANGE),
    ],
)
def test_kind_classification_on_real_titles(title: str, expected: FfeKind) -> None:
    assert classify_kind(title, "") is expected


def test_title_outranks_description() -> None:
    """Descriptions quote changelogs full of unrelated version numbers."""
    assert classify_kind("[FFe] Please sync foo", "upgrade to 5.2.1 ... 4.0.0 ...") is FfeKind.SYNC


def test_derivation_is_recorded() -> None:
    parsed = parse_subject(
        _bug(tasks=(BugTask("curl (Ubuntu)", "New", "U"),)),
        known_series=SERIES,
        default_series="stonking",
    )
    assert "packages:bug-tasks" in parsed.subject.derivation
