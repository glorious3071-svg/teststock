#!/usr/bin/env python3
"""Import East Money industry research metadata into broker_research_report."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests

from db.connection import get_connection

API_URL = "https://reportapi.eastmoney.com/report/list"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; teststock-research-import/1.0)",
    "Referer": "https://data.eastmoney.com/report/",
}


def parse_dt(raw: Any):
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith(".000"):
        text = text[:-4]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def fetch_page(start: str, end: str, page_no: int, page_size: int, retries: int = 3) -> tuple[list[dict[str, Any]], int, int]:
    params = {
        "industryCode": "*",
        "pageSize": page_size,
        "beginTime": start,
        "endTime": end,
        "pageNo": page_no,
        "qType": 1,
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(1.5 * attempt)
    else:
        raise RuntimeError(f"failed to fetch page {page_no}") from last_error
    payload = resp.json()
    data = payload.get("data") or []
    if isinstance(data, dict):
        rows = data.get("list") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return rows, int(payload.get("TotalPage") or 0), int(payload.get("hits") or 0)


def import_range(start: str, end: str, *, page_size: int, max_pages: int, sleep: float) -> dict[str, int]:
    stats = {"fetched": 0, "inserted": 0, "deleted_old": 0}
    values = []
    total_pages = max_pages
    for page_no in range(1, max_pages + 1):
        rows, api_pages, hits = fetch_page(start, end, page_no, page_size)
        if page_no == 1:
            total_pages = min(api_pages or 1, max_pages)
            print(f"range={start}..{end} hits={hits} api_pages={api_pages} importing_pages={total_pages}")
        if not rows:
            break
        stats["fetched"] += len(rows)
        for row in rows:
            report_date = parse_dt(row.get("publishDate") or row.get("date"))
            title = str(row.get("title") or "").strip()
            if not report_date or not title:
                continue
            author = row.get("author") or row.get("researcher") or ""
            if isinstance(author, list):
                author = ",".join(str(x) for x in author if x)
            values.append(
                (
                    report_date,
                    title[:500],
                    str(author or "")[:500],
                    str(row.get("orgName") or row.get("orgSName") or "")[:200],
                    str(row.get("industryName") or row.get("industry") or "")[:100],
                    str(row.get("stockName") or "")[:100],
                    str(row.get("stockCode") or "")[:20],
                    str(row.get("emRatingName") or row.get("emRating") or "")[:50],
                    str(row.get("summary") or "") or None,
                    None,
                    "industry",
                    "eastmoney_api",
                )
            )
        if page_no >= total_pages:
            break
        time.sleep(sleep)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM broker_research_report
                WHERE source='eastmoney_api'
                  AND report_type='industry'
                  AND report_date BETWEEN %s AND %s
                """,
                (start, end),
            )
            stats["deleted_old"] = cur.rowcount
            if values:
                cur.executemany(
                    """
                    INSERT INTO broker_research_report
                        (report_date, title, author, org_name, industry, stock_name,
                         stock_code, rating, summary, content, report_type, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    values,
                )
                stats["inserted"] = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=120)
    parser.add_argument("--sleep", type=float, default=0.15)
    args = parser.parse_args()
    stats = import_range(
        args.date_from,
        args.date_to,
        page_size=args.page_size,
        max_pages=args.max_pages,
        sleep=args.sleep,
    )
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
