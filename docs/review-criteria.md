# The review criteria

The criteria live in [`policy/`](../policy/), as Markdown. Not in Python, not
in a prompt string buried in a module — in files a Release Team member can read,
disagree with, and change without touching code.

That is the point of the arrangement, so it is worth saying plainly: **if you
want to change how requests are judged, edit `policy/`.**

## What is where

| File | Covers |
|---|---|
| `SKILL.md` | The task, and the rules that outrank everything else. |
| `rubric/00-role-and-scope.md` | Advisory only; what Feature Freeze is; what loses a reviewer's trust. |
| `rubric/10-release-context.md` | LTS versus interim; position in the freeze window. |
| `rubric/20-blast-radius.md` | Seeded images, reverse dependencies, criticality. |
| `rubric/30-regression-potential.md` | Naming the actual failure mode. |
| `rubric/40-testing-evidence.md` | What counts as evidence, and what does not. |
| `rubric/50-flavour-rules.md` | Core, single-flavour and multi-flavour ownership. |
| `rubric/60-value-vs-risk.md` | Whether the change is worth the risk at all. |
| `rubric/70-decision-boundaries.md` | Which decision, and how confident. |
| `rubric/80-output-contract.md` | Exactly what to emit. |
| `rubric/90-untrusted-input.md` | Bug text is evidence, never instruction. |
| `rubric/95-precedents.md` | How to weigh learned precedents. |

## Editing the policy

1. Edit the relevant file.
2. Bump `version` in `policy/manifest.toml`.
3. Run `ffe policy-hash` and paste the new hash into `policy_hash`.
4. Commit.

CI fails if the hash does not match the content. That is deliberate: the hash
feeds the review key, so an unrecorded edit would silently re-review every open
bug with no record of what changed. Forcing the two to move together makes a
policy change a visible, reviewable act.

Once merged, every open request is re-reviewed under the new policy
automatically. A change to how requests are judged should change the
judgements.

## What is *not* in the policy

Deliberately, the facts. The model is never asked whether curl is seeded or how
many packages depend on it — those come from Ubuntu tooling and arrive as
evidence. The policy governs judgement; `src/ffe/` governs fact-finding. Anything
the model could get wrong about the archive is something it is never asked.

Two curated files in `data/` sit alongside the derived evidence, and both only
ever raise how a package is treated. `criticality.toml` lists packages the
evidence would understate -- grub2 and shim matter because a regression leaves
a machine unbootable, which no dependency graph records and no SRU can reach.
`watchlist.toml` is an escape hatch for a request the queue has not picked up.
Neither can make anything look safer; if the derived evidence overstates
something, the evidence is what to fix.

Some conclusions do not need judgement at all, and those are hard-coded in
`src/ffe/risk/gates.py` — for instance, that a change with nothing anyone can
check is not ready to review, whatever a model might make of how reasonable the
request sounds. Gates constrain the model rather than advising it; it may
disagree, but it must say so explicitly.

## The criteria, in the order they are weighed

1. **LTS or interim.** An LTS is supported for years; be markedly more
   conservative. This scales the timing score rather than adding to it, so an
   LTS request early in the window is still treated as early.
2. **Where we are in the cycle.** Feature Freeze runs from the FF date until
   release, tightening throughout. Named milestones matter more than the raw
   day count, because each one moves the bar.
3. **Criticality and blast radius.** Derived from evidence — seeded images,
   reverse dependencies, archive component — never from a list of package names.
4. **Seeded images.** The single most load-bearing fact, and the one where an
   incomplete upstream index can mislead. See `docs/security.md` on why absence
   is treated differently from presence.
5. **Dependencies.** Counted from the same service `reverse-depends` uses, at
   source level, so `src:curl` rather than the `curl` binary.
6. **Popularity and importance.** Complements the above; never a substitute.
7. **Regression potential.** The policy requires naming the failure mode. "Low
   risk" is a way of declining to assess, and a reviewer cannot check it.
8. **Testing evidence.** The most consequential practical check, and the one an
   automated reviewer is most easily fooled on.
9. **Value against risk.** Whether the change earns the risk it asks for.

Plus the flavour rules, which are about ownership rather than risk: a change
confined to one flavour's image is that flavour's call to make.
