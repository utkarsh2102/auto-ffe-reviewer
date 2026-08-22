"""Claude Code as a review harness.

Invoked headlessly, one turn, no tools. The flags are chosen to make the call
hermetic and reproducible rather than to make it capable:

- `--bare` skips hooks, plugin sync, CLAUDE.md discovery, auto-memory and
  keychain reads, so the review depends on nothing in the developer's
  environment and produces the same result on a runner as on a laptop.
- `--allowed-tools ""` leaves the model no way to act. This is the load-bearing
  injection control: the worst a manipulated bug can achieve is one wrong
  advisory verdict, displayed beside the evidence that contradicts it.
- `--system-prompt-file` carries the policy, which is some 32 KB and has no
  business on a command line.
- `--strict-mcp-config --mcp-config '{}'` ensures no MCP server is reachable
  whatever is configured globally.

The subprocess environment is an allowlist, so the process that handles
untrusted bug text never sees GITHUB_TOKEN or anything else in the runner.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ffe.errors import SourceTimeout
from ffe.llm.base import HarnessError, HarnessInfo, LLMRequest, LLMResponse
from ffe.util.proc import run

HARNESS_ID = "claude-code"


@dataclass
class ClaudeCodeHarness:
    """Runs the `claude` CLI in print mode."""

    model: str = ""
    executable: str = "claude"
    extra_args: tuple[str, ...] = ()

    def describe(self) -> HarnessInfo:
        return HarnessInfo(
            id=HARNESS_ID,
            model=self.model or "default",
            # The CLI has no JSON-schema enforcement, so the contract layer
            # extracts and validates instead. That path is exercised by every
            # harness, which keeps it from rotting.
            supports_structured_output=False,
            supports_system_prompt=True,
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        if shutil.which(self.executable) is None:
            raise HarnessError(HARNESS_ID, f"{self.executable} is not on PATH")

        with tempfile.TemporaryDirectory(prefix="ffe-prompt-") as workdir:
            system_prompt = Path(workdir) / "system-prompt.md"
            system_prompt.write_text(request.system, encoding="utf-8")

            argv = [
                self.executable,
                "--print",
                "--bare",
                "--output-format",
                "json",
                "--system-prompt-file",
                str(system_prompt),
                "--max-turns",
                "1",
                "--allowed-tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                "{}",
            ]
            if self.model:
                argv += ["--model", self.model]
            argv += list(self.extra_args)

            try:
                result = run(
                    argv,
                    timeout=request.timeout_s,
                    source_id=HARNESS_ID,
                    env=_passthrough_env(),
                    stdin=request.user,
                )
            except SourceTimeout as exc:
                raise HarnessError(HARNESS_ID, str(exc)) from exc

        if not result.ok and not result.stdout.strip():
            raise HarnessError(
                HARNESS_ID,
                f"exit {result.returncode}: {result.stderr.strip()[:300] or 'no output'}",
            )

        text, usage, stop_reason = _unwrap(result.stdout)
        if not text.strip():
            raise HarnessError(HARNESS_ID, "produced an empty response")

        return LLMResponse(
            text=text,
            model=self.model or "default",
            harness=HARNESS_ID,
            stop_reason=stop_reason,
            latency_ms=result.duration_ms,
            usage=usage,
            raw=result.stdout,
        )


def _unwrap(stdout: str) -> tuple[str, dict[str, int], str]:
    """Pull the result out of the CLI's JSON envelope.

    Falls back to the raw output if the envelope is not what we expect, so a
    change in the CLI's output shape degrades to "the contract layer will
    try to parse this" rather than to a hard failure.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, {}, ""

    if not isinstance(envelope, dict):
        return stdout, {}, ""

    if envelope.get("is_error"):
        raise HarnessError(HARNESS_ID, str(envelope.get("result", "reported an error"))[:300])

    raw_usage = envelope.get("usage") or {}
    usage = {k: int(v) for k, v in raw_usage.items() if isinstance(v, int)}
    return str(envelope.get("result", "")), usage, str(envelope.get("subtype", ""))


def _passthrough_env() -> dict[str, str]:
    """Only what the CLI genuinely needs to authenticate and run."""
    import os

    wanted = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    )
    return {key: os.environ[key] for key in wanted if key in os.environ}
