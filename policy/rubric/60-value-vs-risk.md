# Value versus risk

Every FFe asks the release to carry risk it would not otherwise carry. The
question is whether what it buys is worth that.

This is the criterion most often skipped, and the one where the reasoning is
most useful to a Release Team member, because it is the part they would
otherwise have to reconstruct from the bug themselves.

## Evaluate the stated rationale

Read what the bug actually claims, and weigh it. Do not accept "this is useful"
— almost everything in the archive is useful to somebody, and that was true
before Feature Freeze too.

Reasons that genuinely justify risk:

- **Hardware enablement.** Machines shipping around release that do not
  otherwise work. Strong, and time-critical by nature.
- **Release-critical function.** Something in the release is broken or missing
  without this.
- **Important user-facing capability** that the release would otherwise lack.
- **Compatibility.** Keeping working with something outside our control that
  has changed.
- **Critical integration**, where components must land together or not at all.
- **Substantial upstream work** where the alternative is shipping a version
  already known to be deficient.
- **Fixing something worse.** Sometimes the change *reduces* risk, and that is
  the strongest argument available.

Reasons that do not, by themselves:

- The work is already done.
- Upstream released a new version.
- A small group would like it.
- It is a nice feature.
- It would be tidier.

A new calculator function is the standard example: harmless, genuinely useful
to someone, and nowhere near worth a late-cycle risk to the release. It should
wait for the next cycle. Nothing is lost by waiting.

## Scale the bar to the cost

The value required is proportional to the risk being taken. A leaf package
with no dependents, well-tested, early in an interim cycle, needs only a
sensible reason. A core seeded package with hundreds of dependents, late in an
LTS cycle, needs a compelling one.

So do not evaluate value in isolation. "Moderate value, minimal risk" is an
easy approval. "Moderate value, severe risk" is not, and that arithmetic is
exactly what you should be showing the reviewer.

## When the rationale is thin

If the bug does not explain why this matters for *this release*, that is a
finding in itself — often the most useful one, because it is the question the
Release Team would have asked anyway. Ask for it plainly, and say what would
answer it.
