"""Which Ubuntu images ship a package.

This is the single most load-bearing fact in an FFe review. It decides whether
a change is core Ubuntu impact, one flavour's business, or something several
flavour teams need to agree on -- and therefore how much scrutiny the request
deserves at all.

The data is the same index `seeded-in-ubuntu` reads, so answers match what a
Release Team member would get from the tool by hand. It is fetched directly
rather than through the CLI because that tool passes no timeout and was
observed hanging indefinitely; see docs/security.md.

**The index can be quietly incomplete.** On 2026-09-20 it listed 5,865 binaries
across 6 flavours, where a copy from three months earlier held 10,232 across 16;
in the degraded snapshot `bash` appeared on one image rather than sixteen. Nothing
about the response says it is partial. A naive reading would report "curl is not
seeded in Kubuntu" and make a risky FFe look safe, so presence and absence are
treated asymmetrically: finding a package is evidence, but failing to find one
in an index that looks unhealthy is not evidence of anything.
"""

from __future__ import annotations

import gzip
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ffe.models import Fact, FactStatus, FlavourImpact, SeedInfo
from ffe.sources.base import SourceContext, http_fact, unavailable

SOURCE_ID = "seeds"
SEEDS_URL = "http://qa.ubuntuwire.org/ubuntu-seeded-packages/seeded.json.gz"
FLAVOURS_TOML = Path(__file__).resolve().parents[3] / "data" / "flavours.toml"

IMPACT = "cannot tell which Ubuntu images ship this package"

# Packages that must be on a core image in any complete index. Essential and
# build-essential material that has been in every Ubuntu image for two decades;
# if these are missing or barely present, the index is not describing reality.
CANARY_BINARIES = ("bash", "coreutils", "libc6", "dpkg")

# A healthy index has always carried well over ten flavours and five figures of
# binaries. These thresholds are deliberately far below normal, so they fire on
# a genuinely broken index rather than on ordinary fluctuation.
MIN_HEALTHY_FLAVOURS = 8
MIN_HEALTHY_BINARIES = 8000

SeedIndex = dict[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True, slots=True)
class FlavourConfig:
    core_images: frozenset[str]
    teams: dict[str, str] = field(default_factory=dict)
    display_names: dict[str, str] = field(default_factory=dict)

    def is_core(self, image: str) -> bool:
        return image in self.core_images

    def display(self, image: str) -> str:
        return self.display_names.get(image, image)


def load_flavours(path: Path = FLAVOURS_TOML) -> FlavourConfig:
    """Read the image ownership map. Falls back to core-only if absent."""
    if not path.is_file():
        return FlavourConfig(core_images=frozenset({"ubuntu", "ubuntu-server"}))

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    flavours = raw.get("flavours", {})
    return FlavourConfig(
        core_images=frozenset(raw.get("core", {}).get("images", [])),
        teams={key: value.get("team", "") for key, value in flavours.items()},
        display_names={key: value.get("name", key) for key, value in flavours.items()},
    )


def parse_index(payload: bytes) -> SeedIndex:
    """Parse the gzipped index: {binary: [[flavour, image type], ...]}."""
    raw = json.loads(gzip.decompress(payload))
    return {
        str(binary): tuple((str(f), str(t)) for f, t in entries) for binary, entries in raw.items()
    }


@dataclass(frozen=True, slots=True)
class IndexHealth:
    healthy: bool
    reason: str
    binaries: int
    flavours: int


def assess_index(index: SeedIndex) -> IndexHealth:
    """Decide whether an absence from this index can be believed.

    Presence is always trustworthy. Absence is only meaningful if the index
    looks complete, which is what this checks.
    """
    flavours = {flavour for entries in index.values() for flavour, _ in entries}
    missing = [b for b in CANARY_BINARIES if b not in index]

    if missing:
        return IndexHealth(
            False,
            f"index omits {', '.join(missing)}, which are on every Ubuntu image",
            len(index),
            len(flavours),
        )
    if len(flavours) < MIN_HEALTHY_FLAVOURS:
        return IndexHealth(
            False,
            f"index covers only {len(flavours)} flavours",
            len(index),
            len(flavours),
        )
    if len(index) < MIN_HEALTHY_BINARIES:
        return IndexHealth(
            False,
            f"index holds only {len(index)} binaries",
            len(index),
            len(flavours),
        )
    return IndexHealth(True, "", len(index), len(flavours))


