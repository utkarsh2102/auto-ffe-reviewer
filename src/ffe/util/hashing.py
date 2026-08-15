"""Canonical serialisation and hashing.

Change detection lives or dies on these being stable: the same logical content
must always produce the same digest, across runs, machines and dict orderings.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ffe.models import to_jsonable


def canonical_json(obj: Any) -> str:
    """Serialise deterministically: sorted keys, no insignificant whitespace.

    `ensure_ascii` is left on so the output is byte-identical regardless of the
    filesystem or terminal encoding in play.
    """
    return json.dumps(
        to_jsonable(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    """Hash any object graph via its canonical JSON form, prefixed 'sha256:'."""
    return "sha256:" + sha256_hex(canonical_json(obj))


def content_ref(data: bytes) -> str:
    """Content address for a retained raw blob."""
    return "sha256:" + sha256_hex(data)


def short(ref: str, length: int = 12) -> str:
    """Abbreviate a digest for display, keeping any 'sha256:' prefix intact."""
    body = ref.split(":", 1)[-1]
    return body[:length]
