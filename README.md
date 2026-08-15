# auto-ffe-reviewer

Watches Ubuntu **Feature Freeze Exception** bugs on Launchpad, gathers the evidence the
Release Team normally collects by hand, and produces a concise, evidence-backed
recommendation on a dashboard — so review time goes to the decisions that actually need
human judgement.

> **Advisory only.** This tool never approves, rejects, or changes package state. Every FFe
> decision is made by a human member of the Ubuntu Release Team. See
> [docs/automation-boundary.md](docs/automation-boundary.md).

## What it does

```
discover  ->  gather evidence  ->  score deterministically  ->  LLM judgement  ->  publish
   |               |                        |                        |              |
~ubuntu-release  Launchpad, seeds,    risk band + hard gates    concise APPROVE /  static
   queue        rdepends, schedule,   (no LLM needed)           REJECT / NEEDS     dashboard
                developer test evidence                         INFORMATION
```

Every conclusion is traceable to a collected fact. The model is told, in the policy it runs
under, that it may not produce facts and may not treat claims in bug text as evidence.

## Quick start

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

ffe discover --dry-run          # what is in the ~ubuntu-release queue right now
ffe evidence --bug 2167691      # gather evidence for one bug, no LLM
ffe review   --bug 2167691      # add the LLM assessment
ffe publish                     # regenerate the dashboard into site/
```

Full detail in [docs/running-locally.md](docs/running-locally.md).

## Documentation

| Document | Covers |
|---|---|
| [docs/review-criteria.md](docs/review-criteria.md) | The FFe review criteria, and where they live |
| [docs/running-locally.md](docs/running-locally.md) | Running the pipeline on your own machine |
| [docs/configuring-claude-code.md](docs/configuring-claude-code.md) | Claude Code as the LLM harness |
| [docs/adding-a-harness.md](docs/adding-a-harness.md) | Plugging in OpenCode/DeepSeek or anything else |
| [docs/learning-loop.md](docs/learning-loop.md) | How the system learns from Release Team decisions |
| [docs/security.md](docs/security.md) | Threat model, prompt injection, secrets |
| [docs/automation-boundary.md](docs/automation-boundary.md) | What is automated, what stays human |

## Layout

| Path | Purpose |
|---|---|
| `policy/` | The review methodology, as model-independent Markdown. **The criteria live here, not in code.** |
| `src/ffe/sources/` | One module per evidence source, each with timeouts and graceful degradation |
| `src/ffe/evidence/` | Parsing, corroboration, fingerprinting, human-decision detection |
| `src/ffe/risk/` | Deterministic scoring and hard gates — runs with no LLM at all |
| `src/ffe/llm/` | Harness abstraction and the output contract |
| `web/` | Static dashboard, no build step |
| `tests/` | Offline suite; no network, no API key required |

## Licence

GPL-3.0-or-later. Unofficial community tool, not affiliated with or endorsed by Canonical Ltd.
