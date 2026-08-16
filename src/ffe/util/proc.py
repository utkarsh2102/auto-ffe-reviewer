"""Subprocess execution with an enforced deadline.

Used for the optional cross-check against the real Ubuntu CLIs
(`seeded-in-ubuntu`, `reverse-depends`). Those tools have no internal timeout,
so the deadline has to be imposed from outside, and it has to kill the whole
process group: the CLIs are Python scripts that may themselves have spawned
children, and killing only the parent would leave those running.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ffe.errors import SourceTimeout

# Nothing inherited by default. A subprocess handling evidence has no business
# seeing GITHUB_TOKEN, API keys, or anything else in the runner's environment.
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command(self) -> str:
        """The command as a copy-pasteable string, for provenance display."""
        return shlex.join(self.argv)


def safe_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """An allowlisted environment for a child process."""
    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    env.update(extra or {})
    return env


def run(
    argv: Sequence[str],
    *,
    timeout: int,
    source_id: str = "subprocess",
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> CommandResult:
    """Run `argv` with a hard deadline.

    Never uses a shell: arguments reach the child exactly as given, so package
    names taken from a bug report cannot be interpreted as shell syntax.
    """
    import time

    started = time.monotonic()
    try:
        # argv-only; shell=False by construction, so bug-derived strings
        # can never be interpreted as shell syntax.
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=safe_env(env),
            input=stdin,
            check=False,
            start_new_session=True,  # own process group, so we can kill all of it
        )
    except subprocess.TimeoutExpired as exc:
        _kill_group(exc)
        raise SourceTimeout(
            source_id, f"{shlex.join(argv)} exceeded {timeout}s and was killed"
        ) from exc
    except FileNotFoundError as exc:
        raise SourceTimeout(source_id, f"{argv[0]} is not installed") from exc

    return CommandResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _kill_group(exc: subprocess.TimeoutExpired) -> None:
    """Terminate the timed-out child's whole process group.

    subprocess.run() already reaps the direct child on timeout, but any
    grandchildren survive and can hold the job open. Killing the group is what
    actually enforces the deadline.
    """
    pid = getattr(exc, "pid", None)
    if not pid:
        return
    # Already gone is the outcome we wanted anyway, so failure here is fine.
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
