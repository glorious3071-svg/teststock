#!/usr/bin/env python3
"""Download official PBoC monetary-policy-report PDFs and store extracted text."""

from __future__ import annotations

import argparse
import html
import io
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection


USER_AGENT = "teststock-research/1.0 (official PBoC report text sync)"


def official_pboc_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "pbc.gov.cn" or host.endswith(".pbc.gov.cn")


def extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def discover_pdf_url(session: requests.Session, page_url: str, timeout: float) -> str | None:
    response = session.get(page_url, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    matches = re.findall(r"href=[\"']([^\"']+\.pdf(?:\?[^\"']*)?)[\"']", response.text, re.I)
    for match in matches:
        candidate = urljoin(page_url, html.unescape(match))
        if official_pboc_url(candidate):
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--minimum-chars", type=int, default=1000)
    args = parser.parse_args()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            condition = "1=1" if args.force else "content_html IS NULL"
            sql = (
                "SELECT pub_date,title,url,pdf_url FROM pboc_monetary_policy "
                f"WHERE {condition} AND pdf_url IS NOT NULL ORDER BY pub_date"
            )
            if args.limit is not None:
                sql += " LIMIT %s"
                cur.execute(sql, (args.limit,))
            else:
                cur.execute(sql)
            rows = cur.fetchall()

        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        fetched = extracted = skipped = failed = 0
        discovered = 0
        for pub_date, title, page_url, pdf_url in rows:
            url = str(pdf_url)
            if not official_pboc_url(url):
                skipped += 1
                print(f"SKIP non-official URL {pub_date} {url}")
                continue
            try:
                response = session.get(url, timeout=args.timeout)
                if response.status_code == 404 and page_url:
                    replacement = discover_pdf_url(
                        session, str(page_url), args.timeout
                    )
                    if replacement and replacement != url:
                        url = replacement
                        response = session.get(url, timeout=args.timeout)
                        discovered += 1
                response.raise_for_status()
                fetched += 1
                text = extract_pdf_text(response.content)
                if len(text) < args.minimum_chars:
                    raise ValueError(f"extracted only {len(text)} characters")
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE pboc_monetary_policy
                        SET content_html=%s, pdf_url=%s,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE pub_date=%s
                        """,
                        (text, url, pub_date),
                    )
                conn.commit()
                extracted += 1
                print(f"OK {pub_date} chars={len(text)} {title}")
            except Exception as exc:
                conn.rollback()
                failed += 1
                print(f"FAIL {pub_date} {type(exc).__name__}: {exc}")
        print(
            f"Done: rows={len(rows)} fetched={fetched} extracted={extracted} "
            f"discovered={discovered} skipped={skipped} failed={failed}"
        )
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
