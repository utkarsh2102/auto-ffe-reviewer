"""Tests for release-cycle timing.

Parsing runs against the real schedule pages for 26.10 (interim) and 26.04
(LTS), captured from documentation.ubuntu.com. Between them they cover the two
things that break naive parsers: a cycle contained in one calendar year, and a
cycle that crosses a year boundary.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path

import pytest

from ffe.config import load_settings
from ffe.errors import SourceTimeout
from ffe.models import FactStatus, ReleaseType
from ffe.sources.base import SourceContext
from ffe.sources.release_calendar import (
    approximate_milestones,
    build_context,
    development_series,
    find_series,
    load_series,
    parse_schedule,
    release_context,
)
from ffe.util.cache import Cache
from ffe.util.clock import frozen_at
from ffe.util.http import HttpClient, RawResponse

SCHEDULES = Path(__file__).parent / "fixtures" / "raw" / "schedule"
DISTRO_INFO = SCHEDULES / "ubuntu.csv"


def _milestone(page: str, name: str) -> str | None:
    for m in parse_schedule((SCHEDULES / page).read_bytes()):
        if m.name == name:
            return m.date
    return None


# --------------------------------------------------------------------------- #
# distro-info
# --------------------------------------------------------------------------- #


def test_series_lookup_by_codename_and_version() -> None:
    by_name = find_series("stonking", DISTRO_INFO)
    by_version = find_series("26.10", DISTRO_INFO)
    assert by_name == by_version
    assert by_name is not None
    assert by_name.release_date == dt.date(2026, 10, 15)


def test_lts_flag_comes_from_the_version_column() -> None:
    resolute = find_series("resolute", DISTRO_INFO)
    stonking = find_series("stonking", DISTRO_INFO)
    assert resolute is not None and stonking is not None
    assert resolute.is_lts is True
    assert resolute.release_type is ReleaseType.LTS
    assert stonking.is_lts is False
    assert stonking.release_type is ReleaseType.INTERIM
    # The " LTS" suffix is stripped from the version we display.
    assert resolute.version == "26.04"


def test_unknown_series_is_none() -> None:
    assert find_series("neverwasaseries", DISTRO_INFO) is None


def test_development_series_is_the_next_unreleased_one() -> None:
    with frozen_at("2026-09-20T12:00:00Z"):
        devel = development_series(DISTRO_INFO)
    assert devel is not None
    assert devel.series == "stonking"


def test_missing_distro_info_is_not_a_crash() -> None:
    assert load_series(Path("/nonexistent/ubuntu.csv")) == []


# --------------------------------------------------------------------------- #
# Schedule parsing
# --------------------------------------------------------------------------- #


def test_interim_schedule_parses_known_milestones() -> None:
    """26.10, whose whole cycle sits inside one calendar year."""
    assert _milestone("stonking-26.10.html", "Feature Freeze") == "2026-08-20"
    assert _milestone("stonking-26.10.html", "UI Freeze") == "2026-09-10"
    assert _milestone("stonking-26.10.html", "Beta") == "2026-09-24"
    assert _milestone("stonking-26.10.html", "Kernel Freeze") == "2026-10-01"
    assert _milestone("stonking-26.10.html", "Final Freeze") == "2026-10-08"
    assert _milestone("stonking-26.10.html", "Final Release") == "2026-10-15"


def test_lts_schedule_parses_across_a_year_boundary() -> None:
    """26.04 runs October 2025 to April 2026, so the year must come from context.

    The date cells say only "February 19"; the year comes from the month-header
    rows the table is divided by.
    """
    assert _milestone("resolute-26.04.html", "Feature Freeze") == "2026-02-19"
    assert _milestone("resolute-26.04.html", "Final Release") == "2026-04-23"
    # A milestone from the earlier, 2025 half of the cycle.
    assert _milestone("resolute-26.04.html", "Toolchain Uploaded") == "2025-10-16"


def test_release_manager_handles_are_not_milestones() -> None:
    """The page carries a second table whose cells hold handles like @utkarsh."""
    names = [m.name for m in parse_schedule((SCHEDULES / "resolute-26.04.html").read_bytes())]
    assert not [n for n in names if n.startswith("@")]


def test_unparseable_page_yields_no_milestones_rather_than_raising() -> None:
    assert parse_schedule(b"<html><body>nothing useful</body></html>") == ()


# --------------------------------------------------------------------------- #
# The eight-week heuristic is approximate, and that matters
# --------------------------------------------------------------------------- #


def test_eight_week_heuristic_is_exact_for_2610_and_wrong_for_2604() -> None:
    """Why the schedule page is authoritative rather than merely preferred.

    Approximating Feature Freeze as release minus eight weeks lands exactly on
    26.10's real date, and a week late for 26.04, whose Feature Freeze was nine
    weeks out. A reviewer told "you are one week into the freeze" when they are
    really two would be materially misled.
    """
    stonking = find_series("stonking", DISTRO_INFO)
    resolute = find_series("resolute", DISTRO_INFO)
    assert stonking is not None and resolute is not None

    assert approximate_milestones(stonking)[0].date == "2026-08-20"
    assert approximate_milestones(stonking)[0].date == _milestone(
        "stonking-26.10.html", "Feature Freeze"
    )

    approximated = approximate_milestones(resolute)[0].date
    assert approximated == "2026-02-26"
    assert approximated != _milestone("resolute-26.04.html", "Feature Freeze")


# --------------------------------------------------------------------------- #
# Derived timing context
# --------------------------------------------------------------------------- #


def _context(page: str, series: str, when: str):  # type: ignore[no-untyped-def]
    return build_context(
        find_series(series, DISTRO_INFO),  # type: ignore[arg-type]
        parse_schedule((SCHEDULES / page).read_bytes()),
        derivation="schedule-page",
        when=dt.date.fromisoformat(when),
    )


def test_context_for_stonking_today() -> None:
    ctx = _context("stonking-26.10.html", "stonking", "2026-09-20")
    assert ctx.release_type is ReleaseType.INTERIM
    assert ctx.feature_freeze == "2026-08-20"
    assert ctx.days_to_release == 25
    assert ctx.days_bucket == "28-15"
    assert ctx.days_since_feature_freeze == 31
    assert ctx.freeze_window_elapsed_pct == 55


@pytest.mark.parametrize(
    ("when", "expected_phase"),
    [
        ("2026-08-01", "PRE_FEATURE_FREEZE"),
        ("2026-08-20", "FEATURE_FREEZE"),
        ("2026-09-09", "FEATURE_FREEZE"),
        ("2026-09-10", "UI_FREEZE"),
        ("2026-09-20", "UI_FREEZE"),
        ("2026-09-24", "BETA"),
        ("2026-10-01", "KERNEL_FREEZE"),
        ("2026-10-08", "FINAL_FREEZE"),
        ("2026-10-15", "FINAL_RELEASE"),
    ],
)
def test_phase_tracks_the_milestones_that_tighten_the_bar(when: str, expected_phase: str) -> None:
    assert _context("stonking-26.10.html", "stonking", when).phase == expected_phase


def test_freeze_window_runs_from_feature_freeze_to_release() -> None:
    """Feature Freeze is not a moment that ends; it runs until release."""
    start = _context("stonking-26.10.html", "stonking", "2026-08-20")
    end = _context("stonking-26.10.html", "stonking", "2026-10-15")
    assert start.freeze_window_elapsed_pct == 0
    assert end.freeze_window_elapsed_pct == 100


def test_days_to_release_goes_negative_after_release() -> None:
    after = _context("stonking-26.10.html", "stonking", "2026-10-20")
    assert after.days_to_release == -5
    assert after.days_bucket == "post-release"


# --------------------------------------------------------------------------- #
# End-to-end, including degradation
# --------------------------------------------------------------------------- #


class _Transport:
    def __init__(self, result: RawResponse | Exception) -> None:
        self._result = result

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _ctx(tmp_path: Path, result: RawResponse | Exception) -> SourceContext:
    return SourceContext(
        http=HttpClient(
            transport=_Transport(result),  # type: ignore[arg-type]
            sleep=lambda _s: None,
            min_interval_seconds=0,
        ),
        cache=Cache(tmp_path),
        settings=load_settings(Path("/nonexistent.toml"), env={}),
    )


def test_release_context_uses_the_schedule_page(tmp_path: Path) -> None:
    page = (SCHEDULES / "stonking-26.10.html").read_bytes()
    ctx = _ctx(tmp_path, RawResponse(200, page, {}, "u"))

    with frozen_at("2026-09-20T12:00:00Z"):
        fact = release_context(ctx, series="stonking", distro_info_path=DISTRO_INFO)

    assert fact.ok
    assert fact.value is not None
    assert fact.value.derivation == "schedule-page"
    assert fact.value.feature_freeze == "2026-08-20"


def test_unreachable_schedule_falls_back_and_says_so(tmp_path: Path) -> None:
    """Losing the page costs precision, not the review -- and the record says which."""
    ctx = _ctx(tmp_path, SourceTimeout("calendar.schedule", "timed out"))

    with frozen_at("2026-09-20T12:00:00Z"):
        fact = release_context(ctx, series="stonking", distro_info_path=DISTRO_INFO)

    assert fact.ok, "a usable context should still be produced"
    assert fact.value is not None
    assert fact.value.derivation == "approximate"
    assert fact.value.release_type is ReleaseType.INTERIM  # still known from distro-info
    assert "approximated" in (fact.note or "")


def test_missing_distro_info_yields_unavailable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, RawResponse(200, b"", {}, "u"))
    fact = release_context(ctx, series="stonking", distro_info_path=Path("/nonexistent.csv"))

    assert fact.status is FactStatus.UNAVAILABLE
    assert "distro-info-data" in (fact.note or "")
