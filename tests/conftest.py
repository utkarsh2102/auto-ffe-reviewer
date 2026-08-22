"""Shared test fixtures.

The suite is offline by design: no test may open a socket, and none needs an API
key. Anything that would reach the network must be exercised through frozen
fixtures under tests/fixtures/raw/ instead, so the real parsers run against real
bytes without depending on an upstream service being up.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

# Imported here, before the socket guard below can patch anything. `ssl`
# subclasses socket.socket at import time, so a module importing requests
# lazily inside a guarded test would otherwise fail on the patched stub rather
# than on the behaviour under test.
import requests  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to reach the network."""


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all socket use for the whole suite.

    Opt out with @pytest.mark.network for a test that genuinely needs the
    internet; CI excludes those. See test_no_network.py, which asserts this
    guard actually fires -- without that, the guard could silently rot.
    """
    if request.node.get_closest_marker("network"):
        return

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkAccessAttempted(
            "This test tried to open a socket. Use a fixture under "
            "tests/fixtures/raw/ instead, or mark the test @pytest.mark.network."
        )

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


# --------------------------------------------------------------------------- #
# A whole-pipeline harness
#
# Routes every URL the evidence builder touches to a captured fixture, so the
# real parsers run against real upstream bytes end to end without a socket.
# --------------------------------------------------------------------------- #

import re  # noqa: E402
from collections.abc import Mapping  # noqa: E402
from dataclasses import dataclass  # noqa: E402


@dataclass
class Route:
    pattern: str
    target: Path | int | bytes


class FixtureRouter:
    """Serves captured fixtures by URL pattern, first match winning."""

    def __init__(self, routes: list[Route]) -> None:
        self._routes = [(re.compile(r.pattern), r.target) for r in routes]
        self.calls: list[str] = []

    def fetch(self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]):  # type: ignore[no-untyped-def]
        from ffe.util.http import RawResponse

        self.calls.append(url)
        for pattern, target in self._routes:
            if pattern.search(url):
                if isinstance(target, int):
                    return RawResponse(target, b"", {}, url)
                if isinstance(target, bytes):
                    return RawResponse(200, target, {}, url)
                return RawResponse(200, Path(target).read_bytes(), {}, url)
        # Unrouted URLs answer as an empty collection, which keeps each test's
        # route list to the things that test actually cares about.
        return RawResponse(200, b'{"entries": []}', {}, url)


RAW = FIXTURES / "raw"


# Routes covering a healthy, fully-available world. Individual tests override
# entries to simulate a source being down, degraded or absent.
def default_routes(*, seeds: str = "healthy", package: str = "curl") -> list[Route]:
    return [
        Route(
            r"documentation\.ubuntu\.com/release-notes/26\.10", RAW / "schedule/stonking-26.10.html"
        ),
        Route(
            r"documentation\.ubuntu\.com/release-notes/26\.04", RAW / "schedule/resolute-26.04.html"
        ),
        Route(r"ubuntu-seeded-packages/seeded\.json\.gz", RAW / f"seeds/seeded-{seeds}.json.gz"),
        Route(r"/rdepends/v1/\w+/any/src:curl", RAW / "rdepends/src-curl-any.json"),
        Route(r"/rdepends/v1/\w+/source/src:curl", RAW / "rdepends/src-curl-source.json"),
        Route(r"/rdepends/v1/\w+/any/src:kate", RAW / "rdepends/src-kate-any.json"),
        Route(r"/rdepends/v1/\w+/source/src:kate", RAW / "rdepends/src-kate-source.json"),
        Route(r"/rdepends/v1/\w+/any/src:ardour", RAW / "rdepends/src-ardour-any.json"),
        Route(r"/rdepends/v1/\w+/source/src:ardour", RAW / "rdepends/src-ardour-source.json"),
        Route(r"/rdepends/v1/", 404),
        Route(r"getPublishedBinaries", RAW / f"launchpad/published-binaries-{package}.json"),
        Route(r"source_name=curl", RAW / "launchpad/published-sources-curl.json"),
        Route(r"source_name=ardour", RAW / "launchpad/published-sources-ardour.json"),
        Route(r"getPublishedSources", RAW / "launchpad/published-sources-curl.json"),
        Route(r"/bugs/2167691/bug_tasks", RAW / "launchpad/bug-2167691-bug_tasks.json"),
        Route(r"/bugs/2167691/messages", RAW / "launchpad/bug-2167691-messages.json"),
        Route(r"/bugs/2167691/subscriptions", RAW / "launchpad/bug-2167691-subscriptions.json"),
        Route(r"/bugs/2167691$", RAW / "launchpad/bug-2167691.json"),
        Route(r"/bugs/2167756/bug_tasks", RAW / "launchpad/bug-2167756-bug_tasks.json"),
        Route(r"/bugs/2167756$", RAW / "launchpad/bug-2167756.json"),
        Route(r"~ubuntu-release/participants", RAW / "launchpad/ubuntu-release-participants.json"),
        Route(
            r"searchTasks.*ubuntu-release", RAW / "launchpad/searchtasks-ubuntu-release-queue.json"
        ),
        Route(r"searchTasks", RAW / "launchpad/searchtasks-juliank.json"),
    ]


@pytest.fixture
def router() -> FixtureRouter:
    return FixtureRouter(default_routes())


@pytest.fixture
def make_ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Build a SourceContext backed by a fixture router."""

    def _make(routes: list[Route] | None = None, *, offline: bool = False):  # type: ignore[no-untyped-def]
        from ffe.config import load_settings
        from ffe.sources.base import SourceContext
        from ffe.util.cache import Cache
        from ffe.util.http import HttpClient

        transport = FixtureRouter(routes if routes is not None else default_routes())
        ctx = SourceContext(
            http=HttpClient(
                transport=transport,  # type: ignore[arg-type]
                sleep=lambda _s: None,
                min_interval_seconds=0,
            ),
            cache=Cache(tmp_path / "cache"),
            settings=load_settings(Path("/nonexistent.toml"), env={}, offline=offline),
        )
        ctx.transport = transport  # type: ignore[attr-defined]
        return ctx

    return _make
