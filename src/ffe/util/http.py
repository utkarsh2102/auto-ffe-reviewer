"""HTTP with deadlines that are actually enforced.

This layer exists because of a concrete failure. `seeded-in-ubuntu` fetches its
index with `urllib.request.urlretrieve()` and `reverse-depends` uses
`httplib2.Http().request()`; neither passes a timeout, so both inherit a socket
default of None. During development `seeded-in-ubuntu -b bash` hung outright
while the identical URL returned in well under a second under `curl --max-time`.
In a scheduled job an unbounded hang is not a slow run, it is a job that burns
its entire six-hour limit and reports nothing.

So: every request here carries an explicit (connect, read) deadline, retries a
bounded number of times with jittered backoff, honours Retry-After rather than
guessing, and trips a per-host breaker when an upstream asks for longer than the
run has left. Transport is injectable so tests replay recorded bytes through the
real parsers without opening a socket.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ffe.errors import RateLimited, SourceError, SourceTimeout

# Statuses worth trying again: transient upstream trouble, not a wrong request.
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504, 408})
_RATE_LIMITED_STATUS = frozenset({429, 503})

# Cap on how long we will honour an upstream's Retry-After before giving up on
# the source for this run. Launchpad has been observed asking for 900s.
MAX_HONOURED_RETRY_AFTER = 120


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    content: bytes
    headers: Mapping[str, str]
    url: str
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header(self, name: str, default: str = "") -> str:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default


class Transport(Protocol):
    """Minimal fetch surface, so tests can supply recorded bytes."""

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse: ...


class RequestsTransport:
    """Real network access, via a pooled requests session."""

    def __init__(self, user_agent: str) -> None:
        import requests

        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._requests = requests

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        started = time.monotonic()
        try:
            response = self._session.get(
                url, headers=dict(headers), timeout=timeout, allow_redirects=True
            )
        except self._requests.Timeout as exc:
            raise SourceTimeout("http", f"{url} timed out after {timeout}s") from exc
        except self._requests.RequestException as exc:
            raise SourceError("http", f"{url} failed: {exc}") from exc

        return RawResponse(
            status=response.status_code,
            content=response.content,
            headers=dict(response.headers),
            url=response.url,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


class FileTransport:
    """Replay recorded responses. Test-only.

    Mapping real URLs to fixture files means the production parsers run against
    real upstream bytes, so a change in an upstream's HTML or JSON shape is
    caught by the suite rather than in production.
    """

    def __init__(self, mapping: Mapping[str, Path | int], *, default_status: int = 200) -> None:
        # A value may be a fixture path, or a bare status code to simulate a
        # failing upstream without inventing a body for it.
        self._mapping = dict(mapping)
        self._default_status = default_status
        self.calls: list[str] = []

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        self.calls.append(url)
        for prefix, path in self._mapping.items():
            if url.startswith(prefix):
                if isinstance(path, int):  # a bare status code, for error paths
                    return RawResponse(status=path, content=b"", headers={}, url=url)
                return RawResponse(
                    status=self._default_status,
                    content=Path(path).read_bytes(),
                    headers={},
                    url=url,
                )
        raise SourceError("http", f"no fixture registered for {url}")


def _host_of(url: str) -> str:
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]


def _parse_retry_after(value: str) -> int | None:
    """Parse Retry-After. Only the delta-seconds form; HTTP-date is rare here."""
    try:
        seconds = int(value.strip())
    except (ValueError, AttributeError):
        return None
    return max(0, seconds)


@dataclass
class HttpClient:
    """Bounded, polite HTTP access to evidence sources."""

    transport: Transport
    default_timeout: tuple[int, int] = (5, 30)
    max_attempts: int = 3
    min_interval_seconds: float = 0.5
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _last_request_at: dict[str, float] = field(default_factory=dict, repr=False)
    _tripped: dict[str, str] = field(default_factory=dict, repr=False)

    def _throttle(self, host: str) -> None:
        """Keep at least `min_interval_seconds` between requests to one host.

        Launchpad in particular will rate-limit an enthusiastic client, and
        being a good citizen is cheaper than being throttled for 15 minutes.
        """
        previous = self._last_request_at.get(host)
        if previous is not None:
            wait = self.min_interval_seconds - (self.monotonic() - previous)
            if wait > 0:
                self.sleep(wait)
        self._last_request_at[host] = self.monotonic()

    def get(
        self,
        url: str,
        *,
        source_id: str,
        timeout: tuple[int, int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> RawResponse:
        """Fetch `url`, retrying transient failures within a bounded budget.

        Returns 2xx and 404 responses to the caller: a 404 is frequently
        meaningful evidence rather than an error. Everything else either
        succeeds after a retry or raises a SourceError for @source to convert
        into an UNAVAILABLE fact.
        """
        host = _host_of(url)
        if host in self._tripped:
            raise SourceError(
                source_id, f"{host} circuit open: {self._tripped[host]}", retryable=False
            )

        timeout = timeout or self.default_timeout
        request_headers = dict(headers or {})
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            self._throttle(host)
            try:
                response = self.transport.fetch(url, headers=request_headers, timeout=timeout)
            except (SourceTimeout, SourceError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                self._backoff(attempt)
                continue

            if response.ok or response.status == 404:
                return response

            if response.status in _RATE_LIMITED_STATUS:
                requested = _parse_retry_after(response.header("Retry-After"))
                if requested is not None and requested > MAX_HONOURED_RETRY_AFTER:
                    # Asking us to wait longer than the run has left. Stop
                    # bothering this host rather than stalling every source
                    # queued behind it.
                    self._tripped[host] = f"asked for {requested}s backoff"
                    raise RateLimited(
                        source_id,
                        f"{host} asked for {requested}s backoff; skipping for this run",
                        requested,
                    )
                if attempt < self.max_attempts:
                    self.sleep(
                        requested if requested is not None else self._backoff_seconds(attempt)
                    )
                    continue
                raise RateLimited(source_id, f"{host} rate-limited us", requested or 0)

            if response.status in _RETRYABLE_STATUS and attempt < self.max_attempts:
                self._backoff(attempt)
                continue

            raise SourceError(
                source_id,
                f"{url} returned HTTP {response.status}",
                retryable=response.status in _RETRYABLE_STATUS,
            )

        raise SourceTimeout(
            source_id, f"{url} failed after {self.max_attempts} attempts: {last_error}"
        )

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with jitter, so parallel runs do not synchronise."""
        return (2 ** (attempt - 1)) + random.uniform(0, 0.5)

    def _backoff(self, attempt: int) -> None:
        self.sleep(self._backoff_seconds(attempt))
