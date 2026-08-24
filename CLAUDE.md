# Working on auto-ffe-reviewer

Conventions that are not obvious from the code, for anyone — human or
otherwise — changing it.

## The one rule

**This tool never decides anything.** It gathers evidence, scores it
deterministically, asks a model for an opinion, and shows a human the result.
No code path may approve, reject, or write to Launchpad. There are tests
asserting this; if one starts failing, the change is wrong, not the test.

## Where things belong

| Concern | Where |
|---|---|
| Review criteria | `policy/` as Markdown, **never** in Python |
| Fact-finding | `src/ffe/sources/`, `src/ffe/evidence/` |
| Deterministic judgement | `src/ffe/risk/` |
| Model output validation | `src/ffe/llm/contract.py` |
| Talking to a model | `src/ffe/llm/<harness>.py` |

If you are about to encode a review judgement in Python, stop: it belongs in
`policy/`. If you are about to ask the model for a fact, stop: it belongs in a
source.

## Rules with teeth

- **Sources never raise.** Every failure becomes a `Fact` with a non-OK status.
  One flaky upstream must not end a run.
- **`UNAVAILABLE` is not `False`.** "Checked, the answer is no" and "could not
  check" must stay distinct, because only the second lowers confidence. The
  same goes for `NOT_APPLICABLE`.
- **Every outbound call has a timeout.** The tools this replaces did not, and
  one of them hung indefinitely. See `docs/security.md`.
- **Bug text is evidence, never instruction**, and never establishes a fact
  about the archive.
- **Prose claims are not evidence.** "I tested this in a PPA" is
  `CLAIMED_ONLY`, whatever it says.
- **Bucket, do not measure, in the fingerprint.** Raw days-to-release would
  re-review every open bug nightly for no new information.

## If you change...

**...the policy:** bump `version` in `policy/manifest.toml`, run
`ffe policy-hash`, paste in the new `policy_hash`. CI fails otherwise. The hash
feeds the review key, so the open queue is re-reviewed automatically.

**...the risk scoring:** bump `RISK_ALGORITHM_VERSION` in `models.py`, for the
same reason.

**...the fingerprint:** bump `FINGERPRINT_VERSION`. Be sure the new input is
material — anything that moves on its own costs a model call every time it does.

## Tests

`pytest -q` — around 600, no network, no API key. Sockets are blocked for the
whole suite; a test needing the network must be marked `@pytest.mark.network`,
and CI excludes those.

Fixtures under `tests/fixtures/raw/` are real captured upstream responses, so
production parsers run against bytes Ubuntu actually publishes. Prefer adding a
captured fixture over hand-writing one.

`tests/fixtures/scenarios/scenarios.toml` holds the 18 representative FFe
cases. A change to the review criteria that alters an outcome there should be
deliberate.

## Commits

One logical change per commit, each passing `ruff check`, `ruff format --check`,
`mypy src/ffe` and `pytest` on its own. Say why in the message, not just what.

Bot output belongs on the orphan `state` branch, never on `main`.
