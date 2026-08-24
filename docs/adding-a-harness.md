# Using a different model

The review methodology is model-independent by construction. Swapping backend
is configuration, and no review logic moves — there is a test asserting exactly
that, because it is the property most likely to rot.

## OpenCode, DeepSeek, or anything OpenAI-compatible

Already supported:

```sh
export FFE_LLM_HARNESS=openai-compat
export FFE_LLM_MODEL=deepseek-chat
export FFE_LLM_BASE_URL=https://api.deepseek.com/v1
export FFE_LLM_API_KEY=...

ffe review --bug 2167691
```

Or in `config.toml`:

```toml
[llm]
harness = "openai-compat"
model = "deepseek-chat"
```

That is the whole change. The policy, the evidence gathering, the scoring, the
gates and the output validation are untouched, because none of them live in the
harness.

Works with any OpenAI-compatible endpoint: DeepSeek, OpenAI, vLLM, Ollama,
llama.cpp, and OpenCode's own gateway.

## Why swapping is cheap

The interface is deliberately narrow:

```python
class LLMHarness(Protocol):
    def describe(self) -> HarnessInfo: ...
    def complete(self, request: LLMRequest) -> LLMResponse: ...
```

A harness takes a system prompt, a user message and a schema, and returns text.
It knows nothing about Feature Freeze, Ubuntu, or what a good review looks like.
Everything that matters is elsewhere:

| Concern | Where it lives |
|---|---|
| The criteria | `policy/` |
| Fact-finding | `src/ffe/sources/`, `src/ffe/evidence/` |
| Scoring and gates | `src/ffe/risk/` |
| Output validation | `src/ffe/llm/contract.py` |
| Talking to a model | `src/ffe/llm/<harness>.py` |

## Writing a new one

Three things:

```python
# src/ffe/llm/mine.py
@dataclass
class MyHarness:
    model: str = ""

    def describe(self) -> HarnessInfo:
        return HarnessInfo(
            id="mine",
            model=self.model,
            # True only if the backend can enforce a JSON schema itself.
            supports_structured_output=False,
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        ...  # raise HarnessError if no response can be obtained
```

Register it in `src/ffe/llm/registry.py`, and add it to the parametrised test in
`tests/test_harnesses.py` that checks every harness satisfies the interface.

Two conventions worth following:

- **Raise `HarnessError` when no answer was obtained.** That is recorded as
  `LLM_UNAVAILABLE`, which is distinct from an answer that failed validation.
  The record still publishes, with its deterministic half intact.
- **Declare capabilities honestly.** `supports_structured_output` decides
  whether the contract layer asks the backend to enforce the schema or extracts
  and validates itself. Claiming support that is not there means malformed
  output reaching the validator, which handles it, but wastes a retry.

## What a harness must not do

- **Do not interpret the evidence.** Pass the messages through unchanged.
- **Do not retry internally.** `contract.py` owns the repair loop, because
  retries are budgeted and recorded.
- **Do not grant tools.** The review is a single completion with no tool access.
  That is a security property, not a performance choice — see
  `docs/security.md`.
- **Do not log the user message.** It contains untrusted bug text.

## Verifying a swap

```sh
FFE_LLM_HARNESS=openai-compat FFE_LLM_MODEL=deepseek-chat \
  ffe review --bug 2167691 --force
```

Then check the record's `provenance.harness`. If the output validates, the
harness is correct: the contract layer enforces citations, schema conformance,
and the deterministic floors regardless of which model produced the text.

Because the harness id and model are part of the review key, switching
re-reviews the open queue automatically rather than leaving recommendations
that a different model made.
