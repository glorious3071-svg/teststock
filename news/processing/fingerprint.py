"""Event fingerprints for cross-source clustering."""

from __future__ import annotations

import hashlib
import re

from collectors.dedup import normalize_text

_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def title_tokens(title: str) -> set[str]:
    norm = normalize_text(title).lower()
    compact = _PUNCT_RE.sub("", norm)
    tokens: set[str] = set()
    # Character bigrams work for Chinese titles without whitespace
    for i in range(len(compact) - 1):
        bg = compact[i : i + 2]
        if len(bg) == 2:
            tokens.add(bg)
    # Latin/number words split on punctuation boundaries
    for part in _PUNCT_RE.sub(" ", norm).split():
        if len(part) >= 2:
            tokens.add(part)
    return tokens


def event_fingerprint(title: str) -> str:
    """Source-agnostic fingerprint from normalized title prefix."""
    norm = normalize_text(title)[:120]
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


def title_similarity(a: str, b: str) -> float:
    """Token Jaccard similarity in [0, 1]."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
