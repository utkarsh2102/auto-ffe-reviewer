"""Learning from the Release Team's decisions.

Every decided bug carries both what was recommended and what the team chose, so
disagreements are free to detect. Where one happened and a Release Team member
explained their reasoning, that reasoning becomes a precedent applied to future
reviews.

Three constraints keep this from drifting:

- Only comments by members of ~ubuntu-release can teach anything. A developer
  arguing for their own request is advocacy, and treating it as precedent would
  let anyone with a Launchpad account train the system to be more permissive.
- Precedents are schema-constrained records carrying a verbatim quote and its
  author, not free text. Each is traceable to something a person actually said.
- They never outrank the rubric, a gate or a floor. The policy in policy/ is
  never edited automatically.
"""
