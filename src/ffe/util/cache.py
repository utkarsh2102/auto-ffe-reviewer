"""On-disk caching and raw-response retention.

Two jobs, both in service of the same property: a run should cost upstreams as
little as possible, and should degrade rather than fail when they misbehave.

1. **TTL cache with stale fallback.** When an upstream is unreachable we serve
   the last good value and mark it `stale`. This matters more than it sounds:
   a source that flaps would otherwise flip facts to UNAVAILABLE, changing the
   evidence fingerprint and buying a fresh LLM review for no new information.

2. **Content-addressed raw store.** Every response is kept, gzipped, so the
   dashboard can show the actual bytes behind "seeded: no". Blobs are capped --
   provenance is worth keeping, an unbounded archive of upstream output is not.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

from ffe.util.clock import now, parse_iso, utc_iso
from ffe.util.hashing import content_ref, sha256_hex


@dataclass(frozen=True, slots=True)
class CacheEntry:
    data: bytes
    stored_at: str
    age_seconds: int
    etag: str | None = None
    last_modified: str | None = None

    def is_fresh(self, ttl_seconds: int) -> bool:
        return self.age_seconds < ttl_seconds


class Cache:
    """A small file-backed cache. No eviction beyond explicit gc()."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._entries = self.root / "entries"
        self._raw = self.root / "raw"

    # -- TTL cache ---------------------------------------------------------- #

    def _entry_path(self, key: str) -> Path:
        name = sha256_hex(key)
        return self._entries / name[:2] / name

    def get(self, key: str) -> CacheEntry | None:
        """Return the cached entry regardless of age; callers apply the TTL.

        Deliberately age-agnostic so the same lookup serves both the fresh path
        and the stale-fallback path.
        """
        path = self._entry_path(key)
        meta_path = path.with_suffix(".json")
        if not path.is_file() or not meta_path.is_file():
            return None

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            data = gzip.decompress(path.read_bytes())
        except (OSError, ValueError, gzip.BadGzipFile):
            return None  # A corrupt entry is simply a miss.

        stored_at = meta.get("stored_at", "")
        try:
            age = int((now() - parse_iso(stored_at)).total_seconds())
        except ValueError:
            age = 1 << 30  # Unparseable timestamp: treat as infinitely old.

        return CacheEntry(
            data=data,
            stored_at=stored_at,
            age_seconds=max(0, age),
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
        )

    def put(
        self,
        key: str,
        data: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        path = self._entry_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(data))
        path.with_suffix(".json").write_text(
            json.dumps(
                {"key": key, "stored_at": utc_iso(), "etag": etag, "last_modified": last_modified},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def conditional_headers(self, key: str) -> dict[str, str]:
        """Revalidation headers for a cached entry, so upstreams can 304 us."""
        entry = self.get(key)
        if entry is None:
            return {}
        headers = {}
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        if entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        return headers

    # -- Raw blob store ----------------------------------------------------- #

    def store_raw(self, data: bytes, *, max_bytes: int) -> tuple[str, bool]:
        """Retain a raw response for provenance. Returns (ref, truncated).

        Content-addressed, so identical output across many bugs is stored once.
        """
        truncated = len(data) > max_bytes
        payload = data[:max_bytes] if truncated else data

        ref = content_ref(payload)
        path = self.raw_path(ref)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(payload))
        return ref, truncated

    def raw_path(self, ref: str) -> Path:
        body = ref.split(":", 1)[-1]
        return self._raw / body[:2] / f"{body}.gz"

    def load_raw(self, ref: str) -> bytes | None:
        path = self.raw_path(ref)
        if not path.is_file():
            return None
        try:
            return gzip.decompress(path.read_bytes())
        except (OSError, gzip.BadGzipFile):
            return None

    # -- Maintenance -------------------------------------------------------- #

    def gc(self, *, keep_refs: set[str]) -> int:
        """Delete raw blobs no retained record points at. Returns count removed."""
        keep = {r.split(":", 1)[-1] for r in keep_refs}
        removed = 0
        if not self._raw.is_dir():
            return 0
        for path in self._raw.rglob("*.gz"):
            if path.stem not in keep:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
