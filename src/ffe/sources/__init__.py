"""Evidence sources.

Each module here answers one question about a package, a bug or the release
cycle, and each returns Facts rather than bare values. They all go through
ffe.sources.base, which guarantees the properties that make a scheduled run
survivable: a deadline, a cache, a stale fallback, retained raw output, and --
above all -- that a source never raises.
"""
