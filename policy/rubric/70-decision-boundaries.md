# Decision boundaries

## The three decisions

### `APPROVE`

The change is justified, its reach is understood, and there is evidence it
works. You are recommending the Release Team say yes.

Requires all of:

- Meaningful value for *this* release (`60-value-vs-risk`).
- Reach understood — no critical evidence `UNAVAILABLE`.
- Checkable testing evidence, or a documented exemption (`40-testing-evidence`).
- Risk proportionate to value, given where the release is.
- Any required flavour acknowledgements present (`50-flavour-rules`).

### `REJECT`

You are recommending the team say no, and the developer should not wait for
more information because more information would not change the answer.

Appropriate when:

- The value does not come close to justifying the risk.
- It is far too late for a change of this reach.
- The change belongs in an SRU or the next cycle instead.

Rejecting is a strong recommendation. Where the problem is *missing* rather
than *wrong*, `NEEDS_INFORMATION` is the honest answer. Where the request is
sound but the timing is not, say that — recommending it as an SRU or for the
next cycle is more useful than a bare refusal.

### `NEEDS_INFORMATION`

You cannot responsibly recommend either, and you can say precisely what would
unblock it.

This is the right answer far more often than it feels like it should be, and it
is a genuinely good outcome: the whole point of this tool is to tell the
Release Team "this is not ready for you yet" before they spend time on it.

Use it when:

- There is no checkable testing evidence and no exemption applies.
- Critical evidence is `UNAVAILABLE` and the change looks consequential.
- The rationale does not explain why this matters for this release.
- A required flavour acknowledgement is missing.
- What is being changed is unclear.

Always list what is missing, specifically enough to act on.

## Gates and floors

The evidence may carry a `decision_floor`. It means the deterministic layer has
established something that constrains the outcome — most often that no checkable
testing evidence exists.

You may not be more permissive than the floor. If the floor is
`NEEDS_INFORMATION`, you may recommend `NEEDS_INFORMATION` or `REJECT`, not
`APPROVE`.

If you believe the floor is wrong, say so in `disagreement_with_deterministic`
and explain why. That field exists because the rules will sometimes be wrong
and a reviewer should hear about it — but the floor still holds, and the human
decides.

## Confidence

Three levels, meaning how much you would stand behind the recommendation:

| Level | Meaning |
|---|---|
| `HIGH` | Evidence is complete and points clearly one way. A reviewer could act on this quickly. |
| `MEDIUM` | Evidence is adequate but has gaps, or the call is genuinely finely balanced. |
| `LOW` | Substantial evidence missing, or the situation is unusual. Read the bug yourself. |

Confidence is about the **evidence**, not about how you feel. Complete evidence
pointing at an obvious `NEEDS_INFORMATION` is `HIGH` confidence — you are quite
sure the answer is "not ready yet".

The evidence carries a `confidence_ceiling`. Never exceed it: it reflects
sources that could not be consulted, and no amount of good reasoning
compensates for a fact you do not have.
