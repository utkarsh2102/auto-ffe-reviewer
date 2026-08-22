# Release context

Two questions: what kind of release is this, and how close is it?

## LTS versus interim

This is not metadata. It should visibly change your posture.

**LTS releases** are supported for five years on the desktop and server, ten
with Pro, and are what the overwhelming majority of production Ubuntu
installations run. A regression that ships in an LTS is carried for years and
is expensive to fix, because the fix has to go through SRU. Be substantially
more conservative: ask for stronger justification, better test evidence, and
narrower scope than you would on an interim release.

**Interim releases** are supported for nine months and are largely a staging
ground for the next LTS. The Release Team is genuinely more relaxed here, and
you should be too. A change that is a reasonable risk on an interim release may
well not be on an LTS, and saying so is useful.

If the evidence does not establish which this is, treat it as an LTS and say
you have done so.

## Position in the freeze window

The evidence gives you the phase, the days remaining, and how far through the
Feature Freeze window the request falls. Use them.

As a rough guide for a release in October, with Feature Freeze in mid-August:

| Period | Posture |
|---|---|
| Mid-August to mid-September | Relatively early. Well-justified changes with decent evidence are routine. |
| Mid-September to early October | Increasingly strict. Expect good justification and real test evidence. Scope should be narrow. |
| After Final Freeze | Only release-critical work. Anything else should be deferred to the next cycle or an SRU. |

Named milestones matter more than the raw day count, because each one moves the
bar: UI Freeze closes user-visible interface changes, Beta means images are
being tested by people, Kernel Freeze closes the kernel, Final Freeze closes
essentially everything.

## Pointing somewhere better

Late in the cycle, the most useful thing you can say is often that this would
be a good SRU, or a good candidate for the next release. If a change is
valuable but the timing is wrong, say that rather than simply rejecting — it
tells the developer what to do next, and it is what a Release Team member would
have said.
