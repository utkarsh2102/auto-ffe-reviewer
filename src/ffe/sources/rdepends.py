"""Reverse dependencies: how much of the archive a change can reach.

The blast-radius signal. A leaf package with nothing depending on it can be
fixed after the fact; curl cannot. This uses the same ubuntuwire service that
backs `reverse-depends`, so the numbers match the tool a Release Team member
would run by hand -- verified: Depends plus Recommends for `curl` gives 149,
exactly what `reverse-depends -l -r stonking curl` reports.

Queries use the `src:` form, which is the right question for an FFe. An
exception is requested for a *source* package, and asking about the source
covers every binary it builds: `curl` alone shows 105 reverse dependencies,
while `src:curl` shows 677, because libcurl is where the real exposure lives.
Reviewing the binary of the same name would understate the risk by a factor of
six.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ffe.models import Fact, FactStatus, RdepInfo, SeedInfo, rdep_bucket
from ffe.sources.base import SourceContext, http_fact, not_applicable
from ffe.util.http import RawResponse

SOURCE_ID = "rdepends"
RDEPENDS_URL = "http://qa.ubuntuwire.org/rdepends/v1/{series}/{arch}/{package}"

IMPACT = "cannot estimate how much of the archive this change could affect"

# How many rdeps to keep by name. Enough for a reviewer to recognise the shape
# of the exposure; the rest stay in the retained raw response.
SAMPLE_SIZE = 25

# Runtime exposure. Suggests is excluded, matching `reverse-depends`, which
# needs -s to include it: a suggested package is not installed by default and
# so is not part of the blast radius.
BINARY_RELATIONS = ("Reverse-Depends", "Reverse-Recommends")

# Build-time exposure: what fails to build if this package breaks.
BUILD_RELATIONS = (
    "Reverse-Build-Depends",
    "Reverse-Build-Depends-Indep",
    "Reverse-Build-Depends-Arch",
)

# Packages whose own test suites exercise this one. Not part of the blast
# radius proper, but a good proxy for how much CI a regression would light up.
TEST_RELATIONS = ("Reverse-Testsuite-Triggers",)


@dataclass(frozen=True, slots=True)
class RdepEntry:
    package: str
    component: str


# Returned by the 404 handler so the not-in-archive case can be recognised by
# identity rather than by matching on a note string.
_NOT_IN_ARCHIVE: list[RdepEntry] = []


def parse_rdepends(payload: bytes, relations: tuple[str, ...]) -> list[RdepEntry]:
    """Collect the named relations into one deduplicated list.

    Deduplication is by package name: a package appearing under both Depends
    and Recommends is one dependent, not two.
    """
    raw = json.loads(payload)
    seen: dict[str, str] = {}
    for relation in relations:
        for entry in raw.get(relation, []):
            name = str(entry.get("Package", "")).strip()
            if name:
                seen.setdefault(name, str(entry.get("Component", "")))
    return [RdepEntry(package=n, component=c) for n, c in sorted(seen.items())]


def _build_info(
    entries: list[RdepEntry], *, kind: str, arch: str, seeded: SeedInfo | None
) -> RdepInfo:
    names = [e.package for e in entries]

    # Which dependents are themselves on an image. A hundred rdeps that nobody
    # ships matter far less than two that are on the desktop, so this is
    # usually the more informative number.
    seeded_rdeps: tuple[str, ...] = ()
    if seeded is not None:
        on_images = set(seeded.binaries_seeded)
        seeded_rdeps = tuple(sorted(n for n in names if n in on_images))

    return RdepInfo(
        total=len(names),
        bucket=rdep_bucket(len(names)),
        kind=kind,
        arch=arch,
        sample=tuple(names[:SAMPLE_SIZE]),
        seeded_rdeps=seeded_rdeps,
    )


def _fetch(
    ctx: SourceContext,
    *,
    package: str,
    series: str,
    arch: str,
    relations: tuple[str, ...],
    kind: str,
    seeded: SeedInfo | None,
) -> Fact[RdepInfo]:
    # `src:` asks about every binary the source builds, which is the question
    # an FFe actually poses.
    name = package if package.startswith("src:") else f"src:{package}"
    url = RDEPENDS_URL.format(series=series, arch=arch, package=name)

    fact = http_fact(
        ctx,
        source_id=f"{SOURCE_ID}.{kind}",
        url=url,
        ttl=ctx.settings.cache.rdepends_ttl,
        parse=lambda payload: parse_rdepends(payload, relations),
        impact=IMPACT,
        on_not_found=lambda: _NOT_IN_ARCHIVE,
    )

    if fact.status is FactStatus.OK and fact.value is not None:
        if fact.value is _NOT_IN_ARCHIVE:
            # The service answers 404 with "Unknown package" for a name it has
            # never heard of. That is not zero reverse dependencies -- it is a
            # package that is not in the archive, which is the normal state of
            # affairs for a new-package FFe. A genuine zero comes back as 200
            # with an empty object, and must stay distinguishable from this.
            return not_applicable(
                f"{SOURCE_ID}.{kind}",
                url,
                f"{name} is not in the {series} archive, so it has no reverse dependencies yet",
            )
        return Fact(
            value=_build_info(fact.value, kind=kind, arch=arch, seeded=seeded),
            status=FactStatus.OK,
            provenance=fact.provenance,
            note=fact.note,
        )

    return Fact(value=None, status=fact.status, provenance=fact.provenance, note=fact.note)


def binary_rdepends(
    ctx: SourceContext,
    package: str,
    *,
    series: str,
    seeded: SeedInfo | None = None,
) -> Fact[RdepInfo]:
    """Packages that would break at runtime if this one regressed."""
    return _fetch(
        ctx,
        package=package,
        series=series,
        arch="any",
        relations=BINARY_RELATIONS,
        kind="binary",
        seeded=seeded,
    )


def build_rdepends(
    ctx: SourceContext,
    package: str,
    *,
    series: str,
    seeded: SeedInfo | None = None,
) -> Fact[RdepInfo]:
    """Packages that would fail to build if this one regressed."""
    return _fetch(
        ctx,
        package=package,
        series=series,
        arch="source",
        relations=BUILD_RELATIONS,
        kind="build",
        seeded=seeded,
    )


def testsuite_triggers(ctx: SourceContext, package: str, *, series: str) -> Fact[RdepInfo]:
    """Packages whose autopkgtests exercise this one."""
    return _fetch(
        ctx,
        package=package,
        series=series,
        arch="source",
        relations=TEST_RELATIONS,
        kind="testsuite",
        seeded=None,
    )


def unknown_package_response() -> RawResponse:
    """The service's 404 body, for reference in tests."""
    return RawResponse(404, b"<p>Unknown package</p>\n", {}, "")
