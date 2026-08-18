"""Launchpad: the review queue, bug contents, and PPA verification.

Discovery rests on how the Release Team actually works. A developer files an
FFe and subscribes ~ubuntu-release; the team reviews it, sets a status, and
unsubscribes. So the set of bugs subscribed to ~ubuntu-release *is* the queue of
things awaiting review -- not a heuristic over titles, but the team's own
worklist. An empty result means they are caught up, which is a real and common
state, not a failed query.

That also gives decisions away for free. A bug leaving the queue, combined with
its final status, says what was decided without any guessing at the wording of
comments: Triaged (and onward to Fix Committed/Released) is an approval,
Incomplete means more information was wanted, Won't Fix is a rejection.

A secondary sweep looks for bugs that read like FFes but are not subscribed.
Those are not in anyone's queue, which is worth surfacing as a process gap --
but the system only reports them, and never subscribes anyone to anything.

Accessed through the plain REST API rather than launchpadlib: the JSON is
trivially consumable, launchpadlib's WADL bootstrap timed out repeatedly during
development, and going direct means the timeout and the retry policy are ours.
No credentials are used or needed; everything here is a public, read-only GET.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from ffe.config import Settings
from ffe.models import (
    BugComment,
    BugFacts,
    BugTask,
    DiscoverySignal,
    Fact,
    FactStatus,
    HumanDecision,
)
from ffe.sources.base import SourceContext, http_fact
from ffe.util.hashing import sha256_hex

SOURCE_ID = "launchpad"
IMPACT = "cannot read the bug from Launchpad"

# Bug statuses that keep a request in play. A bug the team has finished with
# has left the queue anyway, so this mostly guards the secondary sweep.
OPEN_STATUSES = ("New", "Incomplete", "Confirmed", "Triaged", "In Progress", "Fix Committed")

# Final status to decision. This is the team's actual convention: approve by
# setting Triaged and unsubscribing, and the bug then moves on to Fix
# Committed and Fix Released as the upload lands.
STATUS_TO_DECISION: dict[str, HumanDecision] = {
    "Triaged": HumanDecision.APPROVED,
    "Fix Committed": HumanDecision.APPROVED,
    "Fix Released": HumanDecision.APPROVED,
    "In Progress": HumanDecision.APPROVED,
    "Incomplete": HumanDecision.NEEDS_INFORMATION,
    "Won't Fix": HumanDecision.REJECTED,
    "Invalid": HumanDecision.REJECTED,
    "Opinion": HumanDecision.REJECTED,
}

# How an FFe announces itself in a title: "[FFe]", "[FFe]:", "FFe:", "FFE -".
# Used only by the secondary sweep; the queue itself needs no pattern matching.
FFE_TITLE = re.compile(r"^\s*\[?\s*FFe\s*\]?\s*[:\-]?\s", re.I)

# Tags are not a usable discovery signal -- five of six real FFe bugs sampled
# on 2026-09-20 carried none at all -- but when present they corroborate.
FFE_TAGS = frozenset({"feature-freeze-exception", "ffe", "upgrade-software-version"})

_PPA_URL = re.compile(
    r"https?://(?:www\.)?launchpad\.net/~([\w.+-]+)/\+archive/(?:ubuntu/)?([\w.+-]+)",
    re.I,
)


def person_name(link: str | None) -> str:
    """Extract a Launchpad user name from an API link."""
    if not link:
        return ""
    return link.rstrip("/").rsplit("/", 1)[-1].lstrip("~")


@dataclass(frozen=True, slots=True)
class BugRef:
    """A bug as it appears in a search result, before the full fetch."""

    id: int
    title: str
    status: str
    target: str
    date_last_updated: str
    web_link: str
    discovered_via: DiscoverySignal

    @property
    def decision(self) -> HumanDecision | None:
        return STATUS_TO_DECISION.get(self.status)


@dataclass(frozen=True, slots=True)
class PpaInfo:
    """What we could establish about a PPA a developer pointed at."""

    owner: str
    name: str
    exists: bool
    url: str
    displayname: str = ""
    description: str = ""


@dataclass
class LaunchpadClient:
    """Read-only access to the parts of Launchpad an FFe review needs."""

    ctx: SourceContext
    settings: Settings
    _team_cache: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)

    @property
    def api(self) -> str:
        return self.settings.launchpad.api_root

    # -- plumbing ----------------------------------------------------------- #

    def _get(self, url: str, *, source: str, ttl: int | None = None) -> Fact[dict[str, Any]]:
        return http_fact(
            self.ctx,
            source_id=f"{SOURCE_ID}.{source}",
            url=url,
            ttl=ttl if ttl is not None else self.settings.cache.launchpad_ttl,
            parse=lambda payload: json.loads(payload),
            impact=IMPACT,
        )

    def _collection(
        self, url: str, *, source: str, ttl: int | None = None, max_pages: int = 20
    ) -> tuple[list[dict[str, Any]], Fact[dict[str, Any]]]:
        """Walk a paginated collection.

        Launchpad returns 75 entries a page and a next_collection_link; there is
        no total_size on these responses, only a link to fetch one, which is not
        worth a round trip. Pages are capped so a runaway collection cannot
        consume the whole run.
        """
        entries: list[dict[str, Any]] = []
        page = self._get(url, source=source, ttl=ttl)
        first = page

        for _ in range(max_pages):
            if not page.ok or page.value is None:
                break
            entries.extend(page.value.get("entries", []))
            following = page.value.get("next_collection_link")
            if not following:
                break
            page = self._get(following, source=source, ttl=ttl)

        return entries, first

    # -- discovery ---------------------------------------------------------- #

    def _search_url(self, **params: str) -> str:
        distro = self.settings.launchpad.distribution
        query = {"ws.op": "searchTasks", "order_by": "-date_last_updated", **params}
        return f"{self.api}/{distro}?{urlencode(query)}"

    @staticmethod
    def _to_refs(entries: list[dict[str, Any]], signal: DiscoverySignal) -> tuple[BugRef, ...]:
        """Collapse bug tasks to bugs.

        searchTasks returns one entry per task, so a bug affecting two packages
        arrives twice -- of 75 tasks in a sampled response, only 60 were
        distinct bugs. The first task seen wins for status, and later ones only
        contribute their target.
        """
        by_id: dict[int, BugRef] = {}
        for entry in entries:
            link = entry.get("bug_link", "")
            try:
                bug_id = int(link.rstrip("/").rsplit("/", 1)[-1])
            except (ValueError, AttributeError):
                continue
            if bug_id in by_id:
                continue
            title = str(entry.get("title", ""))
            # Search titles are prefixed 'Bug #N in pkg (Ubuntu): "real title"'.
            quoted = re.search(r'"(.*)"\s*$', title, re.S)
            by_id[bug_id] = BugRef(
                id=bug_id,
                title=quoted.group(1) if quoted else title,
                status=str(entry.get("status", "")),
                target=str(entry.get("bug_target_name", "")),
                date_last_updated=str(entry.get("date_created", "")),
                web_link=str(entry.get("web_link", "")),
                discovered_via=signal,
            )
        return tuple(by_id.values())

    def review_queue(self) -> Fact[tuple[BugRef, ...]]:
        """Bugs currently subscribed to the Release Team: the actual worklist.

        An empty result is meaningful and expected -- it means the team has
        triaged everything outstanding, which was the live state on 2026-09-20.
        """
        team = self.settings.launchpad.release_team
        url = self._search_url(bug_subscriber=f"{self.api}/~{team}")

        entries, page = self._collection(url, source="queue")
        if not page.ok:
            return Fact(value=None, status=page.status, provenance=page.provenance, note=page.note)

        refs = self._to_refs(entries, DiscoverySignal.RELEASE_TEAM_QUEUE)
        return Fact(
            value=refs,
            status=FactStatus.OK,
            provenance=page.provenance,
            note=(
                f"~{team} is subscribed to no open bugs; the review queue is empty"
                if not refs
                else None
            ),
        )

    def ffe_sweep(self) -> Fact[tuple[BugRef, ...]]:
        """Bugs that read like FFes, whether or not anyone is reviewing them.

        Recall net for the queue, and the source of process-gap reporting: a
        request that looks like an FFe but has nobody subscribed is not going
        to be reviewed, because it is in no one's list.
        """
        url = self._search_url(search_text=self.settings.discovery.search_text)
        entries, page = self._collection(url, source="sweep", max_pages=3)
        if not page.ok:
            return Fact(value=None, status=page.status, provenance=page.provenance, note=page.note)

        pattern = re.compile(self.settings.discovery.title_pattern, re.I)
        refs = tuple(
            ref
            for ref in self._to_refs(entries, DiscoverySignal.TITLE_SWEEP)
            if pattern.search(ref.title) and ref.status in OPEN_STATUSES
        )
        return Fact(value=refs, status=FactStatus.OK, provenance=page.provenance)

    # -- bug contents ------------------------------------------------------- #

    def release_team_members(self) -> frozenset[str]:
        """Who can speak for the Release Team.

        Used to decide whose comments carry authority, and -- more importantly
        -- whose comments may teach the system a precedent.
        """
        return self.team_members(self.settings.launchpad.release_team)

    def team_members(self, team: str) -> frozenset[str]:
        """Participants of a Launchpad team. Empty set if it cannot be read."""
        if not team:
            return frozenset()
        if team in self._team_cache:
            return self._team_cache[team]

        entries, page = self._collection(
            f"{self.api}/~{quote(team)}/participants", source="team", ttl=86_400
        )
        members = frozenset(str(e.get("name", "")) for e in entries if e.get("name"))
        if page.ok:
            self._team_cache[team] = members
        return members

    def fetch_bug(
        self,
        bug_id: int,
        *,
        discovered_via: DiscoverySignal = DiscoverySignal.RELEASE_TEAM_QUEUE,
        release_team: frozenset[str] | None = None,
    ) -> Fact[BugFacts]:
        """Fetch a bug with its tasks, comments and subscribers.

        Everything returned here is untrusted input written by whoever filed or
        commented on the bug. It is evidence about what is being asked for; it
        is never instruction, and never establishes a fact about the archive.
        """
        bug = self._get(f"{self.api}/bugs/{bug_id}", source="bug")
        if not bug.ok or bug.value is None:
            return Fact(value=None, status=bug.status, provenance=bug.provenance, note=bug.note)

        data = bug.value
        team = self.settings.launchpad.release_team
        members = release_team if release_team is not None else self.release_team_members()

        task_entries, _ = self._collection(f"{self.api}/bugs/{bug_id}/bug_tasks", source="tasks")
        tasks = tuple(
            BugTask(
                target=str(t.get("bug_target_name", "")),
                status=str(t.get("status", "")),
                importance=str(t.get("importance", "")),
                assignee=person_name(t.get("assignee_link")) or None,
            )
            for t in task_entries
        )

        message_entries, _ = self._collection(
            f"{self.api}/bugs/{bug_id}/messages", source="messages"
        )
        comments = tuple(
            BugComment(
                index=index,
                author=person_name(m.get("owner_link")),
                date_created=str(m.get("date_created", "")),
                content=str(m.get("content", "")),
                content_sha=sha256_hex(str(m.get("content", ""))),
                url=str(m.get("web_link", "")) or None,
                author_is_release_team=person_name(m.get("owner_link")) in members,
            )
            # Index 0 is the description restated as the first comment.
            for index, m in enumerate(message_entries)
        )

        sub_entries, _ = self._collection(
            f"{self.api}/bugs/{bug_id}/subscriptions", source="subscriptions"
        )
        subscribers = tuple(sorted({person_name(s.get("person_link")) for s in sub_entries} - {""}))

        description = str(data.get("description", ""))
        return Fact(
            value=BugFacts(
                id=int(data.get("id", bug_id)),
                url=str(data.get("web_link", "")),
                title=str(data.get("title", "")),
                description=description,
                description_sha=sha256_hex(description),
                reporter=person_name(data.get("owner_link")),
                tags=tuple(sorted(str(t) for t in data.get("tags", []))),
                date_created=str(data.get("date_created", "")),
                date_last_updated=str(data.get("date_last_updated", "")),
                date_last_message=str(data.get("date_last_message") or "") or None,
                message_count=int(data.get("message_count", 0) or 0),
                tasks=tasks,
                comments=comments,
                subscribers=subscribers,
                release_team_subscribed=team in subscribers,
                discovered_via=discovered_via,
            ),
            status=FactStatus.OK,
            provenance=bug.provenance,
        )

    # -- PPA verification --------------------------------------------------- #

    def verify_ppa(self, owner: str, name: str) -> Fact[PpaInfo]:
        """Confirm a PPA a developer linked to actually exists.

        The point is not to be pedantic. A claim of testing is only evidence if
        the thing claimed can be found, and the system must never report a PPA
        as present on the strength of a URL appearing in prose.
        """
        url = f"{self.api}/~{quote(owner)}/+archive/ubuntu/{quote(name)}"
        fact = self._get(url, source="ppa", ttl=self.settings.cache.launchpad_ttl)
        web_url = f"https://launchpad.net/~{owner}/+archive/ubuntu/{name}"

        if fact.status is FactStatus.UNAVAILABLE and "404" in (fact.note or ""):
            return Fact(
                value=PpaInfo(owner=owner, name=name, exists=False, url=web_url),
                status=FactStatus.OK,
                provenance=fact.provenance,
                note="Launchpad has no such PPA",
            )
        if not fact.ok or fact.value is None:
            return Fact(value=None, status=fact.status, provenance=fact.provenance, note=fact.note)

        return Fact(
            value=PpaInfo(
                owner=owner,
                name=name,
                exists=True,
                url=web_url,
                displayname=str(fact.value.get("displayname", "")),
                description=str(fact.value.get("description") or "")[:500],
            ),
            status=FactStatus.OK,
            provenance=fact.provenance,
        )


def find_ppa_urls(text: str) -> tuple[tuple[str, str], ...]:
    """Extract (owner, name) for every PPA URL in a block of text."""
    return tuple(dict.fromkeys((m.group(1), m.group(2)) for m in _PPA_URL.finditer(text)))


def looks_like_ffe(title: str, tags: tuple[str, ...] = ()) -> bool:
    """Whether a bug announces itself as an FFe.

    Title is the real signal. Tags are checked too but carry almost nothing on
    their own: of six real FFe bugs sampled on 2026-09-20, five had no tags.
    """
    return bool(FFE_TITLE.search(title)) or bool(FFE_TAGS & {t.lower() for t in tags})


def client(ctx: SourceContext) -> LaunchpadClient:
    return LaunchpadClient(ctx=ctx, settings=ctx.settings)
