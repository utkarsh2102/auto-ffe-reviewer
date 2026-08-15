"""Neutralise untrusted text before it reaches a model.

Launchpad bug descriptions and comments are written by anyone, so they are
treated as data throughout. This module is one layer of several (see
docs/security.md); it makes text *structurally* safe to embed, while
policy/rubric/90-untrusted-input.md handles the epistemic half -- that a claim
in bug text is never itself evidence.

Nothing here tries to detect malice. Detection is a separate, non-blocking
pre-scan in ffe.evidence.injection, whose findings are shown to the reviewer.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

# Zero-width and bidirectional control characters. These can hide text from a
# human reading the bug on Launchpad while still reaching the model, so a
# reviewer and the reviewer's assistant would be looking at different content.
_INVISIBLE = re.compile(
    "["
    "​-‏"  # zero-width space/joiners, LTR/RTL marks
    "‪-‮"  # embedding and override
    "⁠-⁤"  # word joiner, invisible operators
    "⁦-⁩"  # isolates
    "﻿"  # BOM / zero-width no-break space
    "]"
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EXCESS_BLANKS = re.compile(r"\n{4,}")

# Turns of phrase that mimic prompt or chat structure. These are escaped rather
# than deleted, so a reviewer still sees exactly what the bug said. The escapes
# are visible HTML entities rather than invisible joiners: a zero-width
# character would be stripped if this text were ever scrubbed a second time,
# silently reassembling the very token we defanged. Entity form is idempotent --
# "&lt;untrusted" no longer matches the pattern that produced it.
_STRUCTURAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<(\s*/?\s*untrusted\b)", re.I), r"&lt;\1"),
    (re.compile(r"<(\s*/?\s*system\b)", re.I), r"&lt;\1"),
    (re.compile(r"<\|(\s*im_(?:start|end)\s*)\|>", re.I), r"&lt;|\1|&gt;"),
    (re.compile(r"\[(\s*/?\s*INST\s*)\]", re.I), r"&#91;\1&#93;"),
    # Line-initial role labels, which some harnesses treat as turn boundaries.
    (re.compile(r"(?mi)^(\s*)(assistant|system|human|user)\s*:"), r"\1\2&#58;"),
)

ELISION = "\n[... {n} characters elided ...]\n"


def make_nonce() -> str:
    """A fresh fence marker. Per request, so it cannot be guessed in advance."""
    return secrets.token_hex(8)


def scrub(text: str) -> str:
    """Normalise and defang a block of untrusted text.

    Order matters: normalise first so that compatibility characters cannot be
    used to slip past the structural patterns afterwards.
    """
    if not text:
        return ""

    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = _ANSI.sub("", cleaned)
    cleaned = _INVISIBLE.sub("", cleaned)
    cleaned = _CONTROL.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")

    for pattern, replacement in _STRUCTURAL:
        cleaned = pattern.sub(replacement, cleaned)

    cleaned = _EXCESS_BLANKS.sub("\n\n\n", cleaned)
    return cleaned.strip()


def elide(text: str, max_chars: int) -> str:
    """Trim to `max_chars`, cutting from the middle and saying so.

    The middle goes because the opening states the request and the closing
    usually carries the most recent detail; both matter more than the bulk of a
    pasted log in between.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    marker = ELISION.format(n=len(text) - max_chars)
    budget = max_chars - len(marker)
    if budget <= 0:
        return text[:max_chars]

    head = budget // 2
    tail = budget - head
    return text[:head] + marker + text[len(text) - tail :]


def fence(text: str, nonce: str, label: str) -> str:
    """Wrap untrusted text in nonce-tagged markers.

    Any occurrence of the nonce inside the text itself is removed first, so a
    bug cannot close the fence early and have the remainder of its content read
    as trusted instruction.
    """
    body = text.replace(nonce, "")
    return f'<untrusted id="{label}" nonce="{nonce}">\n{body}\n</untrusted nonce="{nonce}">'


def prepare(text: str, *, nonce: str, label: str, max_chars: int) -> str:
    """Scrub, elide and fence in the one order that is safe."""
    return fence(elide(scrub(text), max_chars), nonce=nonce, label=label)
