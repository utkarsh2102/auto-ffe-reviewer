"""Bugs to review regardless of how discovery found them.

Discovery is normally exact -- the bugs subscribed to ~ubuntu-release are the
team's own queue -- so this is the escape hatch rather than the mechanism. It
covers a request filed without subscribing the team, one being tracked ahead of
a formal request, or a bug the sweep's title matching misses.

Listing a bug here gathers evidence for it. It subscribes nobody to anything,
and it does not mark the bug reviewed, approved or rejected.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

WATCHLIST_TOML = Path(__file__).resolve().parents[3] / "data" / "watchlist.toml"


def load_watchlist(path: Path = WATCHLIST_TOML) -> tuple[int, ...]:
    """Bug ids to review in addition to the queue. Empty if the file is absent."""
    if not path.is_file():
        return ()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        # A malformed watchlist must not stop the queue being reviewed.
        return ()

    entries = (raw.get("watchlist", {}) or {}).get("bugs", []) or []
    seen: list[int] = []
    for entry in entries:
        try:
            bug_id = int(entry)
        except (TypeError, ValueError):
            continue
        if bug_id > 0 and bug_id not in seen:
            seen.append(bug_id)
    return tuple(seen)
