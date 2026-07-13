"""CCTV evening news daily import."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import akshare as ak
import pymysql

from collectors.base import BaseCollector, RawArticle
from collectors.dedup import normalize_text
from db.connection import mysql_config


class CctvDailyCollector(BaseCollector):
    name = "cctv_daily"
    category = "policy"
    tier = "daily"
    request_sleep = 0.5

    def __init__(self, *, lookback_days: int = 3) -> None:
        self.lookback_days = lookback_days

    def fetch(self, *, since: datetime | None = None) -> list[RawArticle]:
        end = date.today()
        if since:
            start = since.date()
        else:
            start = end - timedelta(days=self.lookback_days)

        existing = self._existing_dates(start, end)
        articles: list[RawArticle] = []
        d = start
        while d <= end:
            if d not in existing:
                articles.extend(self._fetch_day(d))
            d += timedelta(days=1)
        return articles

    def _fetch_day(self, target: date) -> list[RawArticle]:
        date_str = target.strftime("%Y%m%d")
        try:
            df = ak.news_cctv(date=date_str)
        except Exception:
            return []
        if df is None or df.empty:
            return []

        pub_time = datetime.combine(target, datetime.min.time()).replace(hour=19, minute=0)
        articles: list[RawArticle] = []
        for _, row in df.iterrows():
            title = normalize_text(str(row.get("title", "") or ""))
            if not title:
                continue
            body = normalize_text(str(row.get("content", "") or "")) or None
            articles.append(
                RawArticle(
                    source="cctv",
                    category=self.category,
                    title=title,
                    body_text=body,
                    pub_time=pub_time,
                    extra_json={"news_date": target.isoformat()},
                )
            )
        return articles

    def _existing_dates(self, start: date, end: date) -> set[date]:
        conn = pymysql.connect(**mysql_config())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT DATE(pub_time)
                    FROM news_article
                    WHERE source = 'cctv'
                      AND pub_time >= %s AND pub_time <= %s
                    """,
                    (start, end + timedelta(days=1)),
                )
                return {row[0] for row in cur.fetchall()}
        finally:
            conn.close()
