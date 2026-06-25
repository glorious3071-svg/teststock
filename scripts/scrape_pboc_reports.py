#!/usr/bin/env python3
"""Scrape PBOC monetary policy reports from pbc.gov.cn into MySQL.

Tushare monetary_policy (doc 465) requires separate permission and returns 0 rows
via teajoin. Official list page is publicly accessible:
  https://www.pbc.gov.cn/zhengcehuobisi/125207/125227/125957/index.html
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

PBOC_INDEX = "https://www.pbc.gov.cn/zhengcehuobisi/125207/125227/125957/index.html"
PBOC_BASE = "https://www.pbc.gov.cn"
USER_AGENT = "Mozilla/5.0 (compatible; teststock-pboc-scraper/1.0)"
REQUEST_SLEEP = 0.6
SCHEMA_FILE = ROOT / "sql" / "corpus_schema.sql"

QUARTERLY_RE = re.compile(r"^\d{4}年第[一二三四1-4]季度中国货币政策执行报告$")


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def fetch_html(url: str, *, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def abs_url(path: str) -> str:
    if path.startswith("http"):
        return path
    return urllib.parse.urljoin(PBOC_BASE, path)


def parse_index(html: str) -> list[dict[str, str]]:
    reports: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{4,120})</a>', html):
        title = re.sub(r"\s+", "", title)
        if not QUARTERLY_RE.match(title):
            continue
        url = abs_url(href)
        if not url.endswith((".html", ".htm", "/")):
            url = url.rstrip("/") + "/index.html"
        elif url.endswith("/"):
            url = url + "index.html"
        if url in seen:
            continue
        seen.add(url)
        reports.append({"title": title, "url": url})
    return reports


def parse_detail(url: str) -> dict[str, str | None]:
    html = fetch_html(url)
    title_m = re.search(r'<meta name="ArticleTitle" content="([^"]+)"', html)
    pub_m = re.search(r'<meta name="PubDate" content="([^"]+)"', html)
    pdf_m = re.search(r'href="([^"]+\.pdf)"', html, re.I)
    title = title_m.group(1).strip() if title_m else None
    pub_date = pub_m.group(1).strip() if pub_m else None
    pdf_url = abs_url(pdf_m.group(1)) if pdf_m else None
    return {"title": title, "pub_date": pub_date, "url": url, "pdf_url": pdf_url}


def apply_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def upsert_reports(conn, rows: list[dict[str, str | None]]) -> int:
    sql = """
        INSERT INTO pboc_monetary_policy (pub_date, title, url, pdf_url, content_html)
        VALUES (%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            url=VALUES(url), pdf_url=VALUES(pdf_url)
    """
    data = [
        (r["pub_date"], r["title"], r["url"], r.get("pdf_url"), None)
        for r in rows
        if r.get("title") and r.get("pub_date")
    ]
    if not data:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, data)
    conn.commit()
    return len(data)


def scrape_reports(*, since_year: int = 2001, limit: int | None = None) -> list[dict[str, str | None]]:
    print(f"Fetching index: {PBOC_INDEX}")
    index_html = fetch_html(PBOC_INDEX)
    candidates = parse_index(index_html)
    candidates = [r for r in candidates if int(r["title"][:4]) >= since_year]
    candidates.sort(key=lambda r: r["title"])
    print(f"Found {len(candidates)} quarterly reports since {since_year}")

    if limit:
        candidates = candidates[:limit]

    rows: list[dict[str, str | None]] = []
    for i, item in enumerate(candidates, 1):
        try:
            detail = parse_detail(item["url"])
            detail["title"] = detail.get("title") or item["title"]
            rows.append(detail)
            print(
                f"  [{i}/{len(candidates)}] {detail['pub_date']} | {detail['title']}"
                + (" | PDF" if detail.get("pdf_url") else "")
            )
        except urllib.error.HTTPError as e:
            print(f"  [{i}/{len(candidates)}] HTTP {e.code} {item['title']}")
        except Exception as e:
            print(f"  [{i}/{len(candidates)}] ERR {item['title']}: {e}")
        time.sleep(REQUEST_SLEEP)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape PBOC monetary policy reports")
    parser.add_argument("--since-year", type=int, default=2001)
    parser.add_argument("--limit", type=int, help="limit number of reports (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="scrape only, do not write DB")
    args = parser.parse_args()

    rows = scrape_reports(since_year=args.since_year, limit=args.limit)
    print(f"\nScraped {len(rows)} reports with pub_date")

    if args.dry_run:
        for r in rows[:5]:
            print(r)
        return

    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        n = upsert_reports(conn, rows)
        print(f"Upserted pboc_monetary_policy: {n}")

        from macro.annual_snapshot import rebuild_annual_snapshots

        snap_n = rebuild_annual_snapshots(conn)
        print(f"Rebuilt macro_annual_snapshot: {snap_n} years")
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
