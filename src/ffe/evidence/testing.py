"""What testing the developer actually did, as opposed to what they said.

An FFe asks to land something that is *not in the archive yet*, so the
archive's own autopkgtest results describe the wrong version and answer the
wrong question. What matters is the evidence the developer put in the bug: a
PPA, a build log, test output, a link to a test run.

The whole module turns on one distinction:

    "I've tested this in a PPA and it works"      -> CLAIMED_ONLY
    "https://launchpad.net/~me/+archive/ubuntu/x" -> FOUND_VERIFIED, if it exists

Both are things a developer wrote in a bug. Only the second can be checked, and
only checkable things count. A claim is still recorded and shown to the
reviewer -- it may well be true, and it tells them what to ask about -- but it
never satisfies the testing requirement, and the policy states that it must not
be treated as though it had. This is the main reason a bug cannot talk the
system into believing something was tested when it was not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ffe.models import BugFacts, EvidenceState, TestingEvidence, TestingItem
from ffe.sources.launchpad import LaunchpadClient, find_ppa_urls

# Links that point at something checkable.
_BUILD_LOG = re.compile(
    r"https?://(?:launchpadlibrarian\.net/\d+/buildlog[\w.+-]*"
    r"|launchpad\.net/[^\s)>\]]*/\+build/\d+[^\s)>\]]*)",
    re.I,
)
_AUTOPKGTEST_LINK = re.compile(
    r"https?://(?:autopkgtest\.ubuntu\.com/[^\s)>\]]*"
    r"|objectstorage\.[^\s)>\]]*autopkgtest[^\s)>\]]*)",
    re.I,
)
_CI_LINK = re.compile(
    r"https?://(?:github\.com/[^\s)>\]]+/(?:actions|runs)[^\s)>\]]*"
    r"|salsa\.debian\.org/[^\s)>\]]*/pipelines?[^\s)>\]]*"
    r"|ci\.debian\.net/[^\s)>\]]*)",
    re.I,
)

# Fenced or heavily indented blocks, which is how pasted output arrives.
_FENCED = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INDENTED_BLOCK = re.compile(r"(?:^[ \t]{2,}\S.*\n){3,}", re.M)

# Markers that a pasted block really is build or test output rather than, say,
# a debdiff or a changelog excerpt.
_OUTPUT_MARKERS = re.compile(
    r"\b(?:PASS|FAIL|autopkgtest|dpkg-buildpackage|dh_auto_test|"
    r"tests? (?:passed|ran|ok)|\d+ passed|Build finished|"
    r"successfully built|pybuild|make check|ninja test)\b",
    re.I,
)

# Assertions of testing with nothing attached. Deliberately broad: the point is
# to notice and surface the claim, not to grade the sincerity of the wording.
_CLAIM = re.compile(
    r"(?:^|[.;\n]\s*)([^.;\n]{0,120}?\b(?:"
    r"I(?:'ve| have)? tested|we(?:'ve| have)? tested|tested (?:this|it|locally|manually|in)|"
    r"testing (?:was|is) (?:done|complete)|builds? (?:fine|cleanly|successfully|ok)|"
    r"no regressions?|works? (?:fine|as expected|well)|verified (?:this|it|locally)|"
    r"ran the tests?|autopkgtests? pass"
    r")\b[^.;\n]{0,120})",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Extracted:
    """Raw findings, before anything has been verified."""

    items: list[TestingItem] = field(default_factory=list)
    ppa_refs: list[tuple[str, str]] = field(default_factory=list)


def _sources(bug: BugFacts) -> list[tuple[str, str]]:
    """Every block of text a developer could have put evidence in.

    Comments count as much as the description: evidence is usually added after
    the Release Team asks for it, which is exactly the update that should flip
    a NEEDS_INFORMATION into something reviewable.
    """
    blocks = [("description", bug.description)]
    blocks.extend(
        (f"comment #{c.index}", c.content)
        for c in bug.comments
        # Index 0 restates the description; counting it twice would double-report.
        if c.index > 0
    )
    return blocks


def extract(bug: BugFacts) -> Extracted:
    """Find every piece of claimed or linked testing evidence in the bug."""
    found = Extracted()

    for where, text in _sources(bug):
        if not text:
            continue

        for owner, name in find_ppa_urls(text):
            found.ppa_refs.append((owner, name))
            found.items.append(
                TestingItem(
                    kind="ppa",
                    state=EvidenceState.FOUND_UNVERIFIED,
                    detail=f"PPA ~{owner}/{name}",
                    locator=f"https://launchpad.net/~{owner}/+archive/ubuntu/{name}",
                    source=where,
                )
            )

        for pattern, kind, label in (
            (_BUILD_LOG, "build_log", "build log"),
            (_AUTOPKGTEST_LINK, "autopkgtest", "autopkgtest result"),
            (_CI_LINK, "ci", "CI run"),
        ):
            for url in dict.fromkeys(pattern.findall(text)):
                found.items.append(
                    TestingItem(
                        kind=kind,
                        state=EvidenceState.FOUND_UNVERIFIED,
                        detail=f"linked {label}",
                        locator=url,
                        source=where,
                    )
                )

        for block in _pasted_output(text):
            found.items.append(
                TestingItem(
                    kind="test_output",
                    state=EvidenceState.FOUND_UNVERIFIED,
                    detail=f"pasted output ({block})",
                    source=where,
                )
            )

        for claim in _claims(text):
            found.items.append(
                TestingItem(
                    kind="claim",
                    state=EvidenceState.CLAIMED_ONLY,
                    detail=claim,
                    source=where,
                )
            )

    return found


def _pasted_output(text: str) -> list[str]:
    """Blocks that look like real build or test output.

    A fenced block alone is not enough -- debdiffs and changelog excerpts are
    fenced too -- so the content has to carry a recognisable marker.
    """
    blocks: list[str] = []
    for block in (*_FENCED.findall(text), *_INDENTED_BLOCK.findall(text)):
        marker = _OUTPUT_MARKERS.search(block)
        if marker:
            blocks.append(f"contains {marker.group(0)!r}")
    return blocks[:5]


def _claims(text: str) -> list[str]:
    """Prose assertions of testing, trimmed and capped."""
    seen: list[str] = []
    for match in _CLAIM.finditer(text):
        claim = " ".join(match.group(1).split())[:160]
        if claim and claim not in seen:
            seen.append(claim)
    return seen[:5]


def _strongest(items: list[TestingItem], kind: str) -> EvidenceState:
    """The best state achieved for one kind of evidence."""
    order = (
        EvidenceState.ABSENT,
        EvidenceState.CLAIMED_ONLY,
        EvidenceState.FOUND_UNVERIFIED,
        EvidenceState.FOUND_VERIFIED,
    )
    states = [i.state for i in items if i.kind == kind]
    return max(states, key=order.index) if states else EvidenceState.ABSENT


def corroborate(
    found: Extracted,
    *,
    launchpad: LaunchpadClient | None = None,
) -> TestingEvidence:
    """Check what can be checked, and report the rest honestly.

    Every PPA that was linked is looked up. One that does not exist is not a
    neutral result -- it is downgraded to a claim, because a link to nothing is
    no better than a sentence asserting the same thing.
    """
    items = list(found.items)

    if launchpad is not None:
        for owner, name in dict.fromkeys(found.ppa_refs):
            fact = launchpad.verify_ppa(owner, name)
            if not fact.ok or fact.value is None:
                continue
            exists = fact.value.exists
            items = [
                (
                    TestingItem(
                        kind=item.kind,
                        state=(
                            EvidenceState.FOUND_VERIFIED if exists else EvidenceState.CLAIMED_ONLY
                        ),
                        detail=(
                            f"PPA ~{owner}/{name} exists on Launchpad"
                            if exists
                            else f"PPA ~{owner}/{name} was linked but does not exist on Launchpad"
                        ),
                        locator=item.locator,
                        source=item.source,
                    )
                    if item.kind == "ppa" and item.locator == fact.value.url
                    else item
                )
                for item in items
            ]

    claims = tuple(i.detail for i in items if i.state is EvidenceState.CLAIMED_ONLY)

    return TestingEvidence(
        items=tuple(items),
        ppa=_strongest(items, "ppa"),
        build=_strongest(items, "build_log"),
        autopkgtest=max(
            (_strongest(items, "autopkgtest"), _strongest(items, "ci")),
            key=(
                EvidenceState.ABSENT,
                EvidenceState.CLAIMED_ONLY,
                EvidenceState.FOUND_UNVERIFIED,
                EvidenceState.FOUND_VERIFIED,
            ).index,
        ),
        test_output=_strongest(items, "test_output"),
        unverified_claims=claims,
    )


def collect(bug: BugFacts, *, launchpad: LaunchpadClient | None = None) -> TestingEvidence:
    """Extract and corroborate in one step."""
    return corroborate(extract(bug), launchpad=launchpad)
