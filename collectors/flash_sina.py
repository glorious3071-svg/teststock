"""Sina global financial flash news."""

from __future__ import annotations

from datetime import datetime

import akshare as ak

from collectors.base import BaseCollector, RawArticle
from collectors.dedup import normalize_text, parse_datetime


class SinaFlashCollector(BaseCollector):
    name = "sina_flash"
    category = "flash"
    tier = "flash"
    mirror_legacy = True
    request_sleep = 1.0

    def fetch(self, *, since: datetime | None = None) -> list[RawArticle]:
        df = ak.stock_info_global_sina()
        articles: list[RawArticle] = []
        for _, row in df.iterrows():
            pub_time = parse_datetime(str(row.get("时间", "") or ""))
            if since and pub_time and pub_time < since:
                continue
            content = normalize_text(str(row.get("内容", "") or ""))
            if not content:
                continue
            title = content[:200]
            articles.append(
                RawArticle(
                    source="sina",
                    category=self.category,
                    title=title,
                    body_text=content,
                    pub_time=pub_time,
                )
            )
        return articles
