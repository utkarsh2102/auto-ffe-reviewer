"""Settings, loaded from config.toml and overridable from the environment.

Everything tunable lives in config.toml so behaviour can be reviewed in a diff.
Environment overrides exist for the things CI legitimately varies -- which LLM
harness and model to use, chiefly -- and are named FFE_<SECTION>_<KEY>.

No secrets are read from here. Credentials come from the environment only, and
v1 needs none for Launchpad, which is read anonymously.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ffe.errors import ConfigError

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"


@dataclass(frozen=True, slots=True)
class LaunchpadSettings:
    instance: str = "production"
    distribution: str = "ubuntu"
    release_team: str = "ubuntu-release"
    user_agent: str = "auto-ffe-reviewer/0.1"
    open_statuses: tuple[str, ...] = (
        "New",
        "Incomplete",
        "Confirmed",
        "Triaged",
        "In Progress",
        "Fix Committed",
    )

    @property
    def api_root(self) -> str:
        host = (
            "api.launchpad.net"
            if self.instance == "production"
            else f"api.{self.instance}.launchpad.net"
        )
        return f"https://{host}/devel"

    @property
    def web_root(self) -> str:
        host = (
            "bugs.launchpad.net"
            if self.instance == "production"
            else f"bugs.{self.instance}.launchpad.net"
        )
        return f"https://{host}"


@dataclass(frozen=True, slots=True)
class DiscoverySettings:
    secondary_sweep: bool = True
    title_pattern: str = r"^\s*\[?\s*FFe\s*\]?\s*[:\-]?\s"
    search_text: str = "FFe"


@dataclass(frozen=True, slots=True)
class TimeoutSettings:
    connect: int = 5
    read: int = 30
    read_large: int = 60
    subprocess: int = 120
    llm: int = 300
    max_run_seconds: int = 1200

    @property
    def default(self) -> tuple[int, int]:
        return (self.connect, self.read)

    @property
    def large(self) -> tuple[int, int]:
        return (self.connect, self.read_large)


@dataclass(frozen=True, slots=True)
class CacheSettings:
    release_calendar_ttl: int = 86_400
    seeds_ttl: int = 21_600
    rdepends_ttl: int = 21_600
    launchpad_ttl: int = 900
    evidence_ttl: int = 21_600
    raw_blob_max_bytes: int = 262_144


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    min_llm_interval_per_bug_seconds: int = 1800
    max_llm_runs_per_bug_per_day: int = 6
    max_llm_runs_per_run: int = 10
    review_max_age_days: int = 7


@dataclass(frozen=True, slots=True)
class LlmSettings:
    harness: str = "claude-code"
    model: str = ""
    temperature: float = 0.0
    max_output_tokens: int = 2000
    max_repair_attempts: int = 2


@dataclass(frozen=True, slots=True)
class LearningSettings:
    enabled: bool = True
    max_active_lessons: int = 15
    max_prompt_chars: int = 6000


@dataclass(frozen=True, slots=True)
class SanitizeSettings:
    max_description_chars: int = 12_000
    max_comment_chars: int = 4_000
    max_total_chars: int = 40_000


@dataclass(frozen=True, slots=True)
class DashboardSettings:
    title: str = "FFe Reviewer"
    base_url: str = ""


@dataclass(frozen=True, slots=True)
class Settings:
    launchpad: LaunchpadSettings = field(default_factory=LaunchpadSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    timeouts: TimeoutSettings = field(default_factory=TimeoutSettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    learning: LearningSettings = field(default_factory=LearningSettings)
    sanitize: SanitizeSettings = field(default_factory=SanitizeSettings)
    dashboard: DashboardSettings = field(default_factory=DashboardSettings)

    # Runtime switches, not persisted in config.toml.
    offline: bool = False
    cache_dir: Path = field(default_factory=lambda: Path(".ffe-cache"))
    state_dir: Path = field(default_factory=lambda: Path(".ffe-state"))


_SECTIONS: dict[str, type] = {
    "launchpad": LaunchpadSettings,
    "discovery": DiscoverySettings,
    "timeouts": TimeoutSettings,
    "cache": CacheSettings,
    "budget": BudgetSettings,
    "llm": LlmSettings,
    "learning": LearningSettings,
    "sanitize": SanitizeSettings,
    "dashboard": DashboardSettings,
}


def _coerce(value: str, target: Any) -> Any:
    """Coerce an environment string to the type of the default it replaces."""
    if isinstance(target, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(target, int):
        return int(value)
    if isinstance(target, float):
        return float(value)
    if isinstance(target, tuple):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    return value


def _build_section(name: str, cls: type, raw: dict[str, Any], env: dict[str, str]) -> Any:
    """Build one settings section from file values, then environment overrides."""
    import dataclasses

    values: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default  # type: ignore[misc]
        value = raw.get(f.name, default)

        # Lists in TOML become tuples, so the settings objects stay hashable.
        if isinstance(default, tuple) and isinstance(value, list):
            value = tuple(value)

        env_key = f"FFE_{name.upper()}_{f.name.upper()}"
        if env_key in env:
            try:
                value = _coerce(env[env_key], default)
            except ValueError as exc:
                raise ConfigError(f"{env_key}={env[env_key]!r} is not valid: {exc}") from exc

        values[f.name] = value
    return cls(**values)


def load_settings(
    path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    offline: bool = False,
) -> Settings:
    """Load settings from `path`, applying FFE_* environment overrides.

    A missing config file is not an error: the dataclass defaults are a complete,
    working configuration, which keeps `ffe` usable from a bare checkout.
    """
    env = dict(os.environ if env is None else env)
    path = path or DEFAULT_CONFIG_PATH

    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    sections = {
        name: _build_section(name, cls, raw.get(name, {}), env) for name, cls in _SECTIONS.items()
    }

    cache_dir = Path(env.get("FFE_CACHE_DIR", ".ffe-cache"))
    state_dir = Path(env.get("FFE_STATE_DIR", ".ffe-state"))
    offline = offline or env.get("FFE_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}

    return Settings(**sections, offline=offline, cache_dir=cache_dir, state_dir=state_dir)
