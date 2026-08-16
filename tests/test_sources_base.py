"""Tests for the source contract.

The invariant under test is the one the whole pipeline leans on: a source never
raises. Every upstream failure mode has to come back as a Fact the rest of the
system can reason about.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ffe.config import load_settings
from ffe.errors import RateLimited, SourceError, SourceTimeout
from ffe.models import CacheState, FactStatus
from ffe.sources.base import (
    SourceContext,
    computed,
    http_fact,
    not_applicable,
    subprocess_fact,
)
from ffe.util.cache import Cache
from ffe.util.clock import frozen_at
from ffe.util.http import HttpClient, RawResponse


class StubTransport:
    """Answers with a queued result, or the same one every time."""

    def __init__(self, *results: RawResponse | Exception, repeat: bool = False) -> None:
        self._results = list(results)
        self._repeat = repeat
        self.calls = 0

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        self.calls += 1
        result = self._results[0] if self._repeat else self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ctx(tmp_path: Path, transport: object, *, offline: bool = False) -> SourceContext:
    settings = load_settings(Path("/nonexistent.toml"), env={}, offline=offline)
    return SourceContext(
        http=HttpClient(
            transport=transport,  # type: ignore[arg-type]
            sleep=lambda _s: None,
            min_interval_seconds=0,
        ),
        cache=Cache(tmp_path),
        settings=settings,
    )


def _json_body(text: bytes) -> RawResponse:
    return RawResponse(200, text, {"ETag": 'W/"v1"'}, "https://example.test/x")


URL = "https://example.test/data.json"


# --------------------------------------------------------------------------- #
# Happy path and caching
# --------------------------------------------------------------------------- #


def test_successful_fetch_carries_provenance(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, StubTransport(_json_body(b"149")))
    fact = http_fact(
        ctx, source_id="rdepends", url=URL, ttl=3600, parse=int, impact="blast radius unknown"
    )

    assert fact.ok
    assert fact.value == 149
    assert fact.provenance.source_id == "rdepends"
    assert fact.provenance.locator == URL
    assert fact.provenance.cache is CacheState.FRESH
    # The raw bytes are retained so a reviewer can check the claim.
    assert fact.provenance.raw_ref is not None
    assert ctx.cache.load_raw(fact.provenance.raw_ref) == b"149"


def test_second_call_within_ttl_uses_the_cache(tmp_path: Path) -> None:
    transport = StubTransport(_json_body(b"149"), repeat=True)
    ctx = _ctx(tmp_path, transport)
    kwargs = {"source_id": "rdepends", "url": URL, "ttl": 3600, "parse": int, "impact": "x"}

    http_fact(ctx, **kwargs)  # type: ignore[arg-type]
    second = http_fact(ctx, **kwargs)  # type: ignore[arg-type]

    assert transport.calls == 1, "cached value should not have been refetched"
    assert second.provenance.cache is CacheState.CACHED
    assert second.value == 149


def test_expired_cache_triggers_a_refetch(tmp_path: Path) -> None:
    transport = StubTransport(_json_body(b"149"), _json_body(b"151"))
    ctx = _ctx(tmp_path, transport)
    kwargs = {"source_id": "rdepends", "url": URL, "ttl": 3600, "parse": int, "impact": "x"}

    with frozen_at("2026-09-20T12:00:00Z"):
        http_fact(ctx, **kwargs)  # type: ignore[arg-type]
    with frozen_at("2026-09-20T18:00:00Z"):
        refreshed = http_fact(ctx, **kwargs)  # type: ignore[arg-type]

    assert transport.calls == 2
    assert refreshed.value == 151


# --------------------------------------------------------------------------- #
# Degradation -- the point of the whole module
# --------------------------------------------------------------------------- #


def test_timeout_without_a_cached_value_is_unavailable_not_an_exception(tmp_path: Path) -> None:
    """The seeded-in-ubuntu hang, as the system should experience it."""
    ctx = _ctx(tmp_path, StubTransport(SourceTimeout("seeds", "timed out"), repeat=True))

    fact = http_fact(
        ctx,
        source_id="seeds",
        url=URL,
        ttl=3600,
        parse=int,
        impact="cannot tell which images ship this package",
    )

    assert fact.status is FactStatus.UNAVAILABLE
    assert fact.value is None
    assert "timed out" in (fact.note or "")
    # And it is recorded for the dashboard, with what it costs us.
    assert len(ctx.unavailable) == 1
    assert ctx.unavailable[0].impact == "cannot tell which images ship this package"


def test_upstream_failure_falls_back_to_stale_rather_than_unknown(tmp_path: Path) -> None:
    """A brief wobble must not rewrite the evidence or churn the fingerprint."""
    ctx = _ctx(tmp_path, StubTransport(_json_body(b"149")))
    kwargs = {"source_id": "rdepends", "url": URL, "ttl": 60, "parse": int, "impact": "x"}

    with frozen_at("2026-09-20T12:00:00Z"):
        http_fact(ctx, **kwargs)  # type: ignore[arg-type]

    ctx.http = HttpClient(
        transport=StubTransport(SourceTimeout("rdepends", "down"), repeat=True),  # type: ignore[arg-type]
        sleep=lambda _s: None,
        min_interval_seconds=0,
    )
    with frozen_at("2026-09-20T18:00:00Z"):
        fact = http_fact(ctx, **kwargs)  # type: ignore[arg-type]

    assert fact.ok, "should have served the last good value"
    assert fact.value == 149
    assert fact.provenance.cache is CacheState.STALE
    assert "6h old" in (fact.note or "")
    # Nothing is reported unavailable, because we still have a usable answer.
    assert ctx.unavailable == []


def test_rate_limiting_degrades_gracefully(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, StubTransport(RateLimited("lp", "backoff 900s", 900), repeat=True))
    fact = http_fact(ctx, source_id="lp", url=URL, ttl=60, parse=int, impact="no bug data")

    assert fact.status is FactStatus.UNAVAILABLE
    assert ctx.unavailable[0].source_id == "lp"


def test_unparseable_response_is_an_error_not_a_crash(tmp_path: Path) -> None:
    """Upstream formats change without notice; one fact degrades, the run lives."""
    ctx = _ctx(tmp_path, StubTransport(_json_body(b"this is not a number")))
    fact = http_fact(ctx, source_id="rdepends", url=URL, ttl=60, parse=int, impact="x")

    assert fact.status is FactStatus.ERROR
    assert fact.value is None
    # The raw response is still kept, so the parser can be fixed against it.
    assert fact.provenance.raw_ref is not None


def test_404_can_be_a_real_answer(tmp_path: Path) -> None:
    """No reverse-dependency record means no reverse dependencies."""
    ctx = _ctx(tmp_path, StubTransport(RawResponse(404, b"", {}, URL)))
    fact = http_fact(
        ctx,
        source_id="rdepends",
        url=URL,
        ttl=60,
        parse=int,
        impact="x",
        on_not_found=lambda: 0,
    )

    assert fact.ok
    assert fact.value == 0
    assert ctx.unavailable == []


def test_404_without_a_handler_is_unavailable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, StubTransport(RawResponse(404, b"", {}, URL)))
    fact = http_fact(ctx, source_id="seeds", url=URL, ttl=60, parse=int, impact="x")
    assert fact.status is FactStatus.UNAVAILABLE


def test_offline_mode_opens_no_connections(tmp_path: Path) -> None:
    transport = StubTransport(_json_body(b"149"), repeat=True)
    ctx = _ctx(tmp_path, transport, offline=True)

    fact = http_fact(ctx, source_id="seeds", url=URL, ttl=60, parse=int, impact="x")

    assert transport.calls == 0
    assert fact.status is FactStatus.UNAVAILABLE


def test_offline_mode_still_serves_cache(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, StubTransport(_json_body(b"149")))
    kwargs = {"source_id": "seeds", "url": URL, "ttl": 3600, "parse": int, "impact": "x"}
    http_fact(ctx, **kwargs)  # type: ignore[arg-type]

    ctx.settings = load_settings(Path("/nonexistent.toml"), env={}, offline=True)
    assert http_fact(ctx, **kwargs).value == 149  # type: ignore[arg-type]


def test_repeated_failures_are_recorded_once(tmp_path: Path) -> None:
    """The dashboard should say a source is down, not say it fifty times."""
    ctx = _ctx(tmp_path, StubTransport(SourceError("seeds", "down"), repeat=True))
    for _ in range(3):
        http_fact(ctx, source_id="seeds", url=URL, ttl=60, parse=int, impact="x")
    assert len(ctx.unavailable) == 1


# --------------------------------------------------------------------------- #
# Other fact constructors
# --------------------------------------------------------------------------- #


def test_computed_facts_still_carry_provenance(tmp_path: Path) -> None:
    fact = computed("calendar", "freeze_phase", "POST_UI_FREEZE")
    assert fact.ok
    assert fact.provenance.locator == "computed:freeze_phase"


def test_not_applicable_is_distinct_from_unavailable(tmp_path: Path) -> None:
    """A new package genuinely has no reverse dependencies; that is an answer."""
    fact = not_applicable("rdepends", "n/a", "package is not in the archive yet")
    assert fact.status is FactStatus.NOT_APPLICABLE
    assert fact.status is not FactStatus.UNAVAILABLE
    assert fact.ok is False


def test_subprocess_fact_captures_output(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, StubTransport())
    fact = subprocess_fact(
        ctx,
        source_id="crosscheck.seeded",
        argv=["echo", "kate is seeded in:"],
        parse=lambda r: r.stdout.strip(),
        impact="cross-check skipped",
    )
    assert fact.ok
    assert fact.value == "kate is seeded in:"
    assert fact.provenance.locator == "echo 'kate is seeded in:'"


def test_subprocess_timeout_degrades(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, StubTransport())
    fact = subprocess_fact(
        ctx,
        source_id="crosscheck.seeded",
        argv=["sleep", "30"],
        parse=lambda r: r.stdout,
        impact="cross-check skipped",
        timeout=1,
    )
    assert fact.status is FactStatus.UNAVAILABLE
    assert ctx.unavailable[0].source_id == "crosscheck.seeded"


def test_missing_crosscheck_binary_degrades(tmp_path: Path) -> None:
    """A runner without ubuntu-dev-tools loses a cross-check, not the run."""
    ctx = _ctx(tmp_path, StubTransport())
    fact = subprocess_fact(
        ctx,
        source_id="crosscheck.seeded",
        argv=["ffe-not-a-real-binary"],
        parse=lambda r: r.stdout,
        impact="cross-check skipped",
    )
    assert fact.status is FactStatus.UNAVAILABLE
