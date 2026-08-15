"""Exception types.

The distinction that matters: `SourceError` is expected. Upstream services go
down, time out and rate-limit us, and a run must survive all of that by marking
the affected evidence UNAVAILABLE rather than aborting. `ContractError` and
`ConfigError` are genuine faults in our own code or setup.
"""

from __future__ import annotations


class FfeError(Exception):
    """Base class for everything this package raises."""


class ConfigError(FfeError):
    """Configuration is missing or malformed."""


class SourceError(FfeError):
    """An evidence source could not be consulted.

    Always caught by the @source decorator and turned into an UNAVAILABLE fact,
    so one flaky upstream never takes down a run.
    """

    def __init__(self, source_id: str, reason: str, *, retryable: bool = True) -> None:
        super().__init__(f"{source_id}: {reason}")
        self.source_id = source_id
        self.reason = reason
        self.retryable = retryable


class SourceTimeout(SourceError):
    """A source exceeded its deadline."""


class RateLimited(SourceError):
    """Upstream asked us to back off. Carries the wait it requested."""

    def __init__(self, source_id: str, reason: str, retry_after_seconds: int) -> None:
        super().__init__(source_id, reason)
        self.retry_after_seconds = retry_after_seconds


class BudgetExceeded(FfeError):
    """A budget stopped work that was otherwise due. Recorded, never silent."""


class ContractError(FfeError):
    """Model output failed validation and could not be repaired."""

    def __init__(self, reason: str, *, violations: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.violations = violations or []
