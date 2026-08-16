"""Tests for TTL caching and raw-blob retention."""

from __future__ import annotations

from pathlib import Path

from ffe.util.cache import Cache
from ffe.util.clock import frozen_at


def test_put_then_get_round_trips(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    cache.put("seeds", b"payload", etag='W/"abc"')

    entry = cache.get("seeds")
    assert entry is not None
    assert entry.data == b"payload"
    assert entry.etag == 'W/"abc"'


def test_missing_key_is_a_miss(tmp_path: Path) -> None:
    assert Cache(tmp_path).get("never-stored") is None


def test_freshness_is_judged_against_a_ttl(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    with frozen_at("2026-09-20T12:00:00Z"):
        cache.put("seeds", b"payload")

    # One hour later: still fresh for a 6h TTL, stale for a 15m one.
    with frozen_at("2026-09-20T13:00:00Z"):
        entry = cache.get("seeds")
        assert entry is not None
        assert entry.age_seconds == 3600
        assert entry.is_fresh(21_600) is True
        assert entry.is_fresh(900) is False


def test_get_returns_stale_entries_for_the_fallback_path(tmp_path: Path) -> None:
    """Serving a stale value beats flipping a fact to UNAVAILABLE.

    A source that flaps would otherwise change the evidence fingerprint and buy
    a fresh LLM review for no new information.
    """
    cache = Cache(tmp_path)
    with frozen_at("2026-09-20T12:00:00Z"):
        cache.put("seeds", b"last known good")

    with frozen_at("2026-10-20T12:00:00Z"):
        entry = cache.get("seeds")
        assert entry is not None
        assert entry.is_fresh(21_600) is False
        assert entry.data == b"last known good"


def test_conditional_headers_let_upstream_respond_304(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    cache.put("seeds", b"x", etag='W/"v1"', last_modified="Wed, 20 Sep 2026 12:00:00 GMT")

    headers = cache.conditional_headers("seeds")
    assert headers["If-None-Match"] == 'W/"v1"'
    assert headers["If-Modified-Since"] == "Wed, 20 Sep 2026 12:00:00 GMT"


def test_conditional_headers_empty_when_nothing_cached(tmp_path: Path) -> None:
    assert Cache(tmp_path).conditional_headers("absent") == {}


def test_corrupt_entry_reads_as_a_miss(tmp_path: Path) -> None:
    """A damaged cache must degrade to a refetch, never to a crash."""
    cache = Cache(tmp_path)
    cache.put("seeds", b"payload")
    path = cache._entry_path("seeds")
    path.write_bytes(b"not gzip at all")

    assert cache.get("seeds") is None


# --------------------------------------------------------------------------- #
# Raw blob store
# --------------------------------------------------------------------------- #


def test_raw_blobs_round_trip(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    ref, truncated = cache.store_raw(b"reverse-depends output", max_bytes=1024)

    assert ref.startswith("sha256:")
    assert truncated is False
    assert cache.load_raw(ref) == b"reverse-depends output"


def test_identical_output_is_stored_once(tmp_path: Path) -> None:
    """Content addressing: the same rdepends output across bugs costs one copy."""
    cache = Cache(tmp_path)
    first, _ = cache.store_raw(b"same bytes", max_bytes=1024)
    second, _ = cache.store_raw(b"same bytes", max_bytes=1024)
    assert first == second


def test_oversized_blobs_are_truncated_and_flagged(tmp_path: Path) -> None:
    """Provenance is worth keeping; an unbounded archive of upstream output is not."""
    cache = Cache(tmp_path)
    ref, truncated = cache.store_raw(b"x" * 5000, max_bytes=1000)

    assert truncated is True
    stored = cache.load_raw(ref)
    assert stored is not None
    assert len(stored) == 1000


def test_load_raw_of_unknown_ref_is_none(tmp_path: Path) -> None:
    assert Cache(tmp_path).load_raw("sha256:" + "0" * 64) is None


def test_gc_removes_only_unreferenced_blobs(tmp_path: Path) -> None:
    cache = Cache(tmp_path)
    keep, _ = cache.store_raw(b"still referenced", max_bytes=1024)
    drop, _ = cache.store_raw(b"orphaned", max_bytes=1024)

    removed = cache.gc(keep_refs={keep})

    assert removed == 1
    assert cache.load_raw(keep) == b"still referenced"
    assert cache.load_raw(drop) is None


def test_gc_on_empty_store_is_harmless(tmp_path: Path) -> None:
    assert Cache(tmp_path).gc(keep_refs=set()) == 0
