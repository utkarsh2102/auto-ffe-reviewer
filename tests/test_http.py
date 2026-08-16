"""Tests for the bounded HTTP client.

The behaviour under test is mostly about failure: what happens when an upstream
is slow, broken, or asking us to go away. That is the path that decides whether
a scheduled run finishes or hangs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from ffe.errors import RateLimited, SourceError, SourceTimeout
from ffe.util.http import FileTransport, HttpClient, RawResponse


class ScriptedTransport:
    """Returns a queued sequence of responses or raises queued exceptions."""

    def __init__(self, *results: RawResponse | Exception) -> None:
        self._results = list(results)
        self.calls = 0

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        self.calls += 1
        result = self._results.pop(0) if self._results else RawResponse(200, b"", {}, url)
        if isinstance(result, Exception):
            raise result
        return result


def _client(transport: object, **kwargs: object) -> HttpClient:
    """A client whose sleeps are instant, so retry tests stay fast."""
    return HttpClient(
        transport=transport,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        min_interval_seconds=0,
        **kwargs,  # type: ignore[arg-type]
    )


def _ok(body: bytes = b"data") -> RawResponse:
    return RawResponse(200, body, {}, "https://example.test/x")


# --------------------------------------------------------------------------- #
# Success and meaningful non-success
# --------------------------------------------------------------------------- #


def test_successful_get_returns_body() -> None:
    client = _client(ScriptedTransport(_ok(b"hello")))
    assert client.get("https://example.test/x", source_id="t").content == b"hello"


def test_404_is_returned_not_raised() -> None:
    """A 404 is frequently evidence, not an error -- the caller decides."""
    transport = ScriptedTransport(RawResponse(404, b"", {}, "https://example.test/x"))
    response = _client(transport).get("https://example.test/x", source_id="t")
    assert response.status == 404
    assert transport.calls == 1  # not retried


def test_client_error_is_not_retried() -> None:
    transport = ScriptedTransport(RawResponse(400, b"", {}, "https://example.test/x"))
    with pytest.raises(SourceError):
        _client(transport).get("https://example.test/x", source_id="t")
    assert transport.calls == 1


# --------------------------------------------------------------------------- #
# Retry behaviour
# --------------------------------------------------------------------------- #


def test_transient_server_error_is_retried_then_succeeds() -> None:
    transport = ScriptedTransport(
        RawResponse(502, b"", {}, "u"),
        RawResponse(500, b"", {}, "u"),
        _ok(b"finally"),
    )
    assert _client(transport).get("https://example.test/x", source_id="t").content == b"finally"
    assert transport.calls == 3


def test_retries_are_bounded() -> None:
    """The property that matters: we always stop, we never spin."""
    transport = ScriptedTransport(*[RawResponse(500, b"", {}, "u") for _ in range(10)])
    with pytest.raises(SourceError):
        _client(transport).get("https://example.test/x", source_id="t")
    assert transport.calls == 3


def test_timeout_is_retried_then_surfaces_as_timeout() -> None:
    transport = ScriptedTransport(
        SourceTimeout("http", "slow"),
        SourceTimeout("http", "slow"),
        SourceTimeout("http", "slow"),
    )
    with pytest.raises(SourceTimeout):
        _client(transport).get("https://example.test/x", source_id="seeds")
    assert transport.calls == 3


def test_timeout_then_success_recovers() -> None:
    transport = ScriptedTransport(SourceTimeout("http", "slow"), _ok(b"recovered"))
    assert _client(transport).get("https://example.test/x", source_id="t").content == b"recovered"


# --------------------------------------------------------------------------- #
# Rate limiting -- observed live from Launchpad as 503 + Retry-After: 900
# --------------------------------------------------------------------------- #


def test_short_retry_after_is_honoured_and_waited_out() -> None:
    slept: list[float] = []
    transport = ScriptedTransport(
        RawResponse(503, b"", {"Retry-After": "2"}, "u"),
        _ok(b"after waiting"),
    )
    client = HttpClient(transport=transport, sleep=slept.append, min_interval_seconds=0)  # type: ignore[arg-type]

    assert client.get("https://example.test/x", source_id="lp").content == b"after waiting"
    # We waited exactly what was asked, rather than guessing a backoff.
    assert 2 in slept


def test_excessive_retry_after_trips_the_breaker() -> None:
    """Launchpad asking for 900s must not stall every source behind it."""
    transport = ScriptedTransport(RawResponse(503, b"", {"Retry-After": "900"}, "u"))
    client = _client(transport)

    with pytest.raises(RateLimited) as caught:
        client.get("https://api.launchpad.net/devel/bugs/1", source_id="lp")
    assert caught.value.retry_after_seconds == 900

    # The host is now skipped outright rather than retried for the rest of the run.
    with pytest.raises(SourceError, match="circuit open"):
        client.get("https://api.launchpad.net/devel/bugs/2", source_id="lp")
    assert transport.calls == 1


def test_breaker_is_per_host() -> None:
    transport = ScriptedTransport(
        RawResponse(503, b"", {"Retry-After": "900"}, "u"),
        _ok(b"other host is fine"),
    )
    client = _client(transport)
    with pytest.raises(RateLimited):
        client.get("https://api.launchpad.net/x", source_id="lp")

    assert client.get("https://qa.ubuntuwire.org/y", source_id="seeds").content == (
        b"other host is fine"
    )


# --------------------------------------------------------------------------- #
# Politeness and fixtures
# --------------------------------------------------------------------------- #


def test_requests_to_one_host_are_spaced_out() -> None:
    slept: list[float] = []
    # _throttle reads the clock twice per request: once to size the wait, once
    # after sleeping to record when the request actually went out.
    #   req 1: record 0.0
    #   req 2: now 0.1 -> only 0.1s elapsed, so wait 0.9; record 0.1
    #   req 3: now 5.0 -> plenty elapsed, no wait; record 5.0
    ticks = iter([0.0, 0.1, 0.1, 5.0, 5.0])
    client = HttpClient(
        transport=ScriptedTransport(_ok(), _ok(), _ok()),  # type: ignore[arg-type]
        sleep=slept.append,
        monotonic=lambda: next(ticks),
        min_interval_seconds=1.0,
    )
    for _ in range(3):
        client.get("https://example.test/x", source_id="t")

    assert slept == [pytest.approx(0.9)]


def test_file_transport_replays_recorded_bytes(tmp_path: Path) -> None:
    fixture = tmp_path / "seeded.json"
    fixture.write_bytes(b'{"kate": [["kubuntu", "daily-live"]]}')
    client = _client(FileTransport({"http://qa.ubuntuwire.org/": fixture}))

    response = client.get("http://qa.ubuntuwire.org/ubuntu-seeded-packages/x", source_id="seeds")
    assert b"kate" in response.content


def test_file_transport_can_simulate_a_failing_upstream() -> None:
    client = _client(FileTransport({"http://qa.ubuntuwire.org/": 503}))
    with pytest.raises((SourceError, SourceTimeout)):
        client.get("http://qa.ubuntuwire.org/x", source_id="seeds")


def test_unregistered_url_is_an_error_not_a_silent_empty() -> None:
    client = _client(FileTransport({}))
    with pytest.raises((SourceError, SourceTimeout)):
        client.get("https://unexpected.test/x", source_id="t")


def test_header_lookup_is_case_insensitive() -> None:
    response = RawResponse(200, b"", {"Content-Type": "application/json"}, "u")
    assert response.header("content-type") == "application/json"
    assert response.header("missing", "fallback") == "fallback"
