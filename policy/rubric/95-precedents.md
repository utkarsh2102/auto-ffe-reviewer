# Learned precedents

You may be given precedents: records of past requests where an earlier
recommendation differed from what the Release Team actually decided, together
with the team member's own explanation.

Each has the situation, what was recommended, what was decided, a verbatim
quote from a Release Team member, and who said it.

## How to use them

Precedents describe **observed team behaviour**. They tell you how this team
weighs things in practice, which is knowledge that exists nowhere else — not in
the documentation, and not in this policy.

Where a precedent matches the situation in front of you, let it inform your
judgement, and cite it in `precedents_applied`.

## How not to use them

**A precedent never outranks this policy.** Where they conflict, the rubric
wins, and you say so in `disagreement_with_deterministic`.

**A precedent never outranks a gate or a floor.** A precedent showing the team
approving something without a PPA does not license you to ignore the testing
gate. It may be a reason to note that the team has been relaxed about this
shape of request before — which is useful to a reviewer — but the floor holds.

**A precedent is not a rule.** It is one decision, in one situation, with
reasons that may not carry over. Check that the situation genuinely matches
before leaning on it. A precedent about an unseeded developer tool says nothing
about a core library.

**Do not stack them.** If several look relevant, use the closest one or two.

## Why they are limited to Release Team members

Only comments from members of `~ubuntu-release` can become precedents. A
developer explaining why their own request should have been approved is not
evidence of how the team decides — it is advocacy, and treating it as
precedent would let anyone with a Launchpad account teach the system to be more
permissive.
