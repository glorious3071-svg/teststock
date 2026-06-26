#!/usr/bin/env python3
"""Scrape NDRC policy documents from www.ndrc.gov.cn into npr_policy table.

Accessible sections (static HTML with pagination):
  fzggwl: 发展改革委令     (~9 pages)
  ghxwj:  规范性文件       (~8 pages)
  gg:     公告             (~20 pages)
  tz:     通知             (~20 pages)

Each section has 25 documents/page. Total ~1400 documents.

URL pattern:
  List page 1:  https://www.ndrc.gov.cn/xxgk/zcfb/{section}/
  List page N:  https://www.ndrc.gov.cn/xxgk/zcfb/{section}/index_{N-1}.html
  Document:     https://www.ndrc.gov.cn/xxgk/zcfb/{section}/{YYYYMM}/t{date}_{id}.html

Usage:
  python scripts/scrape_gov_policy.py               # full scrape all sections
  python scripts/scrape_gov_policy.py --section tz  # single section
  python scripts/scrape_gov_policy.py --dry-run     # print without inserting
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

NDRC_BASE = "https://www.ndrc.gov.cn"
NDRC_ZCFB = f"{NDRC_BASE}/xxgk/zcfb"
USER_AGENT = "Mozilla/5.0 (compatible; teststock-ndrc-scraper/1.0)"
REQUEST_SLEEP = 0.8
FETCH_TIMEOUT = 30

# Section code → (Chinese label, default puborg)
SECTIONS: dict[str, tuple[str, str]] = {
    "fzggwl": ("发展改革委令", "国家发展改革委"),
    "ghxwj": ("规范性文件", "国家发展改革委"),
    "gg": ("公告", "国家发展改革委"),
    "tz": ("通知", "国家发展改革委"),
}

# Regex to extract document number from title (发文字号)
PCODE_RE = re.compile(
    r"（\d{4}）\w+字第?\d+号"
    r"|发改\w+〔\d+〕\d+号"
    r"|发改\w+规〔\d+〕\d+号"
    r"|发改\w+\[\d+\]\d+号"
    r"|\d{4}年第\d+号令"
    r"|国令第\d+号"
)


@dataclass
class PolicyDoc:
    title: str
    url: str
    pubtime: str | None
    pcode: str | None
    puborg: str
    ptype: str
    content_html: str | None = None


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


def fetch_html(url: str, *, timeout: int = FETCH_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        for enc in ("utf-8", "gbk", "gb2312"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    return raw.decode("utf-8", errors="replace")


def _meta(html: str, name: str) -> str | None:
    m = re.search(
        r'<meta\s+name=["\']' + re.escape(name) + r'["\'][^>]*content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ) or re.search(
        r'<meta\s+content=["\']([^"\']+)["\'][^>]*name=["\']' + re.escape(name) + r'["\']',
        html, re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _extract_pcode(text: str) -> str | None:
    m = PCODE_RE.search(text)
    return m.group(0) if m else None


def parse_list_page(html: str, section: str, base_url: str) -> list[dict]:
    """Extract document entries from a list page."""
    ptype_label, default_puborg = SECTIONS[section]
    docs = []
    # Match document links: relative paths like ./YYYYMM/tYYYYMMDD_ID.html
    pattern = re.compile(
        r'<li><a\s+href=["\'](\./\d{6}/t(\d{8})_\d+\.html)["\'][^>]*title=["\']([^"\']+)["\'][^>]*>',
        re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        rel_url, date_str, title = m.group(1), m.group(2), m.group(3).strip()
        doc_url = f"{base_url}/{rel_url.lstrip('./')}"
        pcode = _extract_pcode(title)
        # Date is embedded in filename: tYYYYMMDD_ID.html
        pubtime = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        docs.append({
            "title": title,
            "url": doc_url,
            "pubtime": pubtime,
            "pcode": pcode,
            "puborg": default_puborg,
            "ptype": ptype_label,
        })
    return docs


def count_pages(html: str) -> int:
    """Extract total page count from createPageHTML JS call."""
    m = re.search(r"createPageHTML\((\d+)", html)
    return int(m.group(1)) if m else 1


def fetch_doc_metadata(url: str) -> tuple[str | None, str | None]:
    """Fetch individual document page and return (puborg, content_html)."""
    try:
        html = fetch_html(url)
    except (urllib.error.URLError, OSError):
        return None, None

    puborg = _meta(html, "ContentSource") or _meta(html, "Author")

    # Store the full HTML to capture any inline body text
    body_m = re.search(r'<div[^>]+class=["\']article[_\s]l[^"\']*["\'][^>]*>(.*?)</div', html, re.DOTALL)
    content_html = body_m.group(0) if body_m else None

    return puborg, content_html


def scrape_section(section: str, *, fetch_content: bool = True, max_pages: int | None = None) -> list[PolicyDoc]:
    """Scrape all pages in a section and return PolicyDoc list."""
    ptype_label, default_puborg = SECTIONS[section]
    base_url = f"{NDRC_ZCFB}/{section}"

    # Page 1 (no suffix)
    print(f"  [{section}] fetching page 1...")
    html = fetch_html(f"{base_url}/")
    total_pages = count_pages(html)
    if max_pages:
        total_pages = min(total_pages, max_pages)
    print(f"  [{section}] {total_pages} pages total")

    all_meta: list[dict] = parse_list_page(html, section, base_url)
    time.sleep(REQUEST_SLEEP)

    for page_idx in range(1, total_pages):
        page_url = f"{base_url}/index_{page_idx}.html"
        print(f"  [{section}] fetching page {page_idx + 1}/{total_pages}...")
        try:
            html = fetch_html(page_url)
        except urllib.error.URLError as exc:
            print(f"  [{section}] warn: page {page_idx + 1} fetch failed: {exc}")
            break
        all_meta.extend(parse_list_page(html, section, base_url))
        time.sleep(REQUEST_SLEEP)

    docs: list[PolicyDoc] = []
    for i, meta in enumerate(all_meta):
        print(f"  [{section}] doc {i+1}/{len(all_meta)}: {meta['title'][:50]}")
        pubtime = meta["pubtime"]
        puborg = meta["puborg"]
        content_html = None

        if fetch_content:
            fetched_puborg, content_html = fetch_doc_metadata(meta["url"])
            if fetched_puborg:
                puborg = fetched_puborg
            time.sleep(REQUEST_SLEEP)

        docs.append(PolicyDoc(
            title=meta["title"],
            url=meta["url"],
            pubtime=pubtime,
            pcode=meta["pcode"] or _extract_pcode(meta["title"]),
            puborg=puborg or default_puborg,
            ptype=ptype_label,
            content_html=content_html,
        ))

    return docs


def apply_schema(conn) -> None:
    """Ensure npr_policy table exists."""
    schema_path = ROOT / "sql" / "corpus_schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def upsert_docs(conn, docs: list[PolicyDoc]) -> tuple[int, int]:
    """Insert docs, skip duplicates by URL. Returns (inserted, skipped)."""
    inserted = skipped = 0
    sql = """
        INSERT INTO npr_policy (pubtime, title, url, pcode, puborg, ptype, content_html)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            pubtime = COALESCE(VALUES(pubtime), pubtime),
            puborg  = COALESCE(VALUES(puborg), puborg),
            content_html = COALESCE(VALUES(content_html), content_html),
            updated_at = CURRENT_TIMESTAMP
    """
    # Need unique index on url for ON DUPLICATE KEY to work
    with conn.cursor() as cur:
        # Ensure unique index on url
        try:
            cur.execute(
                "ALTER TABLE npr_policy ADD UNIQUE KEY idx_npr_url (url(500))"
            )
            conn.commit()
        except pymysql.err.OperationalError:
            pass  # Index already exists

        for doc in docs:
            cur.execute(sql, (
                doc.pubtime, doc.title, doc.url, doc.pcode,
                doc.puborg, doc.ptype, doc.content_html,
            ))
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        conn.commit()

    return inserted, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="爬取国家发改委政策文件到 npr_policy 表")
    parser.add_argument("--section", choices=list(SECTIONS.keys()), help="仅爬取指定板块")
    parser.add_argument("--no-content", action="store_true", help="不抓正文（只抓列表元数据）")
    parser.add_argument("--max-pages", type=int, help="每板块最多爬取页数（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="不写入数据库，仅打印")
    args = parser.parse_args()

    sections = [args.section] if args.section else list(SECTIONS.keys())

    conn = None if args.dry_run else pymysql.connect(**mysql_config())

    try:
        if conn:
            apply_schema(conn)

        total_inserted = total_skipped = 0
        for section in sections:
            print(f"\n{'='*50}")
            print(f"板块: {SECTIONS[section][0]} ({section})")
            docs = scrape_section(
                section,
                fetch_content=not args.no_content,
                max_pages=args.max_pages,
            )
            print(f"  爬取完成: {len(docs)} 条")

            if args.dry_run:
                for doc in docs[:3]:
                    print(f"  [DRY] {doc.pubtime} | {doc.puborg} | {doc.title[:60]}")
            else:
                inserted, skipped = upsert_docs(conn, docs)
                total_inserted += inserted
                total_skipped += skipped
                print(f"  入库: 新增 {inserted} 条, 跳过 {skipped} 条")

        if not args.dry_run:
            print(f"\n汇总: 新增 {total_inserted} 条, 跳过 {total_skipped} 条")

    finally:
        if conn:
            conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
