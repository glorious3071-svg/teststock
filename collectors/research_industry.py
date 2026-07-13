"""East Money industry research reports (recent pages)."""

from __future__ import annotations

from datetime import date, datetime

import requests

from collectors.base import BaseCollector, RawArticle
from collectors.dedup import normalize_text, parse_datetime

API_URL = "https://reportapi.eastmoney.com/report/list"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; teststock-research-collector/1.0)",
    "Referer": "https://data.eastmoney.com/report/",
}
QTYPE_INDUSTRY = 1


class IndustryResearchCollector(BaseCollector):
    name = "research_industry"
    category = "research"
    tier = "daily"
    request_sleep = 0.8

    def __init__(self, *, max_pages: int = 5) -> None:
        self.max_pages = max_pages

    def fetch(self, *, since: datetime | None = None) -> list[RawArticle]:
        year = date.today().year
        articles: list[RawArticle] = []
        total_pages = 1

        for page_no in range(1, self.max_pages + 1):
            payload = self._fetch_page(year, page_no)
            if page_no == 1:
                total_pages = min(payload.get("total_pages") or 1, self.max_pages)
            for report in payload.get("reports") or []:
                article = self._to_article(report, since)
                if article:
                    articles.append(article)
            if page_no >= total_pages:
                break
        return articles

    def _fetch_page(self, year: int, page_no: int) -> dict:
        params = {
            "industryCode": "*",
            "pageSize": 100,
            "beginTime": f"{year}-01-01",
            "endTime": f"{year}-12-31",
            "pageNo": page_no,
            "qType": QTYPE_INDUSTRY,
        }
        resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        reports = []
        total_pages = 0
        if isinstance(data.get("data"), list):
            reports = data["data"]
            total_pages = int(data.get("TotalPage") or 0)
        elif isinstance(data.get("data"), dict):
            reports = data["data"].get("list") or []
            total_pages = int(data.get("TotalPage") or 0)
        return {"reports": reports, "total_pages": total_pages}

    def _to_article(self, report: dict, since: datetime | None) -> RawArticle | None:
        title = normalize_text(str(report.get("title") or ""))
        if not title:
            return None
        pub_time = parse_datetime(
            str(report.get("publishDate") or report.get("date") or "")
        )
        if since and pub_time and pub_time < since:
            return None
        summary = normalize_text(str(report.get("summary") or "")) or None
        authors = report.get("author") or []
        if isinstance(authors, list):
            author = ", ".join(str(a) for a in authors if a)[:200] or None
        else:
            author = str(authors)[:200] if authors else None
        return RawArticle(
            source="eastmoney_research",
            category=self.category,
            title=title,
            body_text=summary,
            pub_time=pub_time,
            author=author,
            extra_json={
                "info_code": report.get("infoCode"),
                "industry": report.get("industryName") or report.get("industry"),
                "org_name": report.get("orgName"),
                "rating": report.get("emRatingName") or report.get("emRating"),
                "report_type": "industry",
            },
        )
