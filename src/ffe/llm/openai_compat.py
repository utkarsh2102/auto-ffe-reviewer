"""OpenAI-compatible backends: OpenCode, DeepSeek, and most local runners.

Written against the capability rather than the vendor. Where an endpoint can
enforce a JSON schema it is asked to; where it can only promise valid JSON it
is asked for that; where it can do neither the contract layer extracts from
prose, exactly as it does for the Claude Code CLI. So adding a backend means
describing what it supports, not adding a branch anywhere else.

Switching to this harness is two environment variables:

    FFE_LLM_HARNESS=openai-compat
    FFE_LLM_MODEL=deepseek-chat

plus FFE_LLM_BASE_URL and an API key. No review logic changes, because none of
it lives here -- the criteria are in policy/ and the validation is in
contract.py.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from ffe.llm.base import HarnessError, HarnessInfo, LLMRequest, LLMResponse

HARNESS_ID = "openai-compat"

# Endpoints known to enforce a supplied JSON schema. Anything not listed gets
# the safer, less capable path, which is the right default for an unknown
# backend.
_SCHEMA_CAPABLE_HOSTS = ("api.openai.com", "api.deepseek.com")


@dataclass
class OpenAICompatHarness:
    """Chat-completions client for any OpenAI-compatible endpoint."""

    model: str = ""
    base_url: str = ""
    api_key_env: str = "FFE_LLM_API_KEY"
    structured_output: bool | None = None  # None means infer from the host

    @property
    def _endpoint(self) -> str:
        base = (self.base_url or os.environ.get("FFE_LLM_BASE_URL", "")).rstrip("/")
        if not base:
            raise HarnessError(HARNESS_ID, "no base URL configured (set FFE_LLM_BASE_URL)")
        return f"{base}/chat/completions"

    def describe(self) -> HarnessInfo:
        supports = self.structured_output
        if supports is None:
            base = self.base_url or os.environ.get("FFE_LLM_BASE_URL", "")
            supports = any(host in base for host in _SCHEMA_CAPABLE_HOSTS)
        return HarnessInfo(
            id=HARNESS_ID,
            model=self.model or "unset",
            supports_structured_output=bool(supports),
            supports_system_prompt=True,
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        import requests

        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise HarnessError(HARNESS_ID, f"no API key in ${self.api_key_env}")

        payload: dict[str, object] = {
            "model": self.model,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }

        if request.response_schema:
            if self.describe().supports_structured_output:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ffe_assessment",
                        "strict": True,
                        "schema": request.response_schema,
                    },
                }
            else:
                # Plain JSON mode. The schema is already stated in the system
                # prompt, and contract.py validates regardless.
                payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            response = requests.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(10, request.timeout_s),
            )
        except requests.RequestException as exc:
            raise HarnessError(HARNESS_ID, f"request failed: {exc}") from exc

        if response.status_code != 200:
            raise HarnessError(HARNESS_ID, f"HTTP {response.status_code}: {response.text[:300]}")

        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
            raise HarnessError(HARNESS_ID, f"unexpected response shape: {exc}") from exc

        usage = {k: int(v) for k, v in (body.get("usage") or {}).items() if isinstance(v, int)}
        return LLMResponse(
            text=text,
            model=str(body.get("model", self.model)),
            harness=HARNESS_ID,
            stop_reason=str(choice.get("finish_reason", "")),
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=usage,
            raw=response.text,
        )
