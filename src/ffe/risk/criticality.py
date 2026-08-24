"""Curated overrides for packages the evidence would understate.

Criticality is derived, not declared: a package is critical because it is
seeded on a core image, or has hundreds of reverse dependencies, or sits in
main. This module is not a way around that, and it deliberately only ever
raises the floor -- nothing here can make a package look safer than the
evidence says.

It exists for two blind spots. The seed index has been observed serving
incomplete data, so a package that is genuinely core can read as unseeded. And
some packages matter for reasons no dependency graph records: if grub2
regresses, the machine does not boot, and no SRU can reach it however few
packages declare a dependency.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CRITICALITY_TOML = Path(__file__).resolve().parents[3] / "data" / "criticality.toml"


@dataclass(frozen=True, slots=True)
class Criticality:
    """Packages whose treatment is raised regardless of the evidence."""

    always_core: frozenset[str] = field(default_factory=frozenset)
    unrecoverable: frozenset[str] = field(default_factory=frozenset)
    reasons: dict[str, str] = field(default_factory=dict)

    def is_always_core(self, packages: tuple[str, ...]) -> bool:
        return any(name in self.always_core for name in packages)

    def unrecoverable_failure(self, packages: tuple[str, ...]) -> tuple[str, ...]:
        """Which of these packages cannot be fixed after release if they break."""
        return tuple(sorted(name for name in packages if name in self.unrecoverable))

    def reason(self, key: str) -> str:
        return " ".join(self.reasons.get(key, "").split())


def load_criticality(path: Path = CRITICALITY_TOML) -> Criticality:
    """Read the overrides. An absent file simply means no overrides."""
    if not path.is_file():
        return Criticality()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        # A malformed overrides file must not take down a run; the derived
        # evidence still stands on its own.
        return Criticality()

    always = raw.get("always_core", {}) or {}
    unrecoverable = raw.get("unrecoverable_failure", {}) or {}

    return Criticality(
        always_core=frozenset(str(p) for p in always.get("packages", [])),
        unrecoverable=frozenset(str(p) for p in unrecoverable.get("packages", [])),
        reasons={
            "always_core": str(always.get("reason", "")),
            "unrecoverable_failure": str(unrecoverable.get("reason", "")),
        },
    )
