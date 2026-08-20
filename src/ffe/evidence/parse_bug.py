"""Work out what an FFe is actually asking for.

Everything here reads untrusted text, so the output is framed as *claims about
the request* -- which packages, which release, what kind of change -- and never
as facts about the archive. Whether those packages are seeded, or what depends
on them, is settled by the sources, not by what the bug says about itself.

Package names are taken from the bug's Launchpad tasks wherever possible. A
task is a structured field the reporter chose from Launchpad's own list, which
makes it far more reliable than anything parsed out of a title, and it cannot
be used to smuggle a different package name past the evidence gathering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ffe.models import BugFacts, FfeKind, SubjectFacts

# "curl (Ubuntu)" or "curl (Ubuntu Stonking)" -> package, optional series.
_TASK_TARGET = re.compile(
    r"^(?P<package>[a-z0-9][a-z0-9.+-]*)\s*\(Ubuntu(?:\s+(?P<series>\w+))?\)$", re.I
)

# Phrases that identify the kind of change, checked in this order. Ordering
# matters where a title could match more than one: "sync a new upstream
# release" is a sync, and how it reaches the archive is the more useful label.
_KIND_PATTERNS: tuple[tuple[FfeKind, re.Pattern[str]], ...] = (
    (
        FfeKind.NEW_PACKAGE,
        re.compile(
            r"\bnew (?:source )?package\b|\[needs-packaging\]|\bpackage (?:request|it)\b", re.I
        ),
    ),
    (FfeKind.SYNC, re.compile(r"\bsync(?:ing)?\b|\bsync \d", re.I)),
    (FfeKind.MERGE, re.compile(r"\bmerge\b", re.I)),
    (
        FfeKind.TRANSITION,
        re.compile(r"\btransition\b|\bABI break\b|\bsoname\b|\brebuild the archive\b", re.I),
    ),
    (
        FfeKind.SEED_CHANGE,
        re.compile(r"\bseed(?:ed|s)?\b|\bby default\b|\bdefault install\b|\bimage\b", re.I),
    ),
    (
        FfeKind.NEW_UPSTREAM,
        re.compile(r"\b(?:update|upgrade|bump)\b.{0,24}\b\d+\.\d+|\b\d+\.\d+(?:\.\d+)?\b", re.I),
    ),
)


@dataclass(frozen=True, slots=True)
class ParsedSubject:
    subject: SubjectFacts
    notes: tuple[str, ...] = ()


def packages_from_tasks(bug: BugFacts) -> tuple[str, ...]:
    """Package names from the bug's Launchpad tasks.

    A task target of plain "ubuntu" means the bug is filed against the
    distribution with no package chosen, which is common for new-package
    requests and tells us nothing about which package is involved.
    """
    found: list[str] = []
    for task in bug.tasks:
        match = _TASK_TARGET.match(task.target.strip())
        if match:
            name = match.group("package").lower()
            if name not in found:
                found.append(name)
    return tuple(found)


def series_from_tasks(bug: BugFacts) -> str | None:
    """A series named in a task target, e.g. 'curl (Ubuntu Stonking)'."""
    for task in bug.tasks:
        match = _TASK_TARGET.match(task.target.strip())
        if match and match.group("series"):
            return match.group("series").lower()
    return None


def find_series_mention(text: str, known_series: tuple[str, ...]) -> str | None:
    """Find a release codename mentioned in the text.

    Only codenames the system already knows are accepted, so an arbitrary word
    in a bug description cannot invent a release.
    """
    lowered = text.lower()
    for series in known_series:
        if re.search(rf"\b{re.escape(series)}\b", lowered):
            return series
    return None


def classify_kind(title: str, description: str) -> FfeKind:
    """Label the kind of change being requested.

    A hint for the reviewer and for precedent matching, not a decision input:
    nothing is approved or rejected because of this label.
    """
    # The title states the request; the description often quotes changelogs and
    # unrelated version numbers, so it is only consulted as a fallback.
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(title):
            return kind
    for kind, pattern in _KIND_PATTERNS:
        if kind is not FfeKind.NEW_UPSTREAM and pattern.search(description[:600]):
            return kind
    return FfeKind.FEATURE_CHANGE


def parse_subject(
    bug: BugFacts,
    *,
    known_series: tuple[str, ...] = (),
    default_series: str | None = None,
) -> ParsedSubject:
    """Determine the packages, target release and kind of an FFe request."""
    notes: list[str] = []

    packages = packages_from_tasks(bug)
    derivation = "bug-tasks"
    if not packages:
        # No package task at all. Common for new-package requests filed against
        # "ubuntu" itself. We do not guess a name out of the title: a wrong
        # package would send every downstream source looking at the wrong thing,
        # which is worse than admitting we do not know.
        derivation = "none"
        notes.append(
            "no source package is identified by any Launchpad task on this bug, "
            "so package-specific evidence could not be gathered"
        )

    series = series_from_tasks(bug)
    series_derivation = "bug-task-series"
    if not series and known_series:
        series = find_series_mention(f"{bug.title}\n{bug.description[:2000]}", known_series)
        series_derivation = "mentioned-in-bug-text"
    if not series:
        series = default_series
        series_derivation = "assumed-development-series"
        if default_series:
            notes.append(
                f"no target release is stated on the bug; assuming the development "
                f"series ({default_series})"
            )

    return ParsedSubject(
        subject=SubjectFacts(
            packages=packages,
            target_series=series,
            ffe_kind=classify_kind(bug.title, bug.description),
            derivation=f"packages:{derivation}; series:{series_derivation}",
        ),
        notes=tuple(notes),
    )
