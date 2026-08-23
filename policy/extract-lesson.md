# Extracting a precedent

A Feature Freeze Exception was reviewed automatically, the Ubuntu Release Team
then decided differently, and a team member explained their reasoning. Your job
is to turn that into one reusable precedent.

This is not a review. You are not judging whether the team was right — they
were, by definition. You are recording what their decision reveals about how
they weigh these requests, so a future review can take it into account.

## What you are given

- The situation: package, seeding, blast radius, release timing, evidence.
- The recommendation that was made.
- The decision the team actually took.
- Comments written by members of `~ubuntu-release`, verbatim.

Only Release Team comments are supplied. A developer arguing for their own
request is advocacy, not evidence of how the team decides.

## What to produce

One JSON object:

```json
{
  "situation": "The circumstances, in one sentence, stated generally enough to recognise again.",
  "applies_when": {"ffe_kind": "sync", "seeded": false, "is_lts": false},
  "lesson": "What this reveals about how the team weighs this kind of request.",
  "rationale_quote": "The most telling sentence from a Release Team comment, verbatim.",
  "rationale_author": "launchpad-username",
  "confidence": "HIGH | MEDIUM | LOW"
}
```

## How to write a good precedent

**Generalise, but not far.** "For syncs of unseeded developer tooling, the team
treats the Debian version as sufficient validation and does not require a
separate PPA" is useful. "The team is lenient" is not — it would apply
everywhere and mean nothing. "On bug 2167756 the team approved devscripts
2.6.12" is also not — it applies exactly once.

**Quote, do not paraphrase.** `rationale_quote` must be text a Release Team
member actually wrote. It is shown to reviewers as the evidence for the
precedent, and a paraphrase cannot be checked.

**`applies_when` is how the precedent gets found again.** Use only these keys,
and only those the comments actually justify:

| Key | Values |
|---|---|
| `ffe_kind` | `sync`, `merge`, `new-upstream`, `new-package`, `feature-change`, `transition`, `seed-change` |
| `seeded` | `true`, `false` |
| `is_core` | `true`, `false` |
| `is_lts` | `true`, `false` |
| `flavour_impact` | `CORE`, `SINGLE_FLAVOUR`, `MULTI_FLAVOUR`, `UNSEEDED` |
| `risk_band` | `LOW`, `MODERATE`, `HIGH`, `SEVERE` |
| `phase` | `FEATURE_FREEZE`, `UI_FREEZE`, `BETA_FREEZE`, `BETA`, `KERNEL_FREEZE`, `FINAL_FREEZE` |

Fewer keys means the precedent applies more widely. Include a key only where
the reasoning genuinely depends on it.

**Say so when there is nothing to learn.** If the comments do not explain the
decision — if they are administrative, or simply record the outcome — return
`"confidence": "LOW"` and an empty `lesson`. A precedent invented from a
comment that explained nothing is worse than no precedent, because it will be
applied to future requests as though it meant something.

**Do not learn a rule that contradicts the review policy.** If the team's
reasoning appears to conflict with the rubric, record what they said and why,
and leave it to a human to decide whether the rubric should change. Precedents
describe practice; they do not amend policy.
