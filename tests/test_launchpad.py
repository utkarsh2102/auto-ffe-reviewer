"""Tests for Launchpad discovery and bug reading.

Fixtures are live API captures from 2026-09-20, including the state that
matters most: the ~ubuntu-release queue genuinely empty, because the team had
triaged everything outstanding.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from ffe.config import load_settings
from ffe.errors import SourceTimeout
from ffe.models import DiscoverySignal, FactStatus, HumanDecision
from ffe.sources.base import SourceContext
from ffe.sources.launchpad import (
    STATUS_TO_DECISION,
    LaunchpadClient,
    client,
    find_ppa_urls,
    looks_like_ffe,
    person_name,
)
from ffe.util.cache import Cache
from ffe.util.http import HttpClient, RawResponse

LP = Path(__file__).parent / "fixtures" / "raw" / "launchpad"


class RouterTransport:
    """Routes API URLs to fixture files by regex, like a tiny Launchpad."""

    def __init__(self, routes: list[tuple[str, Path | int]]) -> None:
        self.routes = [(re.compile(p), target) for p, target in routes]
        self.calls: list[str] = []

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        self.calls.append(url)
        for pattern, target in self.routes:
            if pattern.search(url):
                if isinstance(target, int):
                    return RawResponse(target, b"", {}, url)
                return RawResponse(200, Path(target).read_bytes(), {}, url)
        # Anything unrouted is an empty collection, which keeps tests focused
        # on the calls they actually care about.
        return RawResponse(200, b'{"entries": []}', {}, url)


def _client(tmp_path: Path, routes: list[tuple[str, Path | int]]) -> LaunchpadClient:
    transport = RouterTransport(routes)
    ctx = SourceContext(
        http=HttpClient(
            transport=transport,  # type: ignore[arg-type]
            sleep=lambda _s: None,
            min_interval_seconds=0,
        ),
        cache=Cache(tmp_path),
        settings=load_settings(Path("/nonexistent.toml"), env={}),
    )
    lp = client(ctx)
    lp._transport = transport  # type: ignore[attr-defined]
    return lp


BUG_ROUTES: list[tuple[str, Path | int]] = [
    (r"/bugs/2167691/bug_tasks", LP / "bug-2167691-bug_tasks.json"),
    (r"/bugs/2167691/messages", LP / "bug-2167691-messages.json"),
    (r"/bugs/2167691/subscriptions", LP / "bug-2167691-subscriptions.json"),
    (r"/bugs/2167691$", LP / "bug-2167691.json"),
    (r"~ubuntu-release/participants", LP / "ubuntu-release-participants.json"),
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_person_name_from_api_link() -> None:
    assert person_name("https://api.launchpad.net/devel/~utkarsh") == "utkarsh"
    assert person_name(None) == ""


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[FFe] upki integration into curl, by default", True),
        ("FFe: resources 51.0", True),
        ("[FFe]: Please sync 2.6.12 from Debian unstable", True),
        ("FFE - hardware enablement", True),
        ("[MIR] upki", False),
        ("Please merge rdma-core from Debian unstable", False),
        ("Discussion of the ffe process", False),  # not at the start
    ],
)
def test_ffe_title_forms_seen_in_the_wild(title: str, expected: bool) -> None:
    assert looks_like_ffe(title) is expected


def test_tags_corroborate_but_are_nearly_useless_alone() -> None:
    """Five of six real FFe bugs sampled on 2026-09-20 carried no tags at all."""
    assert looks_like_ffe("Some title", ("feature-freeze-exception",)) is True
    # The real curl FFe was tagged only ftbfs/update-excuse, so tags alone
    # would have missed it; the title is what found it.
    assert looks_like_ffe("Nothing special", ("ftbfs", "update-excuse")) is False


def test_ppa_urls_are_extracted_and_deduplicated() -> None:
    found = find_ppa_urls(
        "built in https://launchpad.net/~utkarsh/+archive/ubuntu/ffe-test\n"
        "and https://launchpad.net/~other/+archive/ppa-name\n"
        "see https://launchpad.net/~utkarsh/+archive/ubuntu/ffe-test again"
    )
    assert found == (("utkarsh", "ffe-test"), ("other", "ppa-name"))


def test_no_ppa_urls_in_plain_prose() -> None:
    assert find_ppa_urls("I tested this locally and it works fine.") == ()


# --------------------------------------------------------------------------- #
# Queue discovery -- the team's own worklist
# --------------------------------------------------------------------------- #


def test_empty_queue_is_a_real_state_not_a_failure(tmp_path: Path) -> None:
    """The live state on 2026-09-20: the team had triaged everything.

    This must read as "nothing to review", never as a broken query, or the
    tool would cry wolf every time the team got on top of the backlog.
    """
    lp = _client(tmp_path, [(r"searchTasks", LP / "searchtasks-ubuntu-release-queue.json")])
    fact = lp.review_queue()

    assert fact.ok
    assert fact.value == ()
    assert "queue is empty" in (fact.note or "")


def test_queue_query_subscribes_on_the_release_team(tmp_path: Path) -> None:
    lp = _client(tmp_path, [(r"searchTasks", LP / "searchtasks-ubuntu-release-queue.json")])
    lp.review_queue()

    called = lp._transport.calls[0]  # type: ignore[attr-defined]
    assert "searchTasks" in called
    assert "bug_subscriber" in called
    assert "ubuntu-release" in called


def test_tasks_are_collapsed_to_bugs(tmp_path: Path) -> None:
    """searchTasks returns one entry per task; a two-package bug arrives twice.

    The sampled response holds 75 tasks but only 60 distinct bugs, so without
    dedupe a quarter of the queue would be reviewed twice.
    """
    lp = _client(tmp_path, [(r"searchTasks", LP / "searchtasks-juliank.json")])
    fact = lp.review_queue()

    assert fact.ok
    assert fact.value is not None
    ids = [b.id for b in fact.value]
    assert len(ids) == 60
    assert len(ids) == len(set(ids))


def test_title_is_unwrapped_from_the_search_prefix(tmp_path: Path) -> None:
    """Search results wrap titles as 'Bug #N in pkg (Ubuntu): "real title"'."""
    lp = _client(tmp_path, [(r"searchTasks", LP / "searchtasks-juliank.json")])
    fact = lp.review_queue()

    assert fact.value is not None
    first = next(b for b in fact.value if b.id == 2167691)
    assert first.title == "[FFe] upki integration into curl, by default"


def test_queue_entries_are_marked_with_how_they_were_found(tmp_path: Path) -> None:
    lp = _client(tmp_path, [(r"searchTasks", LP / "searchtasks-juliank.json")])
    fact = lp.review_queue()
    assert fact.value is not None
    assert all(b.discovered_via is DiscoverySignal.RELEASE_TEAM_QUEUE for b in fact.value)


def test_sweep_keeps_only_ffe_shaped_open_bugs(tmp_path: Path) -> None:
    """The recall net: FFe-looking bugs, whoever is or is not subscribed."""
    lp = _client(tmp_path, [(r"searchTasks", LP / "searchtasks-juliank.json")])
    fact = lp.ffe_sweep()

    assert fact.ok
    assert fact.value is not None
    titles = [b.title for b in fact.value]
    assert all(looks_like_ffe(t) for t in titles)
    # The MIR bug in the same response is not an FFe and must not be swept in.
    assert not any(t.startswith("[MIR]") for t in titles)
    assert all(b.discovered_via is DiscoverySignal.TITLE_SWEEP for b in fact.value)


def test_unreachable_launchpad_degrades(tmp_path: Path) -> None:
    ctx = SourceContext(
        http=HttpClient(
            transport=_Failing(),  # type: ignore[arg-type]
            sleep=lambda _s: None,
            min_interval_seconds=0,
        ),
        cache=Cache(tmp_path),
        settings=load_settings(Path("/nonexistent.toml"), env={}),
    )
    fact = client(ctx).review_queue()
    assert fact.status is FactStatus.UNAVAILABLE


class _Failing:
    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        raise SourceTimeout("launchpad", "timed out")


# --------------------------------------------------------------------------- #
# Decisions read from status
# --------------------------------------------------------------------------- #


def test_status_maps_to_the_teams_actual_convention() -> None:
    """Approve by setting Triaged; Incomplete asks for more; Won't Fix rejects."""
    assert STATUS_TO_DECISION["Triaged"] is HumanDecision.APPROVED
    assert STATUS_TO_DECISION["Fix Released"] is HumanDecision.APPROVED
    assert STATUS_TO_DECISION["Incomplete"] is HumanDecision.NEEDS_INFORMATION
    assert STATUS_TO_DECISION["Won't Fix"] is HumanDecision.REJECTED
    # An untouched bug says nothing about what anyone decided.
    assert "New" not in STATUS_TO_DECISION
    assert "Confirmed" not in STATUS_TO_DECISION


