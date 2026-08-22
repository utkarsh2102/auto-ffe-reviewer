"""Writing the dashboard out as static files.

No build step and no framework. At this volume a bundler buys nothing and
costs npm supply-chain exposure, lockfile churn and CI time -- in a tool whose
whole value rests on a release team being able to audit what it says. The page
is HTML and one script, the data is JSON, and both are readable in a text
editor.

Every number on the page is computed here rather than in the browser, so
site/index.json can be read directly and will say exactly what the page shows.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ffe.config import Settings
from ffe.store.index import build_agreement, build_detail, build_index, build_lessons
from ffe.store.repo import Store
from ffe.util.clock import utc_iso

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


def publish(store: Store, settings: Settings, *, web_dir: Path | None = None) -> int:
    """Write the dashboard into the state tree. Returns the number of files."""
    site = store.site
    site.mkdir(parents=True, exist_ok=True)
    written = 0

    index = build_index(store)
    _write(site / "index.json", index)
    written += 1

    _write(site / "agreement.json", build_agreement(store))
    _write(site / "lessons.json", build_lessons(store))
    written += 2

    bugs_dir = site / "bugs"
    bugs_dir.mkdir(parents=True, exist_ok=True)
    for row in index["bugs"]:
        bug_id = row.get("bug_id")
        if bug_id is None:
            continue
        detail = build_detail(store, int(bug_id))
        if detail:
            _write(bugs_dir / f"{bug_id}.json", detail)
            written += 1

    _write(
        site / "meta.json",
        {
            "generated_at": utc_iso(),
            "title": settings.dashboard.title,
            "release_team": settings.launchpad.release_team,
            "harness": settings.llm.harness,
            "counts": index["counts"],
        },
    )
    written += 1

    written += _copy_assets(site, web_dir or WEB_DIR)
    return written


def _copy_assets(site: Path, web_dir: Path) -> int:
    """Copy the static page alongside its data."""
    if not web_dir.is_dir():
        return 0

    copied = 0
    for source in web_dir.rglob("*"):
        if source.is_dir() or "/." in str(source):
            continue
        target = site / source.relative_to(web_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
