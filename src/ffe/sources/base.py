"""The contract every evidence source honours.

One rule dominates: **a source never raises**. An FFe review that is missing
the reverse-dependency count is still worth reading; a run that died halfway
through because ubuntuwire had a bad minute is worth nothing. So every failure
becomes a Fact with a non-OK status, carrying a note saying what went wrong,
and the run continues.

That turns degradation into something the rest of the system can reason about.
An UNAVAILABLE fact lowers the deterministic confidence ceiling, is listed on
the dashboard, and -- when there is no prior value to fall back on -- enters the
change-detection fingerprint, so the review is redone once the source recovers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from ffe.config import Settings
from ffe.errors import RateLimited, SourceError, SourceTimeout
from ffe.models import (
    CacheState,
    Fact,
    FactStatus,
    Provenance,
    SourceMethod,
    Unavailable,
)
from ffe.util.cache import Cache
from ffe.util.clock import utc_iso
from ffe.util.http import HttpClient
from ffe.util.proc import CommandResult
from ffe.util.proc import run as run_command

T = TypeVar("T")


@dataclass
class SourceContext:
    """Shared services for one run, plus a record of what we could not consult."""

    http: HttpClient
    cache: Cache
    settings: Settings
    unavailable: list[Unavailable] = field(default_factory=list)

    def record_unavailable(self, source_id: str, reason: str, impact: str) -> None:
        """Note a source we could not use, and what that blinds us to.

        `impact` is written for a Release Team member reading the dashboard, not
        for a developer reading a log: it says which judgement is now weaker.
        """
        if any(u.source_id == source_id and u.reason == reason for u in self.unavailable):
            return
        self.unavailable.append(Unavailable(source_id=source_id, reason=reason, impact=impact))


def _provenance(
    source_id: str,
    method: SourceMethod,
    locator: str,
    *,
    cache: CacheState = CacheState.NONE,
    duration_ms: int = 0,
    raw_ref: str | None = None,
    raw_truncated: bool = False,
) -> Provenance:
    return Provenance(
        source_id=source_id,
        method=method,
        locator=locator,
        checked_at=utc_iso(),
        raw_ref=raw_ref,
        cache=cache,
        duration_ms=duration_ms,
        raw_truncated=raw_truncated,
    )


def computed(source_id: str, description: str, value: T) -> Fact[T]:
    """A fact derived from other facts rather than fetched.

    Still carries provenance, so "phase: POST_UI_FREEZE" can be traced back to
    the calculation that produced it rather than appearing from nowhere.
    """
    return Fact(
        value=value,
        status=FactStatus.OK,
        provenance=_provenance(source_id, SourceMethod.COMPUTED, f"computed:{description}"),
    )


def not_applicable(source_id: str, locator: str, why: str) -> Fact[T]:
    """The question does not apply here -- distinct from being unable to answer it.

    A package that is not yet in the archive has no reverse dependencies. That
    is a real answer, and it must not be confused with a failed lookup, which
    would wrongly lower our confidence.
    """
    return Fact(
        value=None,
        status=FactStatus.NOT_APPLICABLE,
        provenance=_provenance(source_id, SourceMethod.COMPUTED, locator),
        note=why,
    )


def unavailable(source_id: str, locator: str, reason: str) -> Fact[T]:
    return Fact(
        value=None,
        status=FactStatus.UNAVAILABLE,
        provenance=_provenance(source_id, SourceMethod.COMPUTED, locator),
        note=reason,
    )


def http_fact(
    ctx: SourceContext,
    *,
    source_id: str,
    url: str,
    ttl: int,
    parse: Callable[[bytes], T],
    impact: str,
    timeout: tuple[int, int] | None = None,
    cache_key: str | None = None,
    on_not_found: Callable[[], T] | None = None,
) -> Fact[T]:
    """Fetch `url`, parse it, and wrap the result with provenance.

    The order of preference is: a fresh cache entry, then a live fetch, then a
    stale cache entry, then UNAVAILABLE. Falling back to stale before giving up
    is what stops a brief upstream wobble from rewriting the evidence.

    `on_not_found` handles the case where a 404 is itself the answer -- for
    example, a package with no reverse-dependency data is a package with no
    reverse dependencies.
    """
    key = cache_key or url
    timeout = timeout or ctx.settings.timeouts.default

    cached = ctx.cache.get(key)
    if cached is not None and cached.is_fresh(ttl):
        parsed = _parse_or_none(parse, cached.data)
        if parsed is not None:
            ref, truncated = ctx.cache.store_raw(
                cached.data, max_bytes=ctx.settings.cache.raw_blob_max_bytes
            )
            return Fact(
                value=parsed,
                status=FactStatus.OK,
                provenance=_provenance(
                    source_id,
                    SourceMethod.HTTP_GET,
                    url,
                    cache=CacheState.CACHED,
                    raw_ref=ref,
                    raw_truncated=truncated,
                ),
            )

    if ctx.settings.offline:
        return _stale_or_unavailable(
            ctx, source_id, url, key, parse, impact, reason="offline mode; no cached value"
        )

    try:
        response = ctx.http.get(
            url,
            source_id=source_id,
            timeout=timeout,
            headers=ctx.cache.conditional_headers(key),
        )
    except (SourceTimeout, RateLimited, SourceError) as exc:
        return _stale_or_unavailable(ctx, source_id, url, key, parse, impact, reason=str(exc))

    if response.status == 404:
        if on_not_found is not None:
            return Fact(
                value=on_not_found(),
                status=FactStatus.OK,
                provenance=_provenance(
                    source_id, SourceMethod.HTTP_GET, url, duration_ms=response.elapsed_ms
                ),
                note="upstream returned 404, which for this source means 'nothing recorded'",
            )
        return _stale_or_unavailable(
            ctx, source_id, url, key, parse, impact, reason="upstream returned HTTP 404"
        )

    ctx.cache.put(
        key,
        response.content,
        etag=response.header("ETag") or None,
        last_modified=response.header("Last-Modified") or None,
    )
    ref, truncated = ctx.cache.store_raw(
        response.content, max_bytes=ctx.settings.cache.raw_blob_max_bytes
    )

    parsed = _parse_or_none(parse, response.content)
    if parsed is None:
        ctx.record_unavailable(source_id, "response could not be parsed", impact)
        return Fact(
            value=None,
            status=FactStatus.ERROR,
            provenance=_provenance(
                source_id,
                SourceMethod.HTTP_GET,
                url,
                cache=CacheState.FRESH,
                duration_ms=response.elapsed_ms,
                raw_ref=ref,
                raw_truncated=truncated,
            ),
            note="upstream responded, but the response could not be parsed",
        )

    return Fact(
        value=parsed,
        status=FactStatus.OK,
        provenance=_provenance(
            source_id,
            SourceMethod.HTTP_GET,
            url,
            cache=CacheState.FRESH,
            duration_ms=response.elapsed_ms,
            raw_ref=ref,
            raw_truncated=truncated,
        ),
    )


def subprocess_fact(
    ctx: SourceContext,
    *,
    source_id: str,
    argv: list[str],
    parse: Callable[[CommandResult], T],
    impact: str,
    timeout: int | None = None,
) -> Fact[T]:
    """Run a command and wrap its output, retaining stdout for provenance.

    Used only for the optional cross-check against the real Ubuntu CLIs; the
    hot path fetches the same upstream data directly, where the deadline is
    ours to set. See docs/security.md.
    """
    timeout = timeout or ctx.settings.timeouts.subprocess
    locator = " ".join(argv)

    try:
        result = run_command(argv, timeout=timeout, source_id=source_id)
    except SourceTimeout as exc:
        ctx.record_unavailable(source_id, str(exc), impact)
        return unavailable(source_id, locator, str(exc))

    ref, truncated = ctx.cache.store_raw(
        (result.stdout + result.stderr).encode("utf-8", "replace"),
        max_bytes=ctx.settings.cache.raw_blob_max_bytes,
    )
    parsed = _parse_or_none(parse, result)
    if parsed is None:
        ctx.record_unavailable(source_id, "command output could not be parsed", impact)
        return Fact(
            value=None,
            status=FactStatus.ERROR,
            provenance=_provenance(
                source_id,
                SourceMethod.SUBPROCESS,
                result.command,
                duration_ms=result.duration_ms,
                raw_ref=ref,
                raw_truncated=truncated,
            ),
            note=f"exit {result.returncode}; output could not be parsed",
        )

    return Fact(
        value=parsed,
        status=FactStatus.OK,
        provenance=_provenance(
            source_id,
            SourceMethod.SUBPROCESS,
            result.command,
            duration_ms=result.duration_ms,
            raw_ref=ref,
            raw_truncated=truncated,
        ),
    )


def _parse_or_none(parse: Callable[..., T], payload: object) -> T | None:
    """Run a parser, treating any failure as 'could not parse'.

    Broad by design: upstream formats change without notice, and a parser
    blowing up must degrade one fact rather than end the run.
    """
    try:
        return parse(payload)  # type: ignore[arg-type]
    except Exception:  # deliberately total -- see the docstring
        return None


def _stale_or_unavailable(
    ctx: SourceContext,
    source_id: str,
    url: str,
    key: str,
    parse: Callable[[bytes], T],
    impact: str,
    *,
    reason: str,
) -> Fact[T]:
    """Fall back to the last good value, or admit we do not know.

    Serving stale data is the better failure here. The alternative -- declaring
    the fact unknown because an upstream had a bad minute -- would both weaken
    the review and churn the fingerprint into re-running the model.
    """
    cached = ctx.cache.get(key)
    if cached is not None:
        parsed = _parse_or_none(parse, cached.data)
        if parsed is not None:
            ref, truncated = ctx.cache.store_raw(
                cached.data, max_bytes=ctx.settings.cache.raw_blob_max_bytes
            )
            age_hours = cached.age_seconds // 3600
            return Fact(
                value=parsed,
                status=FactStatus.OK,
                provenance=_provenance(
                    source_id,
                    SourceMethod.HTTP_GET,
                    url,
                    cache=CacheState.STALE,
                    raw_ref=ref,
                    raw_truncated=truncated,
                ),
                note=f"served from cache ({age_hours}h old): {reason}",
            )

    ctx.record_unavailable(source_id, reason, impact)
    return unavailable(source_id, url, reason)
