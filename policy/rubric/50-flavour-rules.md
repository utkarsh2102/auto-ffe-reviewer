# Flavour rules

Ubuntu flavours — Kubuntu, Xubuntu, Lubuntu, Ubuntu MATE, Ubuntu Budgie, Ubuntu
Studio, Ubuntu Kylin, Ubuntu Cinnamon, Ubuntu Unity, Edubuntu — are separate
products with their own leads, their own release teams and their own users.

The principle is ownership, not technical risk. **A flavour lead owns their
image and carries the consequences of what goes on it.** Where a change is
theirs to make, the Release Team should not be re-doing their risk analysis.

The evidence classifies every request into one of four cases.

## Core impact

The package is seeded on `ubuntu`, `ubuntu-server` or another
Canonical-published image.

This is core Ubuntu. Flavour relaxation does not apply, however many flavours
are *also* affected. Apply the full analysis.

## Single-flavour impact

The package is seeded on exactly one flavour's image and no core image.

**Be considerably more relaxed.** If the request comes from that flavour's team,
this is their product and their call. The Release Team's role is to note it and
let them get on with it, not to second-guess a decision about an image they do
not own.

Say so explicitly in your recommendation, because it is the reason a reviewer
can move on quickly. Something like: *"Ubuntu Studio only; their team owns the
image and the consequences."*

You should still report the facts — what it touches, what evidence exists — but
you are reporting, not gatekeeping, and the bar for `APPROVE` is
correspondingly lower.

If the requester is **not** from that flavour's team, that is worth a flag. It
may be perfectly routine, but the flavour should know.

## Multi-flavour impact

The package is seeded on two or more flavour images.

**Now it is nobody's call alone.** Flavour A cannot accept risk on Flavour B's
behalf, and B may have entirely different priorities.

For each affected flavour, one of these must hold:

1. The bug contains evidence that the flavour is content with the change, or
2. Someone from that flavour's team has commented.

The evidence lists acknowledgements it found, attributed to a named person
whose Launchpad team membership was checked, and names any flavour still
outstanding.

**Where an acknowledgement is missing, flag it prominently.** Name the flavour.
This is one of the most useful things you can surface, because it is easy to
miss — a developer working on a KDE package may simply not know that Ubuntu
Studio ships it too.

Where every affected flavour has acknowledged, say so and be relaxed. Once the
flavour leads agree, the Release Team does not need to repeat the analysis.

## Not seeded

No image ships it. Flavour rules do not apply; judge on reach and evidence
alone.

## Reporting

Whichever case applies, make it visible. A reviewer scanning a queue should be
able to tell at a glance whether a request is theirs to think hard about or
somebody else's to own.
