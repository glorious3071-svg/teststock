"""East Money global financial flash news."""

from __future__ import annotations

from datetime import datetime

import akshare as ak

from collectors.base import BaseCollector
from collectors.models import RawArticle
from collectors.dedup import normalize_text, parse_datetime


class EastmoneyFlashCollector(BaseCollector):
    name = "eastmoney_flash"
    category = "flash"
    tier = "flash"
    mirror_legacy = True
    request_sleep = 1.0

    def fetch(self, *, since: datetime | None = None) -> list[RawArticle]:
        df = ak.stock_info_global_em()
        articles: list[RawArticle] = []
        for _, row in df.iterrows():
            pub_time = parse_datetime(str(row.get("发布时间", "") or ""))
            if since and pub_time and pub_time < since:
                continue
            title = normalize_text(str(row.get("标题", "") or ""))
            if not title:
                continue
            body = normalize_text(str(row.get("摘要", "") or "")) or None
            articles.append(
                RawArticle(
                    source="eastmoney",
                    category=self.category,
                    title=title,
                    body_text=body,
                    pub_time=pub_time,
                )
            )
        return articles
