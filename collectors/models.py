"""Shared data models for collectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawArticle:
    source: str
    category: str
    title: str
    body_text: str | None = None
    pub_time: datetime | None = None
    url: str | None = None
    author: str | None = None
    lang: str = "zh"
    extra_json: dict[str, Any] | None = None
    fetch_status: str = "ok"


@dataclass
class CollectResult:
    collector: str
    fetched: int = 0
    inserted: int = 0
    skipped_dup: int = 0
    error_msg: str | None = None

    @property
    def status(self) -> str:
        if self.error_msg and self.inserted == 0 and self.fetched == 0:
            return "failed"
        if self.error_msg:
            return "partial"
        return "success"
