"""Time, injectable.

Every timestamp in the system comes from here so tests can pin "now" and
golden records stay stable. Real code never calls datetime.now() directly.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from contextlib import contextmanager

_frozen: _dt.datetime | None = None


def now() -> _dt.datetime:
    """Current time as an aware UTC datetime, or the frozen value under test."""
    return _frozen if _frozen is not None else _dt.datetime.now(tz=_dt.UTC)


def utc_iso(when: _dt.datetime | None = None) -> str:
    """Format as ISO-8601 in UTC with a trailing Z, to the second.

    Sub-second precision is dropped deliberately: these strings end up in
    records that are diffed and hashed, and microseconds add churn without
    adding meaning.
    """
    when = when or now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.UTC)
    return when.astimezone(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> _dt.date:
    return now().date()


def parse_iso(value: str) -> _dt.datetime:
    """Parse an ISO-8601 timestamp, tolerating the trailing Z Launchpad may use."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = _dt.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def days_between(earlier: _dt.date, later: _dt.date) -> int:
    """Whole days from `earlier` to `later`; negative when `later` is first."""
    return (later - earlier).days


@contextmanager
def frozen_at(when: _dt.datetime | str) -> Iterator[_dt.datetime]:
    """Pin `now()` for the duration of the block. Test-only."""
    global _frozen
    previous = _frozen
    _frozen = parse_iso(when) if isinstance(when, str) else when
    try:
        yield _frozen
    finally:
        _frozen = previous
