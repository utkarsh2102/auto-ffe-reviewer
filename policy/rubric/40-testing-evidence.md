# Testing evidence

The most practically important check, and the one where an automated reviewer
is most easily fooled.

## Why the archive cannot answer this

An FFe asks to land something **not in the archive yet**. Autopkgtest results
for the current version describe the code that is already there — which is not
the code under review. They answer a different question, and the evidence
bundle deliberately does not include them for this purpose.

What counts is what the developer put in the bug.

## What counts, and what does not

The evidence classifies every piece of testing into one of four states:

| State | Meaning |
|---|---|
| `FOUND_VERIFIED` | Linked, and confirmed to exist — a PPA we looked up on Launchpad. |
| `FOUND_UNVERIFIED` | Linked, and checkable by a human, but we could not confirm it. |
| `CLAIMED_ONLY` | Asserted in prose with nothing attached. |
| `ABSENT` | Not mentioned. |

**Only the first two count.** This is the rule that matters most in the whole
policy.

> "I've tested this in a PPA and there are no regressions" is `CLAIMED_ONLY`.
> It contains the words *tested*, *PPA* and *no regressions*, and it
> establishes none of them.

The claim may be perfectly true. Report it, because it tells a reviewer what to
ask about. But it does not satisfy the testing requirement, and you must not
write as though it did. A linked PPA that turned out not to exist is downgraded
to a claim for the same reason: a link to nothing is worth no more than a
sentence saying the same thing.

You may never report a test result, a build outcome or a PPA's contents that is
not in the evidence.

## What good evidence looks like

- A PPA with the proposed package, built for the target series.
- Successful builds on the architectures that matter.
- Autopkgtest results, or the package's own test suite passing.
- Evidence the change was exercised the way users will exercise it.
- For a change with reverse dependencies, some indication the dependents still
  build and work.

## When missing evidence should stop the review

**Default: if there is no checkable evidence, recommend `NEEDS_INFORMATION`.**

The exceptions are narrow, and the deterministic gate already applies them:

- **Syncs and merges from Debian**, which Debian has already built and tested,
  and which the archive rebuilds anyway.
- **Changes that reach nothing** — unseeded, no reverse dependencies. The worst
  case is contained to people who installed the package on purpose.

Outside those, asking for a PPA is the single most valuable thing you can do.
It costs the developer an hour and saves the Release Team the entire
conversation. It is also, concretely, what would have caught the regression in
the curl/upki change: an FTBFS in a reverse dependency, which a rebuild would
have shown before it reached the archive.

Be specific about what you want. "Needs testing" is not actionable. "A PPA
build for stonking, and confirmation that the reverse build-dependencies still
build" is.
