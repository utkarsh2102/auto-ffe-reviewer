"""Storage, on a git branch rather than in a database.

There is no server here and no schema migration. Reviews are JSON files on an
orphan `state` branch, committed one decision at a time, which gives an audit
trail as a side effect of storing things at all: `git log` answers "what did
this recommend on Tuesday, and on what evidence" without anyone having built a
feature for it. For a tool whose output a release team is meant to weigh, being
able to reconstruct a past recommendation matters more than query flexibility.

The layout:

    state.json                          per-bug tracking and budgets
    reviews/<bug>/<fingerprint>.json    immutable; one per distinct evidence state
    reviews/<bug>/latest.json           a copy, so the dashboard need not guess
    archive/<bug>.json                  decided bugs, with the team's verdict
    lessons/<id>.json                   learned precedents
    site/                               generated dashboard payloads

Records are keyed by evidence fingerprint, so re-reviewing unchanged evidence
overwrites nothing and a genuine change leaves the previous record in place.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ffe.models import ReviewRecord, to_jsonable
from ffe.util.clock import now, parse_iso, utc_iso
from ffe.util.hashing import digest

STATE_VERSION = 1


@dataclass
class BugState:
    """What we know about one bug between runs.

    Enough to answer "has anything changed" cheaply, and to keep spending
    bounded, without re-fetching or re-reviewing.
    """

    bug_id: int
    # Cheap change gate: compared before any source is consulted.
    date_last_updated: str = ""
    message_count: int = 0
    date_last_message: str | None = None

    # Expensive gate: the model runs only when this moves.
    fingerprint: str = ""
    review_key: str = ""

    last_seen_at: str = ""
    last_evidence_at: str = ""
    last_review_at: str = ""
    decision: str | None = None
    confidence: str | None = None
    status: str = ""
    in_queue: bool = True
    discovered_via: str = ""

    # Budget counters, so a comment storm cannot burn a day's spend.
    llm_runs_today: int = 0
    llm_runs_date: str = ""

    def llm_budget_used(self, today: str) -> int:
        return self.llm_runs_today if self.llm_runs_date == today else 0

    def seconds_since_review(self) -> float:
        if not self.last_review_at:
            return float("inf")
        try:
            return (now() - parse_iso(self.last_review_at)).total_seconds()
        except ValueError:
            return float("inf")


@dataclass
class State:
    version: int = STATE_VERSION
    updated_at: str = ""
    bugs: dict[int, BugState] = field(default_factory=dict)

    def get(self, bug_id: int) -> BugState:
        return self.bugs.get(bug_id, BugState(bug_id=bug_id))

    def put(self, bug: BugState) -> None:
        self.bugs[bug.bug_id] = bug


class Store:
    """Reads and writes the state tree."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.state_path = self.root / "state.json"
        self.reviews = self.root / "reviews"
        self.archive_dir = self.root / "archive"
        self.lessons_dir = self.root / "lessons"
        self.site = self.root / "site"
        self.runs = self.root / "runs"

    # -- state -------------------------------------------------------------- #

    def load_state(self) -> State:
        if not self.state_path.is_file():
            return State()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt state file costs one run of extra work, not the run.
            # Everything in it is re-derivable from Launchpad.
            return State()

        bugs = {}
        for key, value in (raw.get("bugs") or {}).items():
            try:
                bugs[int(key)] = BugState(**value)
            except (TypeError, ValueError):
                continue  # A field we no longer recognise: drop that entry.
        return State(
            version=int(raw.get("version", STATE_VERSION)),
            updated_at=raw.get("updated_at", ""),
            bugs=bugs,
        )

    def save_state(self, state: State) -> None:
        state.updated_at = utc_iso()
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_json(
            self.state_path,
            {
                "version": state.version,
                "updated_at": state.updated_at,
                "bugs": {str(k): asdict(v) for k, v in sorted(state.bugs.items())},
            },
        )

    # -- review records ----------------------------------------------------- #

    def record_path(self, bug_id: int, fingerprint: str) -> Path:
        # Fingerprints arrive as "sha256:..." which is not a filename.
        name = fingerprint.split(":", 1)[-1][:16] or "unknown"
        return self.reviews / str(bug_id) / f"{name}.json"

    def write_record(self, record: ReviewRecord) -> Path:
        """Persist a review, and refresh the bug's `latest` pointer."""
        path = self.record_path(record.evidence.bug.id, record.evidence.fingerprint)
        payload = to_jsonable(record)
        self._write_json(path, payload)
        self._write_json(path.parent / "latest.json", payload)
        return path

    def latest_record(self, bug_id: int) -> dict[str, Any] | None:
        path = self.reviews / str(bug_id) / "latest.json"
        return self._read_json(path)

    def records_for(self, bug_id: int) -> list[dict[str, Any]]:
        """Every retained review of a bug, oldest first."""
        folder = self.reviews / str(bug_id)
        if not folder.is_dir():
            return []
        found = [
            self._read_json(p) for p in sorted(folder.glob("*.json")) if p.name != "latest.json"
        ]
        return sorted(
            (r for r in found if r),
            key=lambda r: str(r.get("provenance", {}).get("generated_at", "")),
        )

    # -- archive ------------------------------------------------------------ #

    def archive_record(
        self,
        record: ReviewRecord,
        *,
        our_last_assessment: dict[str, Any] | None = None,
        our_last_risk: dict[str, Any] | None = None,
    ) -> Path:
        """Move a decided bug out of the active set.

        The pairing of our last recommendation with the team's decision is what
        makes the learning loop and the agreement metric possible, so both are
        kept in full rather than reduced to a verdict.
        """
        payload = to_jsonable(record)
        assert isinstance(payload, dict)
        payload["our_last_assessment"] = our_last_assessment
        payload["our_last_risk"] = our_last_risk

        path = self.archive_dir / f"{record.evidence.bug.id}.json"
        self._write_json(path, payload)
        return path

    def archived(self) -> list[dict[str, Any]]:
        if not self.archive_dir.is_dir():
            return []
        found = [self._read_json(p) for p in sorted(self.archive_dir.glob("*.json"))]
        return [r for r in found if r]

    def is_archived(self, bug_id: int) -> bool:
        return (self.archive_dir / f"{bug_id}.json").is_file()

    # -- lessons ------------------------------------------------------------ #

    def lessons(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        if not self.lessons_dir.is_dir():
            return []
        found = [self._read_json(p) for p in sorted(self.lessons_dir.glob("*.json"))]
        lessons = [lesson for lesson in found if lesson]
        if active_only:
            lessons = [lesson for lesson in lessons if lesson.get("status") == "active"]
        return sorted(lessons, key=lambda lesson: str(lesson.get("created_at", "")), reverse=True)

    def write_lesson(self, lesson: dict[str, Any]) -> Path:
        path = self.lessons_dir / f"{lesson['lesson_id']}.json"
        self._write_json(path, lesson)
        return path

    def retire_lesson(self, lesson_id: str, reason: str) -> bool:
        """Stop applying a lesson, without erasing that it was learned."""
        path = self.lessons_dir / f"{lesson_id}.json"
        lesson = self._read_json(path)
        if not lesson:
            return False
        lesson.update({"status": "retired", "retired_at": utc_iso(), "retired_reason": reason})
        self._write_json(path, lesson)
        return True

    def lessons_hash(self) -> str:
        """Digest of the active lesson set.

        Part of the review key, so learning something new re-reviews the open
        queue rather than leaving earlier recommendations standing on rules the
        system no longer holds.
        """
        return digest([lesson.get("lesson_id", "") for lesson in self.lessons()])

    # -- run logs ----------------------------------------------------------- #

    def write_run(self, run_id: str, summary: dict[str, Any]) -> Path:
        path = self.runs / f"{run_id}.json"
        self._write_json(path, summary)
        return path

    # -- helpers ------------------------------------------------------------ #

    def _write_json(self, path: Path, payload: Any) -> None:
        """Write atomically, and stably.

        Sorted keys and a trailing newline keep git diffs meaningful: a review
        that changed one field should show as one changed line, not as a
        reordering of the whole file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None


def touch_seen(
    bug: BugState, *, date_last_updated: str, message_count: int, date_last_message: str | None
) -> BugState:
    """Record what the cheap gate saw, without claiming a review happened."""
    return replace(
        bug,
        date_last_updated=date_last_updated,
        message_count=message_count,
        date_last_message=date_last_message,
        last_seen_at=utc_iso(),
    )