def build_seed_info(index: SeedIndex, binaries: list[str], flavours: FlavourConfig) -> SeedInfo:
    """Summarise which images ship any of `binaries`."""
    images: set[tuple[str, str]] = set()
    seeded: list[str] = []

    for binary in binaries:
        entries = index.get(binary)
        if entries:
            seeded.append(binary)
            images.update(entries)

    image_flavours = {flavour for flavour, _ in images}
    return SeedInfo(
        flavours=tuple(sorted(image_flavours)),
        images=tuple(sorted(images)),
        is_core=any(flavours.is_core(f) for f in image_flavours),
        binaries_seeded=tuple(sorted(seeded)),
        binaries_checked=tuple(sorted(set(binaries))),
    )


def classify_impact(info: SeedInfo, flavours: FlavourConfig) -> FlavourImpact:
    """Reduce seeding to the question the flavour rules actually turn on.

    Core wins outright: a change on a Canonical-published image is never a
    single flavour's call, however many flavours it also touches.
    """
    if info.is_core:
        return FlavourImpact.CORE

    community = [f for f in info.flavours if not flavours.is_core(f)]
    if len(community) >= 2:
        return FlavourImpact.MULTI_FLAVOUR
    if len(community) == 1:
        return FlavourImpact.SINGLE_FLAVOUR
    return FlavourImpact.UNSEEDED


def seed_info(
    ctx: SourceContext,
    binaries: list[str],
    *,
    flavours: FlavourConfig | None = None,
) -> Fact[SeedInfo]:
    """Look up `binaries` in the seeded-packages index.

    Returns UNAVAILABLE rather than "not seeded" when nothing is found in an
    index that looks incomplete. Reporting a seeded package as unseeded would
    understate the blast radius of exactly the changes that most need scrutiny.
    """
    if not binaries:
        return unavailable(SOURCE_ID, SEEDS_URL, "no binary package names to look up")

    flavours = flavours or load_flavours()
    fact = http_fact(
        ctx,
        source_id=SOURCE_ID,
        url=SEEDS_URL,
        ttl=ctx.settings.cache.seeds_ttl,
        parse=parse_index,
        impact=IMPACT,
    )
    if not fact.ok or fact.value is None:
        return unavailable(SOURCE_ID, SEEDS_URL, fact.note or "seed index unavailable")

    index = fact.value
    health = assess_index(index)
    info = build_seed_info(index, binaries, flavours)

    if info.binaries_seeded:
        # Found it. Presence is reliable even in a partial index, though an
        # incomplete one may be understating how many images are affected.
        note = None
        if not health.healthy:
            note = (
                f"seed index looks incomplete ({health.reason}); "
                "the listed images are real but there may be more"
            )
        return Fact(value=info, status=FactStatus.OK, provenance=fact.provenance, note=note)

    if not health.healthy:
        ctx.record_unavailable(SOURCE_ID, f"seed index incomplete: {health.reason}", IMPACT)
        return Fact(
            value=None,
            status=FactStatus.UNAVAILABLE,
            provenance=fact.provenance,
            note=(
                f"none of {', '.join(sorted(set(binaries)))} appear in the seed index, but the "
                f"index looks incomplete ({health.reason}), so this is not evidence that the "
                "package is unseeded"
            ),
        )

    return Fact(
        value=info,
        status=FactStatus.OK,
        provenance=fact.provenance,
        note="not present in the seed index; the index looks complete, so this package is unseeded",
    )
