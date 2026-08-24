# The learning loop

When the Release Team decides differently from the recommendation, and a member
explains why, that explanation becomes a precedent applied to later reviews.

## Why it costs nothing to collect

The team's own workflow supplies the ground truth. A bug leaving the
`~ubuntu-release` queue, with its final status, says what was decided; the last
review says what was recommended. Every decided bug therefore carries both, and
a disagreement is simply the two disagreeing. Nobody has to label anything.

## The cycle

```
bug leaves the queue
        |
        v
decision recorded beside our recommendation      -> archive/<bug>.json
        |
        v
did they differ?  ------- no ------> counted toward the agreement rate, nothing more
        |
       yes
        |
        v
did a ~ubuntu-release member explain why?  -- no --> nothing is learned
        |
       yes
        |
        v
extract a precedent (separate, constrained call) -> lessons/<id>.json
        |
        v
lessons_hash changes  ->  the open queue is re-reviewed
```

## What it will not learn from

Most of the module is about refusal, and most of its tests are containment
rather than happy path. A system that adjusts its own reasoning from text on
the internet needs its limits pinned down more carefully than its successes.

- **Anyone outside `~ubuntu-release`.** A developer arguing for their own
  request is advocacy. Learning from it would let anyone with a Launchpad
  account train the system to be more permissive, which is the obvious attack
  and the easy one to prevent.
- **A decision with no explanation.** "approved" is a decision, not a reason.
  Inventing a rationale from it would be worse than leaving it alone, because
  the invention would then be applied to real requests.
- **Agreement.** It teaches nothing beyond "keep doing that", and recording it
  would bury the cases that matter.

## What it will not produce

- **A quote nobody wrote.** The quote is the only thing making a precedent
  checkable by a reviewer, so it is verified against what that person actually
  said — matched on words rather than bytes, since the sanitiser rewrites
  whitespace.
- **An attribution to someone who did not comment.**
- **A precedent the model is unsure of.** The extraction prompt asks for
  `confidence: LOW` and an empty lesson where the comments explained nothing,
  and those are discarded.
- **A facet outside a closed vocabulary**, so precedents stay matchable.

## How a precedent is used

Precedents are ranked against the request by how well their facets match, and
the best few are appended to the prompt under a heading that states, in the same
breath, that they never outrank the rubric. The policy says the same thing in
`rubric/95-precedents.md`:

> A precedent never outranks this policy. A precedent never outranks a gate or
> a floor.

So a precedent showing the team approving something without a PPA does not
license ignoring the testing gate. It may be a reason to note that the team has
been relaxed about this shape of request before — which is useful to a reviewer
— but the floor holds.

**The rubric in `policy/` is never edited automatically.** Precedents live in a
separate store. If a precedent suggests the rubric itself is wrong, that is a
conversation for a human to have.

## Watching it

```sh
ffe learn --dry-run    # disagreements worth learning from
ffe learn              # extract precedents
ffe lessons            # what has been learned
ffe lessons --all      # including retired ones
```

The dashboard has a Precedents page showing each one with its verbatim quote
and author, and an Agreement page tracking how often recommendations matched
decisions — split so that if the loop ever makes things worse, it is visible
rather than discovered later.

No agreement rate is shown below ten comparable decisions. A percentage over
three would invite far more confidence than it deserves.

## Retiring one

```sh
ffe lessons --retire L2167756-a1b2c3 --reason "no longer reflects practice"
```

Retiring stops a precedent being applied without erasing that it was learned,
and changes `lessons_hash`, so the open queue is re-reviewed without it.

## If it drifts

Everything is a file in git. `lessons/` can be inspected, pruned, or emptied,
and the agreement metric shows whether it is helping. The loop can be switched
off entirely:

```toml
[learning]
enabled = false
```
