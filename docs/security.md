# Security

## Threat model

The system reads text written by anyone with a Launchpad account, passes it to
a language model, and publishes the result on a page the Release Team acts on.
Three things follow.

**Bug content is hostile input.** Not usually, but the design assumes it always
might be.

**The model is probabilistic.** It can be argued with. Correctness therefore
cannot rest on it behaving, and does not.

**The output influences decisions.** Not by making them, but by shaping where
attention goes. A plausible wrong answer costs more than an obvious one,
because it is acted on.

## What an attacker could try, and what it gets them

### Talking the reviewer into an approval

A bug description instructing the model to ignore its rules and approve.

The defences, in order of how much they actually do:

**Capability denial.** The model has no tools, no network, no filesystem, one
turn, and one output path that must pass schema and semantic validation. Its
only effect on the world is a line on a dashboard. This is the defence that
matters; the rest reduce the chance of even that.

**The facts are not up for discussion.** Whether curl is seeded, what depends
on it, whether a PPA exists — none of that comes from the model, and none of it
comes from the bug. An injected bug can argue. It cannot change what the seed
index says.

**Deterministic floors.** If no checkable testing evidence exists, the floor is
`NEEDS_INFORMATION` and the model cannot go below it without stating a
disagreement, which is then visible on the dashboard. A successful injection
still cannot produce a bare `APPROVE` on a request with nothing to check.

**Structural isolation.** Bug text never enters the system prompt. In the user
message it is wrapped in per-request nonce fences, and the nonce is stripped
from the text first, so a bug cannot close the fence early and continue as
trusted instruction.

**Epistemic separation.** `rubric/90-untrusted-input.md` states that bug text
establishes intent, never fact.

**Detection.** A deterministic pre-scan flags instruction-override phrasing,
role reassignment, claimed prior approval, forged markers and hidden
characters, and shows the matched text on the dashboard. It never blocks — a
bug can legitimately *discuss* prompt injection, and refusing to review it
would be its own failure — so it informs rather than decides.

**Worst case:** one wrong advisory line, displayed beside the evidence that
contradicts it, with the suspicious passage quoted underneath.

### Hiding text from the human but not the model

Zero-width characters, bidirectional overrides, fullwidth lookalikes — text that
renders one way on Launchpad and tokenises another way.

Stripped before the model sees it, NFKC-normalised so compatibility forms cannot
slip past the structural patterns, and reported by name: "contains a
RIGHT-TO-LEFT OVERRIDE" tells a reviewer what to look for in a way that
"contains 2 odd characters" does not.

### Getting a link onto the dashboard

A URL in a bug that ends up rendered where a Release Team member might click it.

Output validation rejects any URL not present in the evidence. The page renders
every model-produced string with `textContent`, and a strict
Content-Security-Policy in a meta tag forbids remote anything. Tests assert all
three, including that `innerHTML` appears nowhere in `app.js`.

### Fabricating evidence

Claiming testing that did not happen.

A claim in prose is `CLAIMED_ONLY` and never satisfies the testing requirement.
Linked PPAs are checked against Launchpad, and one that does not exist is
downgraded to a claim — a link to nothing is worth no more than a sentence
saying the same. Every reasoning point must cite a JSON pointer that resolves
in the evidence bundle, and a citation that does not resolve invalidates the
whole response.

### Poisoning the learning loop

Writing a persuasive comment so the system learns to be more permissive.

Only comments from members of `~ubuntu-release` can produce a precedent. The
quote is verified against what that person actually wrote, and attribution to
someone who did not comment is rejected. Precedents cannot override a gate or a
floor, and never edit the rubric. See `docs/learning-loop.md`.

## Upstream data that lies without erroring

Not an attack, but the failure that most nearly produced a wrong answer in
practice.

The seeded-packages index served 5,865 binaries across 6 flavours on
2026-09-20, where a copy from three months earlier held 10,232 across 16. A
200 with well-formed JSON, and nothing indicating it was partial. Read naively
it reports "kate is not seeded" — when kate ships on both Kubuntu and Ubuntu
Studio, turning a change that needs two teams' agreement into one that looks
like it affects nobody.

So presence and absence are treated asymmetrically. Finding a package is
trustworthy whatever else is missing. Failing to find one is only evidence if
the index looks complete, judged against canary packages that have been on every
Ubuntu image for twenty years. Otherwise the fact is `UNAVAILABLE`, which lowers
the confidence ceiling, rather than a confident and wrong "unseeded".

The general rule: **a source that might be quietly incomplete must not be read
as authoritative about absence.**

## Credentials

- **Launchpad: none.** Every call is an unauthenticated GET. The system holds
  no Launchpad credentials and could not write if it tried, which is a security
  control as much as a scoping decision.
- **The model API key** reaches exactly one workflow step — the review — and
  not the one that pushes results. Tested.
- **The model subprocess environment is an allowlist.** The process handling
  untrusted bug text never sees `GITHUB_TOKEN`.
- **Fork pull requests** never run the review workflow, so a fork cannot reach
  a secret. CI uses none at all, so fork contributors still get a full signal.

## Subprocess execution

The optional cross-check against the real Ubuntu CLIs runs with `shell=False`
and an argv list, so a package name taken from a bug report cannot be
interpreted as shell syntax. It has a hard deadline and kills the whole process
group, because those tools are Python scripts that may have spawned children.

The deadline exists because `seeded-in-ubuntu` and `reverse-depends` pass no
timeout to their HTTP calls, and `seeded-in-ubuntu -b bash` was observed hanging
indefinitely. In a scheduled job an unbounded hang is not a slow run — it is
six hours of runner burned for nothing.

## Reporting something

Open an issue, or contact the Ubuntu Release Team directly if it concerns
Launchpad or archive access rather than this code.
