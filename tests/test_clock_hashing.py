"""Tests for time handling and canonical hashing."""

from __future__ import annotations

import datetime as dt

from ffe.util.clock import days_between, frozen_at, now, parse_iso, utc_iso
from ffe.util.hashing import canonical_json, content_ref, digest, sha256_hex, short


def test_frozen_clock_pins_now() -> None:
    with frozen_at("2026-09-20T12:00:00Z"):
        assert utc_iso() == "2026-09-20T12:00:00Z"
        assert now().year == 2026
    # And releases afterwards.
    assert utc_iso() != "2026-09-20T12:00:00Z"


def test_frozen_clock_nests_and_restores() -> None:
    with frozen_at("2026-09-20T12:00:00Z"):
        with frozen_at("2026-08-20T00:00:00Z"):
            assert utc_iso() == "2026-08-20T00:00:00Z"
        assert utc_iso() == "2026-09-20T12:00:00Z"


def test_utc_iso_drops_subsecond_precision() -> None:
    """Microseconds would add diff churn to records without adding meaning."""
    assert utc_iso(parse_iso("2026-09-20T12:34:56.789012Z")) == "2026-09-20T12:34:56Z"


def test_parse_iso_accepts_launchpad_and_offset_forms() -> None:
    # Launchpad returns +00:00; a trailing Z must work identically.
    assert parse_iso("2026-09-20T12:00:00+00:00") == parse_iso("2026-09-20T12:00:00Z")


def test_parse_iso_assumes_utc_when_naive() -> None:
    assert parse_iso("2026-09-20T12:00:00").tzinfo is dt.UTC


def test_days_between_signs() -> None:
    ff = dt.date(2026, 8, 20)  # stonking Feature Freeze
    release = dt.date(2026, 10, 15)
    assert days_between(ff, release) == 56  # exactly eight weeks
    assert days_between(release, ff) == -56


def test_canonical_json_ignores_key_order() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_has_no_incidental_whitespace() -> None:
    assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_digest_is_stable_and_prefixed() -> None:
    one = digest({"pkg": "curl", "rdeps": 149})
    two = digest({"rdeps": 149, "pkg": "curl"})
    assert one == two
    assert one.startswith("sha256:")


def test_digest_changes_with_content() -> None:
    assert digest({"rdeps": 149}) != digest({"rdeps": 150})


def test_sha256_accepts_str_and_bytes_alike() -> None:
    assert sha256_hex("abc") == sha256_hex(b"abc")


def test_content_ref_and_short() -> None:
    ref = content_ref(b"raw tool output")
    assert ref.startswith("sha256:")
    assert len(short(ref)) == 12
    assert short(ref) == ref.split(":", 1)[1][:12]
