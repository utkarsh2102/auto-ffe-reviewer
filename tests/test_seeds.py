"""Tests for seeded-image lookup.

Both fixtures are real snapshots of the upstream index. `seeded-healthy` was
captured in June 2026 (10,232 binaries, 16 flavours); `seeded-degraded` was
captured on 2026-09-20 (5,865 binaries, 6 flavours), when upstream was quietly
serving a partial index. The degraded one is the more valuable fixture: it is
what a wrong answer looks like when nothing reports an error.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from ffe.config import load_settings
from ffe.errors import SourceTimeout
from ffe.models import FactStatus, FlavourImpact
from ffe.sources.base import SourceContext
from ffe.sources.seeds import (
    assess_index,
    build_seed_info,
    classify_impact,
    load_flavours,
    parse_index,
    seed_info,
)
from ffe.util.cache import Cache
from ffe.util.http import HttpClient, RawResponse

SEEDS = Path(__file__).parent / "fixtures" / "raw" / "seeds"
FLAVOURS = load_flavours()


def _index(tag: str):  # type: ignore[no-untyped-def]
    return parse_index((SEEDS / f"seeded-{tag}.json.gz").read_bytes())


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


def _response(tag: str) -> RawResponse:
    return RawResponse(200, (SEEDS / f"seeded-{tag}.json.gz").read_bytes(), {}, "u")


# --------------------------------------------------------------------------- #
# Parsing and configuration
# --------------------------------------------------------------------------- #


def test_index_parses_to_binary_to_images() -> None:
    index = _index("healthy")
    assert len(index) > 10_000
    assert ("kubuntu", "daily-live") in index["kate"]


def test_core_images_are_configured_not_hardcoded() -> None:
    assert FLAVOURS.is_core("ubuntu")
    assert FLAVOURS.is_core("ubuntu-server")
    assert not FLAVOURS.is_core("kubuntu")
    assert not FLAVOURS.is_core("ubuntustudio")


def test_flavour_teams_are_known_for_acknowledgement_checks() -> None:
    assert FLAVOURS.teams["kubuntu"] == "kubuntu-dev"
    assert FLAVOURS.teams["ubuntustudio"] == "ubuntustudio-dev"
    assert FLAVOURS.display("ubuntustudio") == "Ubuntu Studio"


# --------------------------------------------------------------------------- #
# Impact classification -- what the flavour rules turn on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("binary", "expected"),
    [
        ("curl", FlavourImpact.CORE),
        ("bash", FlavourImpact.CORE),
        ("kate", FlavourImpact.MULTI_FLAVOUR),
        ("ardour", FlavourImpact.SINGLE_FLAVOUR),
        ("not-a-real-package", FlavourImpact.UNSEEDED),
    ],
)
def test_impact_classification(binary: str, expected: FlavourImpact) -> None:
    info = build_seed_info(_index("healthy"), [binary], FLAVOURS)
    assert classify_impact(info, FLAVOURS) is expected


def test_multi_flavour_names_the_flavours_needing_acknowledgement() -> None:
    """kate ships on Kubuntu and Ubuntu Studio, so neither lead decides alone."""
    info = build_seed_info(_index("healthy"), ["kate"], FLAVOURS)
    assert info.flavours == ("kubuntu", "ubuntustudio")
    assert info.is_core is False


def test_single_flavour_is_that_flavours_call() -> None:
    info = build_seed_info(_index("healthy"), ["ardour"], FLAVOURS)
    assert info.flavours == ("ubuntustudio",)


def test_core_outranks_flavour_count() -> None:
    """curl is on seven images; that makes it core, not multi-flavour."""
    info = build_seed_info(_index("healthy"), ["curl"], FLAVOURS)
    assert len(info.flavours) > 2
    assert classify_impact(info, FLAVOURS) is FlavourImpact.CORE


def test_seed_info_surfaces_the_actual_images_not_just_yes_no() -> None:
    info = build_seed_info(_index("healthy"), ["kate"], FLAVOURS)
    assert ("kubuntu", "daily-live") in info.images
    assert info.binaries_seeded == ("kate",)


def test_multiple_binaries_are_unioned() -> None:
    info = build_seed_info(_index("healthy"), ["kate", "ardour"], FLAVOURS)
    assert set(info.flavours) == {"kubuntu", "ubuntustudio"}
    assert info.binaries_seeded == ("ardour", "kate")


# --------------------------------------------------------------------------- #
# Index health -- presence and absence are not symmetric
# --------------------------------------------------------------------------- #


def test_healthy_index_is_recognised() -> None:
    health = assess_index(_index("healthy"))
    assert health.healthy is True
    assert health.binaries == 10_232
    assert health.flavours == 16


def test_degraded_index_is_detected() -> None:
    """Upstream served this without any error; only the shape gives it away."""
    health = assess_index(_index("degraded"))
    assert health.healthy is False
    assert health.flavours == 6
    assert "flavours" in health.reason


def test_canary_check_catches_a_gutted_index() -> None:
    health = assess_index({"some-leaf-package": (("ubuntu", "daily-live"),)})
    assert health.healthy is False
    assert "bash" in health.reason


def test_absence_from_a_degraded_index_is_unavailable_not_unseeded(tmp_path: Path) -> None:
    """The failure this whole module is built to prevent.

    kate really is seeded, on Kubuntu and Ubuntu Studio. Reading the degraded
    index naively reports it as unseeded, which would make a change to it look
    far safer than it is.
    """
    fact = seed_info(_ctx(tmp_path, _response("degraded")), ["kate"], flavours=FLAVOURS)

    assert fact.status is FactStatus.UNAVAILABLE
    assert fact.value is None
    assert "not evidence" in (fact.note or "")


def test_absence_from_a_healthy_index_is_a_real_answer(tmp_path: Path) -> None:
    fact = seed_info(
        _ctx(tmp_path, _response("healthy")), ["not-a-real-package"], flavours=FLAVOURS
    )

    assert fact.ok
    assert fact.value is not None
    assert fact.value.flavours == ()
    assert "unseeded" in (fact.note or "")


def test_presence_is_trusted_even_in_a_degraded_index(tmp_path: Path) -> None:
    """Finding a package is positive evidence whatever the index is missing."""
    fact = seed_info(_ctx(tmp_path, _response("degraded")), ["curl"], flavours=FLAVOURS)

    assert fact.ok
    assert fact.value is not None
    assert fact.value.is_core is True
    # But we say the picture may be incomplete rather than implying it is whole.
    assert "incomplete" in (fact.note or "")


def test_healthy_index_needs_no_caveat(tmp_path: Path) -> None:
    fact = seed_info(_ctx(tmp_path, _response("healthy")), ["curl"], flavours=FLAVOURS)
    assert fact.ok
    assert fact.note is None


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


def test_unreachable_index_is_unavailable(tmp_path: Path) -> None:
    """The observed seeded-in-ubuntu hang, as the system should experience it."""
    ctx = _ctx(tmp_path, SourceTimeout("seeds", "timed out"))
    fact = seed_info(ctx, ["curl"], flavours=FLAVOURS)

    assert fact.status is FactStatus.UNAVAILABLE
    assert ctx.unavailable != []


def test_no_binaries_to_look_up_is_unavailable(tmp_path: Path) -> None:
    fact = seed_info(_ctx(tmp_path, _response("healthy")), [], flavours=FLAVOURS)
    assert fact.status is FactStatus.UNAVAILABLE
