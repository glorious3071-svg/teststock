"""Text normalization and deduplication for news articles."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = text.replace("\u3000", " ").replace("&nbsp;", " ")
    return _WS_RE.sub(" ", text).strip()


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    return normalize_text(text)


def content_hash(
    source: str,
    title: str,
    body_text: str | None,
    pub_time: datetime | None,
) -> str:
    pub_key = pub_time.strftime("%Y%m%d%H%M") if pub_time else "unknown"
    title_norm = normalize_text(title)[:120]
    body_norm = normalize_text(body_text)[:200]
    key = f"{source}|{pub_key}|{title_norm}|{body_norm}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = str(value).strip()
    if cleaned.endswith(".000"):
        cleaned = cleaned[:-4]
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None