def test_bug_ref_exposes_its_decision(tmp_path: Path) -> None:
    lp = _client(tmp_path, [(r"searchTasks", LP / "searchtasks-juliank.json")])
    fact = lp.review_queue()
    assert fact.value is not None

    curl_ffe = next(b for b in fact.value if b.id == 2167691)
    assert curl_ffe.status == "Fix Committed"
    assert curl_ffe.decision is HumanDecision.APPROVED

    untouched = next(b for b in fact.value if b.status == "New")
    assert untouched.decision is None


# --------------------------------------------------------------------------- #
# Reading a bug
# --------------------------------------------------------------------------- #


def test_fetch_bug_assembles_the_whole_picture(tmp_path: Path) -> None:
    """LP #2167691: the real curl/upki FFe, in full."""
    fact = _client(tmp_path, BUG_ROUTES).fetch_bug(2167691)

    assert fact.ok
    bug = fact.value
    assert bug is not None
    assert bug.id == 2167691
    assert bug.title == "[FFe] upki integration into curl, by default"
    assert bug.reporter == "juliank"
    assert bug.tags == ("ftbfs", "update-excuse")
    assert "upki finished MIR process" in bug.description
    # Two packages are affected, which is why task-level dedupe matters.
    assert {t.target for t in bug.tasks} == {"curl (Ubuntu)", "upki (Ubuntu)"}


