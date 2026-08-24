# What is automated, and what is not

The short version: this tool gathers evidence and offers an opinion. It never
decides anything, and it cannot.

## Automated

| Step | What happens |
|---|---|
| Discovery | Finds bugs subscribed to `~ubuntu-release` — the team's own review queue. |
| Evidence | Looks up the release calendar, seeded images, reverse dependencies, archive standing, and whatever testing the developer linked. |
| Corroboration | Checks linked PPAs actually exist on Launchpad. |
| Scoring | Computes a risk band with every component's reasoning, deterministically. |
| Gates | Applies hard rules, such as "no checkable testing evidence means not ready". |
| Recommendation | Asks a model to weigh the evidence and produce a concise verdict. |
| Publication | Regenerates the dashboard. |
| Learning | Where a past recommendation differed from the team's decision, records why. |

## Not automated, and not automatable by this tool

- **Approving an FFe.** Nothing in the codebase can set a bug to Triaged.
- **Rejecting one.** Nothing can set Won't Fix.
- **Asking for more information.** Nothing can set Incomplete.
- **Any write to Launchpad at all.** No comments, no tags, no status changes,
  no subscriptions. Every Launchpad call is an unauthenticated GET; the system
  holds no Launchpad credentials and could not write if it tried.
- **Uploading, copying or removing packages.** It does not have, and does not
  ask for, archive access.

## How that is enforced, rather than merely intended

- No Launchpad credentials exist anywhere in the configuration, the workflow,
  or the code. `docs/security.md` explains why v1 keeps it that way.
- The model runs with no tools: `--allowed-tools ""`, one turn, no network, no
  filesystem. Its only output is JSON matching a schema.
- No deterministic gate can produce `APPROVE`. There is a test asserting this
  across every combination of inputs, because it is the property most worth
  protecting.
- Every page of the dashboard carries the advisory notice, asserted by test.

## What "advisory" means in practice

A recommendation of `APPROVE` means: *on the evidence gathered, a Release Team
member would probably approve this.* It is a claim about the evidence, not a
decision, and it is wrong sometimes — which is why the evidence sits next to it
and every reasoning point cites what it rests on.

The useful case is often the dull one. `NEEDS_INFORMATION` with a precise list
of what is missing means a reviewer can close the tab and move on, and the
developer knows exactly what to supply. Saving that round trip is most of the
value here.

## If this were to change

Posting to Launchpad is deliberately deferred. If it is ever added, the right
shape is a draft subcommand: generate a comment, print it, and let a human
paste it.
The moment write credentials exist, the prompt-injection threat model in
`docs/security.md` changes materially, because the worst case stops being "a
wrong line on a dashboard" and becomes "a wrong comment on a public bug under
the Release Team's name". That is a different system and deserves a fresh
review, not an inherited one.
