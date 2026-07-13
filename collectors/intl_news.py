"""International / macro financial news (Futu global + Eastmoney 财经早餐 fallback)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime

import akshare as ak

from collectors.base import BaseCollector
from collectors.models import RawArticle
from collectors.dedup import normalize_text, parse_datetime

FETCH_TIMEOUT_SEC = 30


def _call_with_timeout(fn, timeout: int = FETCH_TIMEOUT_SEC):
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except FutureTimeout:
        return None
    finally:
        ex.shutdown(wait=False)


class IntlNewsCollector(BaseCollector):
    name = "intl_cls"
    category = "intl"
    tier = "daily"
    request_sleep = 1.0

    def fetch(self, *, since: datetime | None = None) -> list[RawArticle]:
        articles: list[RawArticle] = []
        articles.extend(self._fetch_futu(since))
        articles.extend(self._fetch_cjzc(since))
        return articles

    def _fetch_cjzc(self, since: datetime | None) -> list[RawArticle]:
        df = _call_with_timeout(lambda: ak.stock_info_cjzc_em())
        if df is None or df.empty:
            return []
        out: list[RawArticle] = []
        for _, row in df.iterrows():
            title = normalize_text(str(row.get("标题", "") or ""))
            if not title:
                continue
            pub_time = parse_datetime(str(row.get("发布时间", "") or ""))
            if since and pub_time and pub_time < since:
                continue
            body = normalize_text(str(row.get("摘要", "") or "")) or None
            out.append(
                RawArticle(
                    source="cjzc_em",
                    category=self.category,
                    title=title,
                    body_text=body,
                    pub_time=pub_time,
                    url=str(row.get("链接", "") or "") or None,
                    extra_json={"provider": "东方财富财经早餐"},
                )
            )
        return out

    def _fetch_futu(self, since: datetime | None) -> list[RawArticle]:
        df = _call_with_timeout(lambda: ak.stock_info_global_futu())
        if df is None or df.empty:
            return []

        out: list[RawArticle] = []
        for _, row in df.iterrows():
            title = normalize_text(str(row.get("标题", "") or row.get("title", "") or ""))
            if not title:
                continue
            pub_time = parse_datetime(
                str(row.get("发布时间", "") or row.get("时间", "") or "")
            )
            if since and pub_time and pub_time < since:
                continue
            body = normalize_text(
                str(row.get("内容", "") or row.get("摘要", "") or "")
            ) or None
            out.append(
                RawArticle(
                    source="futu",
                    category=self.category,
                    title=title,
                    body_text=body,
                    pub_time=pub_time,
                    extra_json={"provider": "富途全球资讯"},
                )
            )
        return out
