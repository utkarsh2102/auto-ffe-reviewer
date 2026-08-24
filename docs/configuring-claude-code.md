# Using Claude Code as the harness

Claude Code is the default. Nothing needs configuring to use it beyond
credentials.

## Locally

Install it, and authenticate however you normally do:

```sh
npm install -g @anthropic-ai/claude-code
claude          # sign in once, interactively
```

Then:

```sh
ffe review --bug 2167691
```

## In CI

Set `ANTHROPIC_API_KEY` as a repository secret. The workflow installs the CLI
and passes the key to exactly one step — the one that runs the review, not the
one that pushes results.

Without a key the workflow runs `--no-llm` and still publishes a useful
dashboard, with evidence, risk scores and gates but no model recommendation.

## How it is invoked

```
claude --print --bare --output-format json \
       --system-prompt-file <policy bundle> \
       --max-turns 1 \
       --allowed-tools "" \
       --strict-mcp-config --mcp-config '{}' \
       [--model <model>]
```

with the evidence on stdin. Each flag is doing something specific:

- **`--bare`** skips hooks, plugin sync, `CLAUDE.md` discovery, auto-memory and
  keychain reads. The review then depends on nothing in the local environment,
  so it produces the same result on a runner as on your laptop.
- **`--allowed-tools ""`** leaves the model no way to act. This is the
  load-bearing injection control: the worst a manipulated bug can achieve is
  one wrong advisory line, shown beside the evidence that contradicts it.
- **`--system-prompt-file`** carries the policy, which is around 32 KB and has
  no business on a command line.
- **`--strict-mcp-config --mcp-config '{}'`** ensures no MCP server is
  reachable, whatever is configured globally.
- **`--max-turns 1`** because this is one question, not a conversation.

The subprocess environment is an allowlist — `PATH`, `HOME`, and the Anthropic
credentials. The process that handles untrusted bug text never sees
`GITHUB_TOKEN` or anything else in the runner.

## Choosing a model

```sh
export FFE_LLM_MODEL=<model>
```

or in `config.toml`:

```toml
[llm]
harness = "claude-code"
model = "..."
```

Left empty, the CLI's default is used. The model identifier is part of the
review key, so changing it re-reviews the open queue — the same reasoning
applied by a different model is a different review.

## Iterating on the policy

Claude Code is the most convenient harness for this, because the review is one
subprocess you can run repeatedly:

```sh
ffe evidence --bug 2167691 --json > /tmp/evidence.json   # what the model sees
$EDITOR policy/rubric/40-testing-evidence.md
ffe policy-hash                                          # update manifest.toml
ffe review --bug 2167691 --force
```

`--force` is needed because an unchanged review key would otherwise skip the
call — which is the behaviour you want in production and not while editing.

The policy also drops into `.claude/skills/ffe-review/` unmodified if you want
to iterate on it interactively. That copy is never on the automated path: the
pipeline always delivers the bundle explicitly, so there is no doubt about which
version a given review ran under.

## Verifying a run

Every record carries what produced it:

```json
"provenance": {
  "policy_version": "1.0.0",
  "policy_hash": "sha256:b41aa8...",
  "risk_algorithm_version": "risk/1.1.0",
  "harness": {"id": "claude-code", "model": "...", "attempts": 1}
}
```

If a recommendation looks wrong, that tells you which policy produced it.
