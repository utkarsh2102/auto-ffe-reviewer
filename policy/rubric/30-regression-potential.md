# Regression potential

The question is not "how likely is this to break". You cannot know that. The
question is **what happens if it does**, and whether anyone would notice in
time.

## Name the failure mode

"Low risk" is not an assessment. It is a way of declining to make one. Say what
would actually go wrong, in terms of the change in front of you:

- "If the new dependency fails to resolve, packages build-depending on libcurl
  fail to build, which blocks the transition."
- "If the seed change is wrong, the package is missing from the desktop image
  and the feature silently does not work."
- "If the version bump changes the config format, existing installs fail to
  start on upgrade."

A reviewer can check any of those against their own knowledge in seconds. They
cannot check "low risk" at all.

## What to weigh

**Severity.** Ranked roughly by how bad it is to ship:

1. Fails to boot, or breaks the installer.
2. Breaks upgrades, or leaves packages unconfigurable.
3. Breaks package installation or removal.
4. Breaks a major desktop or server function.
5. Breaks a feature of the package itself.

**Reach.** Everyone on the default install, one flavour's users, or people who
sought this package out.

**Detectability.** Something that fails loudly in autopkgtest or at build time
gets caught. Something that only manifests on upgrade, on one architecture, or
under a configuration nobody tests, ships. A quiet failure mode deserves more
caution than a loud one of the same severity.

**Recoverability.** An SRU can fix most things after release, at a cost. Some
things cannot be fixed that way: data loss, a broken upgrade path, a failure
that prevents the system booting far enough to apply the fix.

## Scope is the lever developers can pull

A narrow change to one package is easier to reason about, easier to test, and
easier to revert than a broad one. Where a request is broader than it needs to
be, saying so is useful — the developer can often cut it down, and a reviewer
will usually ask for exactly that.

The curl/upki request that motivated much of this design was approved partly
*because* it had been narrowed to a single package. That is the shape of a good
late-cycle FFe.
