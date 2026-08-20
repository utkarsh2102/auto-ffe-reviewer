"""Deterministic pre-scan for text trying to steer the reviewer.

This does not defend anything. The actual defences are elsewhere and are
structural: the model gets no tools, no network and one validated output path;
untrusted text is nonce-fenced and never enters the system prompt; and the
policy states that bug text establishes intent, never fact, so an injected bug
can argue but cannot manufacture evidence.

What this module does is *notice*, and say so out loud. Findings are attached
to the record and shown on the dashboard next to the recommendation, with the
matched text quoted. That gives a Release Team member something they can check
independently, and makes the model's own SUSPECTED_PROMPT_INJECTION flag
verifiable rather than something they have to take on trust.

It never blocks and never changes a decision. A bug containing the phrase
"ignore previous instructions" might be quoting a CVE advisory or discussing
prompt injection in a package's own test suite, and refusing to review it would
be its own kind of failure.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ffe.models import BugComment, BugFacts, InjectionSignal

EXCERPT_CHARS = 160
MAX_SIGNALS = 20


@dataclass(frozen=True, slots=True)
class Pattern:
    id: str
    regex: re.Pattern[str]
    description: str


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "instruction-override",
        re.compile(
            r"\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all)\b",
            re.I,
        ),
        "attempts to discard earlier instructions",
    ),
    Pattern(
        "role-reassignment",
        re.compile(
            r"\byou are (?:now|a|an)\b|\bact as\b|\bpretend to be\b|\bfrom now on,? you\b", re.I
        ),
        "attempts to reassign the reviewer's role",
    ),
    Pattern(
        "system-prompt-reference",
        re.compile(
            r"\bsystem prompt\b|\byour instructions\b|\byour rules\b|\bdeveloper message\b", re.I
        ),
        "refers to the reviewer's own instructions",
    ),
    Pattern(
        "decision-injection",
        re.compile(
            r"\b(?:output|respond with|answer|reply with|return|say)\b[^.\n]{0,40}"
            r"\b(?:APPROVE[D]?|REJECT[ED]?|NEEDS[_ ]INFORMATION)\b",
            re.I,
        ),
        "attempts to dictate the recommendation",
    ),
    Pattern(
        "authority-claim",
        re.compile(
            r"\b(?:release team|ubuntu-release)\b[^.\n]{0,40}\balready (?:approved|agreed|signed off)\b"
            r"|\bthis (?:has been|was) (?:pre-)?approved\b",
            re.I,
        ),
        "claims an approval that must be verified against the bug's own status",
    ),
    Pattern(
        "fence-forgery",
        re.compile(
            r"</?\s*untrusted\b|<\|im_(?:start|end)\|>|\[/?INST\]|&lt;/?\s*untrusted\b", re.I
        ),
        "contains markers resembling prompt structure",
    ),
    Pattern(
        "urgency-pressure",
        re.compile(
            r"\b(?:do not|don'?t)\b[^.\n]{0,30}\b(?:ask|question|verify|check|flag)\b", re.I
        ),
        "discourages verification",
    ),
    Pattern(
        "encoded-payload",
        re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"),
        "contains a long encoded-looking run",
    ),
    Pattern(
        "data-uri",
        re.compile(r"\bdata:[\w.+-]+/[\w.+-]+;base64,", re.I),
        "contains an inline data URI",
    ),
    Pattern(
        "remote-image",
        re.compile(r"!\[[^\]]*\]\(\s*https?://", re.I),
        "embeds a remote image, which can leak a page view",
    ),
)

# Characters that render as nothing, or reverse the reading order of what
# follows. Their presence in a bug report is not normal.
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def _excerpt(text: str, start: int, end: int) -> str:
    """Quote the match with a little context, on one line."""
    left = max(0, start - 30)
    right = min(len(text), end + 30)
    snippet = " ".join(text[left:right].split())
    return snippet[:EXCERPT_CHARS]


def scan_text(text: str, location: str) -> list[InjectionSignal]:
    """Scan one block of text. Returns every distinct pattern that matched."""
    if not text:
        return []

    signals: list[InjectionSignal] = []
    # Normalise so compatibility and fullwidth forms cannot evade the patterns,
    # the same way the sanitiser does before the text reaches a model.
    normalised = unicodedata.normalize("NFKC", text)

    for pattern in PATTERNS:
        match = pattern.regex.search(normalised)
        if match:
            signals.append(
                InjectionSignal(
                    pattern_id=pattern.id,
                    excerpt=_excerpt(normalised, match.start(), match.end()),
                    offset=match.start(),
                    location=location,
                )
            )

    hidden = _HIDDEN.findall(text)
    if hidden:
        names = sorted({unicodedata.name(c, f"U+{ord(c):04X}") for c in hidden})
        signals.append(
            InjectionSignal(
                pattern_id="hidden-characters",
                excerpt=f"{len(hidden)} invisible or bidirectional character(s): {', '.join(names[:4])}",
                offset=text.index(hidden[0]),
                location=location,
            )
        )

    return signals


def scan_bug(bug: BugFacts) -> tuple[InjectionSignal, ...]:
    """Scan a bug's description and comments.

    Comments are included because a bug can be edited after review: an FFe
    approved on Monday can acquire a persuasive comment on Tuesday, and the
    re-review needs to see it.
    """
    signals = list(scan_text(bug.description, "description"))
    for comment in bug.comments:
        if comment.index > 0:  # index 0 restates the description
            signals.extend(scan_text(comment.content, f"comment #{comment.index}"))
    return tuple(signals[:MAX_SIGNALS])


def scan_comment(comment: BugComment) -> tuple[InjectionSignal, ...]:
    return tuple(scan_text(comment.content, f"comment #{comment.index}"))
