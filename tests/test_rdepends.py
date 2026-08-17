"""Tests for reverse-dependency analysis.

Fixtures are live captures from the ubuntuwire service for stonking, covering
the full range the review cares about: curl (enormous), kate (a handful),
ardour (almost none) and a package that is not in the archive at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from ffe.config import load_settings
from ffe.errors import SourceTimeout
from ffe.models import FactStatus, SeedInfo
from ffe.sources.base import SourceContext
from ffe.sources.rdepends import (
    BINARY_RELATIONS,
    BUILD_RELATIONS,
    binary_rdepends,
    build_rdepends,
    parse_rdepends,
)
from ffe.util.cache import Cache
from ffe.util.http import HttpClient, RawResponse

RD = Path(__file__).parent / "fixtures" / "raw" / "rdepends"


class _Transport:
    def __init__(self, result: RawResponse | Exception) -> None:
        self._result = result

    def fetch(
        self, url: str, *, headers: Mapping[str, str], timeout: tuple[int, int]
    ) -> RawResponse:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _ctx(tmp_path: Path, result: RawResponse | Exception) -> SourceContext:
    return SourceContext(
        http=HttpClient(
            transport=_Transport(result),  # type: ignore[arg-type]
            sleep=lambda _s: None,
            min_interval_seconds=0,
        ),
        cache=Cache(tmp_path),
        settings=load_settings(Path("/nonexistent.toml"), env={}),
    )


def _ok(fixture: str) -> RawResponse:
    return RawResponse(200, (RD / fixture).read_bytes(), {}, "u")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parser_reproduces_the_reverse_depends_cli_count() -> None:
    """Our number must be the one a Release Team member would get by hand.

    `reverse-depends -l -r stonking curl` reported 149 when this fixture was
    captured. Depends plus Recommends, deduplicated, is exactly that.
    """
    entries = parse_rdepends((RD / "curl-any.json").read_bytes(), BINARY_RELATIONS)
    assert len(entries) == 149


def test_suggests_is_excluded_like_the_cli() -> None:
    """A suggested package is not installed by default, so it is not exposure."""
    entries = parse_rdepends((RD / "curl-any.json").read_bytes(), BINARY_RELATIONS)
    # w3m appears only under Reverse-Suggests for curl.
    assert "w3m" not in {e.package for e in entries}


def test_a_package_under_two_relations_counts_once() -> None:
    entries = parse_rdepends((RD / "src-ardour-any.json").read_bytes(), BINARY_RELATIONS)
    names = [e.package for e in entries]
    assert len(names) == len(set(names))


def test_missing_relations_are_not_an_error() -> None:
    """ardour has no Reverse-Depends key at all; that is simply zero of them."""
    entries = parse_rdepends((RD / "src-ardour-any.json").read_bytes(), BINARY_RELATIONS)
    assert len(entries) == 5


def test_empty_object_parses_as_zero() -> None:
    assert parse_rdepends(b"{}", BUILD_RELATIONS) == []


# --------------------------------------------------------------------------- #
# Source-level queries
# --------------------------------------------------------------------------- #


def test_source_query_captures_binaries_the_source_builds(tmp_path: Path) -> None:
    """The reason `src:` is used rather than the bare package name.

    An FFe is requested for a source package. Asking about the `curl` binary
    alone gives 105 reverse dependencies; asking about the source gives 664,
    because libcurl is where the exposure actually is. Reviewing the binary
    would understate the blast radius roughly sixfold.

    Note 664 unique packages from 677 raw entries: at source level a dependent
    is listed once per binary of ours it depends on, so a package needing both
    curl and libcurl4t64 appears twice and must only be counted once.
    """
    binary_only = parse_rdepends((RD / "curl-any.json").read_bytes(), ("Reverse-Depends",))
    source_wide = parse_rdepends((RD / "src-curl-any.json").read_bytes(), ("Reverse-Depends",))

    assert len(binary_only) == 105
    assert len(source_wide) == 664


def test_query_uses_the_src_prefix(tmp_path: Path) -> None:
    transport = _Transport(_ok("src-curl-any.json"))
    ctx = SourceContext(
        http=HttpClient(transport=transport, sleep=lambda _s: None, min_interval_seconds=0),  # type: ignore[arg-type]
        cache=Cache(tmp_path),
        settings=load_settings(Path("/nonexistent.toml"), env={}),
    )
    fact = binary_rdepends(ctx, "curl", series="stonking")
    assert "src:curl" in fact.provenance.locator


def test_an_already_prefixed_name_is_not_double_prefixed(tmp_path: Path) -> None:
    fact = binary_rdepends(_ctx(tmp_path, _ok("src-curl-any.json")), "src:curl", series="stonking")
    assert "src:src:" not in fact.provenance.locator


# --------------------------------------------------------------------------- #
# Blast radius across the range
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fixture", "package", "expected_total", "expected_bucket"),
    [
        ("src-curl-any.json", "curl", 711, "500+"),
        ("src-kate-any.json", "kate", 6, "6-20"),
        ("src-ardour-any.json", "ardour", 5, "1-5"),
    ],
)
def test_binary_rdepends_across_the_range(
    tmp_path: Path, fixture: str, package: str, expected_total: int, expected_bucket: str
) -> None:
    fact = binary_rdepends(_ctx(tmp_path, _ok(fixture)), package, series="stonking")

    assert fact.ok
    assert fact.value is not None
    assert fact.value.total == expected_total
    assert fact.value.bucket == expected_bucket
    assert fact.value.kind == "binary"


def test_build_rdepends_are_queried_separately(tmp_path: Path) -> None:
    fact = build_rdepends(_ctx(tmp_path, _ok("src-curl-source.json")), "curl", series="stonking")

    assert fact.ok
    assert fact.value is not None
    assert fact.value.total == 529
    assert fact.value.kind == "build"
    assert fact.value.arch == "source"


def test_genuinely_zero_build_rdepends(tmp_path: Path) -> None:
    """kate builds nothing else; a 200 with {} is a real zero."""
    fact = build_rdepends(_ctx(tmp_path, _ok("src-kate-source.json")), "kate", series="stonking")

    assert fact.ok
    assert fact.value is not None
    assert fact.value.total == 0
    assert fact.value.bucket == "0"


def test_sample_is_capped_but_total_is_not(tmp_path: Path) -> None:
    """The dashboard shows a recognisable sample; the raw blob holds the rest."""
    fact = binary_rdepends(_ctx(tmp_path, _ok("src-curl-any.json")), "curl", series="stonking")

    assert fact.value is not None
    assert fact.value.total == 711
    assert len(fact.value.sample) == 25
    assert fact.provenance.raw_ref is not None


def test_seeded_rdeps_are_identified(tmp_path: Path) -> None:
    """Which dependents are themselves on an image is the sharper number.

    A hundred reverse dependencies nobody ships matter less than two that are
    on the desktop.
    """
    seeded = SeedInfo(
        flavours=("kubuntu", "ubuntustudio"),
        images=(("kubuntu", "daily-live"), ("ubuntustudio", "daily-live")),
        is_core=False,
        binaries_seeded=("kubuntu-desktop", "ubuntustudio-desktop-core"),
        binaries_checked=("kubuntu-desktop", "ubuntustudio-desktop-core"),
    )
    fact = binary_rdepends(
        _ctx(tmp_path, _ok("src-kate-any.json")), "kate", series="stonking", seeded=seeded
    )

    assert fact.value is not None
    # kate's dependents include both desktop metapackages, which is the same
    # multi-flavour exposure the seed lookup reports, arrived at independently.
    assert set(fact.value.seeded_rdeps) == {"kubuntu-desktop", "ubuntustudio-desktop-core"}
    assert fact.value.total == 6


# --------------------------------------------------------------------------- #
# Not in the archive vs genuinely zero
# --------------------------------------------------------------------------- #


def test_unknown_package_is_not_applicable_not_zero(tmp_path: Path) -> None:
    """The distinction that keeps a new-package FFe honest.

    The service answers 404 for a name it has never seen. Recording that as
    "zero reverse dependencies" would read as evidence of safety, when it is
    really just a package that does not exist yet.
    """
    ctx = _ctx(tmp_path, RawResponse(404, b"<p>Unknown package</p>\n", {}, "u"))
    fact = binary_rdepends(ctx, "brand-new-package", series="stonking")

    assert fact.status is FactStatus.NOT_APPLICABLE
    assert fact.status is not FactStatus.OK
    assert "not in the stonking archive" in (fact.note or "")


def test_not_applicable_is_distinguishable_from_a_real_zero(tmp_path: Path) -> None:
    missing = binary_rdepends(
        _ctx(tmp_path, RawResponse(404, b"<p>Unknown package</p>\n", {}, "u")),
        "brand-new",
        series="stonking",
    )
    real_zero = build_rdepends(
        _ctx(tmp_path, _ok("src-kate-source.json")), "kate", series="stonking"
    )

    assert missing.status is FactStatus.NOT_APPLICABLE
    assert real_zero.status is FactStatus.OK
    assert real_zero.value is not None
    assert real_zero.value.total == 0


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def test_unreachable_service_degrades(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, SourceTimeout("rdepends", "timed out"))
    fact = binary_rdepends(ctx, "curl", series="stonking")

    assert fact.status is FactStatus.UNAVAILABLE
    assert ctx.unavailable != []
    assert "archive" in ctx.unavailable[0].impact


def test_malformed_response_degrades(tmp_path: Path) -> None:
    fact = binary_rdepends(
        _ctx(tmp_path, RawResponse(200, b"not json", {}, "u")), "curl", series="stonking"
    )
    assert fact.status is FactStatus.ERROR
