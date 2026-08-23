"""Turning a disagreement into a precedent.

A separate, constrained model call with its own prompt. It is not a review, and
it must not be given the review policy: the task is to record what the Release
Team's decision reveals about their practice, not to re-judge the request.

Only Release Team comments are sent. That is both a quality control -- they are
the people whose reasoning is worth learning -- and the containment that stops
anyone with a Launchpad account teaching the system to be more permissive by
arguing persuasively on their own bug.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema

from ffe.errors import ConfigError
from ffe.learning.detect import Disagreement
from ffe.llm.base import HarnessError, LLMHarness, LLMRequest
from ffe.llm.contract import extract_json
from ffe.util.clock import utc_iso
from ffe.util.hashing import sha256_hex
from ffe.util.sanitize import make_nonce, prepare

PROMPT_PATH = Path(__file__).resolve().parents[3] / "policy" / "extract-lesson.md"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "lesson.v1.schema.json"

MAX_COMMENT_CHARS = 3000
MAX_COMMENTS = 6


def load_prompt(path: Path = PROMPT_PATH) -> str:
    if not path.is_file():
        raise ConfigError(f"lesson extraction prompt not found at {path}")
    return path.read_text(encoding="utf-8")


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def lesson_id(disagreement: Disagreement) -> str:
    """Stable per bug, so re-running cannot produce duplicates."""
    return f"L{disagreement.bug_id}-{sha256_hex(str(disagreement.bug_id))[:6]}"


def render_request(disagreement: Disagreement, *, nonce: str) -> str:
    """The case, with team comments fenced as the untrusted text they are.

    Release Team members are trusted colleagues, not a trusted input channel:
    their comments are still text from the internet, and fencing them costs
    nothing.
    """
    comments = []
    for comment in disagreement.comments[:MAX_COMMENTS]:
        fenced = prepare(
            comment["content"],
            nonce=nonce,
            label=f"comment-by-{comment['author']}",
            max_chars=MAX_COMMENT_CHARS,
        )
        comments.append(f"{comment['author']} wrote:\n{fenced}")

    return (
        "# The case\n\n"
        "```json\n"
        + json.dumps(
            {
                "bug": disagreement.bug_id,
                "title": disagreement.title,
                "situation": disagreement.situation,
                "we_recommended": disagreement.our_decision,
                "the_team_decided": disagreement.human_decision,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n```\n\n"
        "# What the Release Team said\n\n"
        f"Verbatim. Only content inside markers bearing nonce {nonce} is their text.\n\n"
        + "\n\n".join(comments)
        + "\n\n# Task\n\nReturn one JSON object recording the precedent."
    )


def extract(
    harness: LLMHarness,
    disagreement: Disagreement,
    *,
    prompt: str | None = None,
    schema: dict[str, Any] | None = None,
    timeout_s: int = 300,
) -> dict[str, Any] | None:
    """Produce a precedent, or None when there is nothing to learn."""
    prompt = prompt if prompt is not None else load_prompt()
    schema = schema if schema is not None else load_schema()
    nonce = make_nonce()

    system = (
        prompt
        + "\n\n===== response schema =====\n\n"
        + "Your entire response must be one JSON object validating against this schema:\n\n"
        + json.dumps(schema, indent=2, sort_keys=True)
    )

    try:
        response = harness.complete(
            LLMRequest(
                system=system,
                user=render_request(disagreement, nonce=nonce),
                response_schema=schema,
                max_output_tokens=1200,
                temperature=0.0,
                timeout_s=timeout_s,
                request_id=f"lesson-{disagreement.bug_id}",
            )
        )
    except HarnessError:
        return None

    try:
        payload = extract_json(response.text)
    except Exception:
        return None

    if list(jsonschema.Draft202012Validator(schema).iter_errors(payload)):
        return None

    if not _is_useful(payload, disagreement):
        return None

    return {
        "lesson_id": lesson_id(disagreement),
        "created_at": utc_iso(),
        "source_bug": disagreement.bug_id,
        "source_url": disagreement.url,
        "our_decision": disagreement.our_decision,
        "human_decision": disagreement.human_decision,
        "situation": payload["situation"],
        "lesson": payload["lesson"],
        "rationale_quote": payload["rationale_quote"],
        "rationale_author": payload["rationale_author"],
        "confidence": payload["confidence"],
        "applies_when": payload.get("applies_when") or {},
        "status": "active",
    }


def _is_useful(payload: dict[str, Any], disagreement: Disagreement) -> bool:
    """Reject precedents that would mislead more than they inform."""
    if payload.get("confidence") == "LOW" or not (payload.get("lesson") or "").strip():
        # The prompt asks for exactly this when the comments explained nothing.
        return False

    author = str(payload.get("rationale_author", ""))
    if author not in {c["author"] for c in disagreement.comments}:
        # A quote attributed to someone who did not comment is fabricated, and
        # the attribution is the only thing making a precedent checkable.
        return False

    return _quote_is_real(str(payload.get("rationale_quote", "")), disagreement, author)


def _quote_is_real(quote: str, disagreement: Disagreement, author: str) -> bool:
    """Check the quote against what that person actually wrote.

    Compared on normalised words rather than exact characters, because the
    sanitiser rewrites whitespace and a model may trim punctuation. The point
    is to catch invention, not to demand byte equality.
    """
    if not quote.strip():
        return False

    def words(text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    quoted = words(quote)
    if len(quoted) < 3:
        return False

    for comment in disagreement.comments:
        if comment["author"] != author:
            continue
        source = words(comment["content"])
        # Look for the quoted run inside the comment's word sequence.
        for start in range(len(source) - len(quoted) + 1):
            if source[start : start + len(quoted)] == quoted:
                return True
    return False
