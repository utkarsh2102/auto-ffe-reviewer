# Blast radius

How far can this change reach if it goes wrong? Three inputs, all supplied as
evidence. None of them is a number you may estimate.

## Seeded images

The evidence names the exact images and flavours that ship the affected
binaries. Report them, do not reduce them to a yes or no — "seeded on ubuntu
and ubuntu-server" and "seeded on Ubuntu Studio" are entirely different
situations and a reviewer needs to see which.

- **On a core image** (`ubuntu`, `ubuntu-server`, `ubuntu-base`, and the other
  Canonical-published images): a regression reaches the default install. This
  is the highest-consequence case there is.
- **On one or more flavour images**: see `50-flavour-rules`, which governs who
  decides.
- **Not seeded**: users who installed it deliberately are affected. Real, but
  far more contained.

If seeding is `UNAVAILABLE`, you do not know. Say so, and do not let a reader
infer that the package is unseeded. The index this comes from has been observed
serving incomplete data without signalling it, which is exactly why the
evidence distinguishes "not seeded" from "could not tell".

## Reverse dependencies

The evidence gives counts and buckets for both runtime and build
reverse-dependencies, derived from the same service that backs
`reverse-depends`.

- Hundreds, or any at all that are themselves seeded: a regression propagates,
  and the damage is not confined to people using this package.
- A handful: contained, usually recoverable.
- Zero: the change reaches nothing but its own users.

Reverse build-dependencies matter separately. A package that 529 sources
build-depend on can take down a large part of the archive's build queue without
any user ever running it — which is precisely what happened to the curl/upki
change that prompted much of this design.

Quote the actual numbers from the evidence. Never estimate one, and never
reason from a package's reputation: "curl is widely used" is not a substitute
for the count, and if the count is unavailable then the blast radius is
unknown.

## Criticality

Package importance follows from the evidence above, not from a list of names.
A package is critical here if it is on a core image, has substantial reverse
dependencies, or sits in `main` with a Canonical support commitment behind it.

Kernel, OpenSSL, curl, OpenJDK, Tomcat and similar infrastructure will show up
as critical because the evidence says so — seeded, hundreds of dependents, in
`main`. That is the right reason to treat them carefully. Treating a package as
critical because you recognise the name, when the evidence disagrees, means one
of two things: the evidence is incomplete, or your intuition is wrong. Say
which you think it is rather than quietly overriding the data.

The converse also holds. A package nobody has heard of, seeded on the desktop
with 200 reverse dependencies, is a critical package.
