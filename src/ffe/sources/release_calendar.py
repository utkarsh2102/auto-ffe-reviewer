"""Where the target release sits in its cycle.

Feature Freeze runs from the FF date until final release; there is no separate
"are we frozen" state to look up. Everything the review needs about timing is a
position inside that one window, plus which named milestones have already
passed.

The published release schedule is authoritative. A common shortcut is to
approximate Feature Freeze as release minus eight weeks, and that is worth
knowing but not worth trusting: it lands exactly right for 26.10 (2026-08-20)
and a week early for 26.04, whose Feature Freeze was 2026-02-19, nine weeks
before release. So the schedule page is the source, distro-info supplies the
release dates and the LTS flag, and the offset heuristic is a clearly-labelled
last resort whose approximate nature is recorded in `derivation`.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import re
from dataclasses import dataclass
from pathlib import Path

from ffe.models import (
    Fact,
    FactStatus,
    Milestone,
    ReleaseContext,
    ReleaseType,
    days_to_release_bucket,
)
from ffe.sources.base import SourceContext, http_fact, unavailable
from ffe.util.clock import today

SOURCE_ID = "calendar"
DISTRO_INFO_CSV = Path("/usr/share/distro-info/ubuntu.csv")
SCHEDULE_URL = "https://documentation.ubuntu.com/release-notes/{version}/schedule/"

IMPACT = "cannot tell how late in the freeze this request is"

# Milestone names as the schedule prints them, mapped to the names we use.
# Anything not listed is still captured verbatim; this only normalises the ones
# the review criteria actually reason about.
_MILESTONE_ALIASES = {
    "feature freeze": "Feature Freeze",
    "user interface freeze": "UI Freeze",
    "ui freeze": "UI Freeze",
    "documentation string freeze": "Documentation String Freeze",
    "beta": "Beta",
    "beta (mandatory)": "Beta",
    "beta freeze": "Beta Freeze",
    "kernel freeze": "Kernel Freeze",
    "kernel feature freeze": "Kernel Feature Freeze",
    "hardware enablement freeze": "Hardware Enablement Freeze",
    "final freeze": "Final Freeze",
    "release candidate": "Release Candidate",
    "final release": "Final Release",
    "debian import freeze": "Debian Import Freeze",
}

# Milestones that meaningfully tighten the bar, in the order they occur. Passing
# one of these is a phase change, and phase is what the fingerprint tracks.
PHASE_MILESTONES = (
    "Feature Freeze",
    "UI Freeze",
    "Beta Freeze",
    "Beta",
    "Kernel Freeze",
    "Final Freeze",
    "Final Release",
)

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_NUMBER = {name.lower(): index for index, name in enumerate(_MONTHS, start=1)}

_MONTH_HEADER = re.compile(rf"^({'|'.join(_MONTHS)})\s+(\d{{4}})$", re.I)
_DATE_CELL = re.compile(rf"^({'|'.join(_MONTHS)})\s+(\d{{1,2}})\b", re.I)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class SeriesInfo:
    series: str  # "stonking"
    version: str  # "26.10"
    created: dt.date
    release_date: dt.date
    is_lts: bool

    @property
    def release_type(self) -> ReleaseType:
        return ReleaseType.LTS if self.is_lts else ReleaseType.INTERIM


# --------------------------------------------------------------------------- #
# distro-info: series names, versions, release dates, LTS flag
# --------------------------------------------------------------------------- #


def load_series(path: Path = DISTRO_INFO_CSV) -> list[SeriesInfo]:
    """Read the distro-info table. Returns [] if it is not installed."""
    if not path.is_file():
        return []

    out: list[SeriesInfo] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            version = (row.get("version") or "").strip()
            series = (row.get("series") or "").strip()
            try:
                created = dt.date.fromisoformat((row.get("created") or "").strip())
                release = dt.date.fromisoformat((row.get("release") or "").strip())
            except ValueError:
                continue  # Rows without a planned release date are not useful here.
            out.append(
                SeriesInfo(
                    series=series,
                    version=version.replace(" LTS", ""),
                    created=created,
                    release_date=release,
                    # distro-info marks LTS in the version column, e.g. "26.04 LTS".
                    is_lts="LTS" in version,
                )
            )
    return out


def find_series(name: str, path: Path = DISTRO_INFO_CSV) -> SeriesInfo | None:
    """Look up by codename ('stonking') or version ('26.10')."""
    wanted = name.strip().lower().replace(" lts", "")
    for info in load_series(path):
        if wanted in (info.series.lower(), info.version.lower()):
            return info
    return None


def development_series(path: Path = DISTRO_INFO_CSV) -> SeriesInfo | None:
    """The series currently in development: the earliest not yet released."""
    now = today()
    upcoming = [s for s in load_series(path) if s.release_date > now]
    return min(upcoming, key=lambda s: s.release_date) if upcoming else None


# --------------------------------------------------------------------------- #
# Schedule page parsing
# --------------------------------------------------------------------------- #


def _is_milestone(label: str) -> bool:
    """Reject cell contents that are not milestone names.

    The events column also carries release-manager handles such as "@utkarsh",
    which would otherwise show up on the dashboard as a milestone.
    """
    return bool(label) and not label.startswith("@") and len(label) > 2


def _text(fragment: str) -> str:
    return html.unescape(_TAG.sub(" ", fragment)).replace("\xa0", " ").strip()


def parse_schedule(payload: bytes) -> tuple[Milestone, ...]:
    """Extract milestones from a release-schedule page.

    The date cells carry no year -- they read "August 20" -- but the table is
    broken up by month-header rows such as "October 2025", which is what makes
    a cycle spanning a year boundary (any .04 release) parseable at all.
    """
    document = payload.decode("utf-8", "replace")
    year: int | None = None
    found: dict[str, str] = {}

    for row_html in _ROW.findall(document):
        cells = [_text(c) for c in _CELL.findall(row_html)]
        if not cells:
            continue

        header = _MONTH_HEADER.match(cells[0])
        if header:
            year = int(header.group(2))
            continue

        # The page also renders a five-column milestone summary table whose
        # cells hold release-manager handles rather than milestone names, so
        # only the three-column week table is parsed.
        if len(cells) != 3 or year is None:
            continue

        date_match = _DATE_CELL.match(cells[1])
        if not date_match:
            continue

        month = _MONTH_NUMBER[date_match.group(1).lower()]
        day = int(date_match.group(2))
        try:
            when = dt.date(year, month, day).isoformat()
        except ValueError:
            continue

        for raw in re.split(r"[,•]", cells[2]):
            label = _text(raw)
            if not _is_milestone(label):
                continue
            name = _MILESTONE_ALIASES.get(label.lower(), label)
            # First occurrence wins: the page repeats its table lower down, and
            # a later restatement should not overwrite the real entry.
            found.setdefault(name, when)

    return tuple(
        sorted(
            (Milestone(name=n, date=d) for n, d in found.items()), key=lambda m: (m.date, m.name)
        )
    )


# --------------------------------------------------------------------------- #
# Deriving the review's timing context
# --------------------------------------------------------------------------- #


def _phase(milestones: tuple[Milestone, ...], when: dt.date) -> str:
    """Name the most recent milestone that has already passed.

    Only milestones that tighten the bar count, so the phase changes when the
    Release Team's tolerance actually changes rather than on every calendar row.
    """
    passed = [
        m
        for m in milestones
        if m.name in PHASE_MILESTONES and dt.date.fromisoformat(m.date) <= when
    ]
    if not passed:
        return "PRE_FEATURE_FREEZE"
    latest = max(passed, key=lambda m: m.date)
    return latest.name.upper().replace(" ", "_")


def _milestone_date(milestones: tuple[Milestone, ...], name: str) -> str | None:
    for m in milestones:
        if m.name == name:
            return m.date
    return None


def build_context(
    info: SeriesInfo,
    milestones: tuple[Milestone, ...],
    *,
    derivation: str,
    when: dt.date | None = None,
) -> ReleaseContext:
    """Assemble the timing context from a series and its milestones."""
    when = when or today()

    release_date = info.release_date
    scheduled_release = _milestone_date(milestones, "Final Release")
    if scheduled_release:
        release_date = dt.date.fromisoformat(scheduled_release)

    feature_freeze_iso = _milestone_date(milestones, "Feature Freeze")
    feature_freeze = dt.date.fromisoformat(feature_freeze_iso) if feature_freeze_iso else None

    days_to_release = (release_date - when).days
    days_since_ff = (when - feature_freeze).days if feature_freeze else None

    elapsed_pct: int | None = None
    if feature_freeze and release_date > feature_freeze:
        window = (release_date - feature_freeze).days
        elapsed_pct = max(0, min(100, round(100 * (when - feature_freeze).days / window)))

    return ReleaseContext(
        series=info.series,
        version=info.version,
        release_type=info.release_type,
        feature_freeze=feature_freeze.isoformat() if feature_freeze else None,
        release_date=release_date.isoformat(),
        milestones=milestones,
        phase=_phase(milestones, when),
        days_to_release=days_to_release,
        days_since_feature_freeze=days_since_ff,
        days_bucket=days_to_release_bucket(days_to_release),
        freeze_window_elapsed_pct=elapsed_pct,
        derivation=derivation,
    )


def approximate_milestones(info: SeriesInfo) -> tuple[Milestone, ...]:
    """Fallback when the schedule page cannot be read.

    Feature Freeze is roughly eight weeks before release. "Roughly" is doing
    real work: 26.10 was exactly eight, 26.04 was nine. Anything derived this
    way is labelled approximate so a reviewer knows not to lean on the exact day.
    """
    return (
        Milestone(
            name="Feature Freeze", date=(info.release_date - dt.timedelta(weeks=8)).isoformat()
        ),
        Milestone(name="Final Release", date=info.release_date.isoformat()),
    )


def release_context(
    ctx: SourceContext,
    *,
    series: str | None = None,
    distro_info_path: Path = DISTRO_INFO_CSV,
) -> Fact[ReleaseContext]:
    """Resolve the timing context for `series`, or for the development series."""
    info = find_series(series, distro_info_path) if series else development_series(distro_info_path)
    if info is None:
        what = f"series {series!r}" if series else "the development series"
        return unavailable(
            SOURCE_ID,
            str(distro_info_path),
            f"could not identify {what}; is distro-info-data installed?",
        )

    url = SCHEDULE_URL.format(version=info.version)
    schedule = http_fact(
        ctx,
        source_id=f"{SOURCE_ID}.schedule",
        url=url,
        ttl=ctx.settings.cache.release_calendar_ttl,
        parse=parse_schedule,
        impact=IMPACT,
    )

    if schedule.ok and schedule.value:
        milestones = schedule.value
        derivation = "schedule-page"
        note = None
        if not _milestone_date(milestones, "Feature Freeze"):
            # The page parsed but says nothing about Feature Freeze; fill that
            # one gap from the heuristic rather than discarding the real data.
            milestones = tuple(
                sorted(
                    [*milestones, approximate_milestones(info)[0]], key=lambda m: (m.date, m.name)
                )
            )
            derivation = "schedule-page+approximate-ff"
            note = "Feature Freeze not listed on the schedule page; approximated as release minus 8 weeks"
    else:
        milestones = approximate_milestones(info)
        derivation = "approximate"
        note = (
            "schedule page unavailable; dates approximated as release minus 8 weeks, "
            "which has been observed to be up to a week early"
        )

    # The context is usable either way: distro-info resolved the series, so we
    # always have a release date and an LTS flag. A failed schedule fetch costs
    # precision on the milestone dates, which `derivation` and `note` record.
    return Fact(
        value=build_context(info, milestones, derivation=derivation),
        status=FactStatus.OK,
        provenance=schedule.provenance,
        note=note,
    )