def test_release_team_comments_are_identified(tmp_path: Path) -> None:
    """Whose word counts -- for authority, and for what may teach a precedent."""
    fact = _client(tmp_path, BUG_ROUTES).fetch_bug(2167691)
    bug = fact.value
    assert bug is not None

    approval = next(c for c in bug.comments if "FFe approved" in c.content)
    assert approval.author == "utkarsh"
    assert approval.author_is_release_team is True

    reporter_comment = bug.comments[0]
    assert reporter_comment.author == "juliank"
    assert reporter_comment.author_is_release_team is False


def test_comment_content_is_hashed_for_change_detection(tmp_path: Path) -> None:
    fact = _client(tmp_path, BUG_ROUTES).fetch_bug(2167691)
    bug = fact.value
    assert bug is not None
    assert all(c.content_sha for c in bug.comments)
    assert len({c.content_sha for c in bug.comments}) == len(bug.comments)


def test_release_team_subscription_is_recorded(tmp_path: Path) -> None:
    """This bug was already triaged, so the team is no longer subscribed.

    That is exactly how a handled FFe looks, and why the queue was empty.
    """
    fact = _client(tmp_path, BUG_ROUTES).fetch_bug(2167691)
    bug = fact.value
    assert bug is not None
    assert bug.release_team_subscribed is False
    assert "juliank" in bug.subscribers


def test_release_team_roster_is_read_from_launchpad(tmp_path: Path) -> None:
    members = _client(tmp_path, BUG_ROUTES).release_team_members()
    assert "utkarsh" in members
    assert {"apw", "ginggs", "laney", "paride", "skia"} <= members


def test_roster_is_fetched_once(tmp_path: Path) -> None:
    lp = _client(tmp_path, BUG_ROUTES)
    lp.release_team_members()
    lp.release_team_members()
    fetches = [c for c in lp._transport.calls if "participants" in c]  # type: ignore[attr-defined]
    assert len(fetches) == 1


def test_unreadable_bug_degrades(tmp_path: Path) -> None:
    lp = _client(tmp_path, [(r"/bugs/999", 500)])
    assert lp.fetch_bug(999).status in {FactStatus.UNAVAILABLE, FactStatus.ERROR}


# --------------------------------------------------------------------------- #
# PPA verification
# --------------------------------------------------------------------------- #


def test_existing_ppa_is_confirmed(tmp_path: Path) -> None:
    payload = json.dumps({"displayname": "PPA for tests", "description": "x"}).encode()
    lp = _client(tmp_path, [(r"\+archive/ubuntu/ffe-test", _write(tmp_path, payload))])

    fact = lp.verify_ppa("utkarsh", "ffe-test")
    assert fact.ok
    assert fact.value is not None
    assert fact.value.exists is True
    assert fact.value.url == "https://launchpad.net/~utkarsh/+archive/ubuntu/ffe-test"


def test_missing_ppa_is_reported_as_absent_not_unknown(tmp_path: Path) -> None:
    """A linked PPA that does not exist is a finding, and a definite one.

    The system must never report a PPA as present because a URL appeared in
    prose, and "we could not check" would be a weaker claim than the truth.
    """
    lp = _client(tmp_path, [(r"\+archive", 404)])
    fact = lp.verify_ppa("someone", "nonexistent")

    assert fact.ok
    assert fact.value is not None
    assert fact.value.exists is False
    assert "no such PPA" in (fact.note or "")


def _write(tmp_path: Path, payload: bytes) -> Path:
    path = tmp_path / "ppa.json"
    path.write_bytes(payload)
    return path
