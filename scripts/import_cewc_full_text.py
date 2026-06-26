#!/usr/bin/env python3
"""Import CEWC full-text articles into MySQL.

Inputs:
  - data/cewc_full_text/{apply_year}.md  (one file per year, plain text body)
  - Optional YAML-ish front matter at top:
        ---
        meeting_date: 2008-12-10
        source_url: https://...
        source_name: xinhuanet
        ---

Fallback：对于目录里缺失年份，从 cewc_annual 拼接 theme + tone + fiscal_policy
+ monetary_policy + primary_task + keywords + summary 作为「半全文」入库，
source_name 标 'derived_from_cewc_annual'，保证 21 年都有内容供 LLM 处理。

用法：
    python3 scripts/import_cewc_full_text.py            # 处理 data/cewc_full_text/ 下所有 .md
    python3 scripts/import_cewc_full_text.py --years 2008,2009  # 仅处理指定年份
    python3 scripts/import_cewc_full_text.py --no-fallback      # 关闭兜底（仅入 .md 文件）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

FULL_TEXT_DIR = ROOT / "data" / "cewc_full_text"
SCHEMA_FILE = ROOT / "sql" / "cewc_full_text_schema.sql"
APPLY_YEAR_RANGE = range(2006, 2027)  # 与 cewc_annual 覆盖一致


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host":     os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


def apply_schema(conn: pymysql.connections.Connection) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def parse_front_matter(text: str) -> tuple[dict, str]:
    """解析顶部 --- ... --- 块（简单 key: value 行），返回 (meta_dict, body)"""
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        return {}, text
    meta_block, body = match.group(1), match.group(2)
    meta: dict = {}
    for line in meta_block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body.lstrip()


def load_md_for_year(year: int) -> tuple[str, dict] | None:
    path = FULL_TEXT_DIR / f"{year}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)
    return body, meta


def build_fallback_text(cur, year: int) -> tuple[str, dict] | None:
    """从 cewc_annual 拼接半全文作为兜底"""
    cur.execute(
        """
        SELECT meeting_year, meeting_start, meeting_end, theme, tone,
               fiscal_policy, monetary_policy, keywords, summary,
               primary_task, source_url
        FROM cewc_annual WHERE apply_year = %s
        """,
        (year,),
    )
    row = cur.fetchone()
    if not row:
        return None
    (meeting_year, meeting_start, meeting_end, theme, tone,
     fiscal, monetary, keywords, summary, primary_task, source_url) = row

    parts = [f"【中央经济工作会议 — {year} 年实施纲要（半全文：基于 cewc_annual 结构化字段拼接）】", ""]
    if meeting_year:
        parts.append(f"会议年份：{meeting_year} 年")
    if meeting_start and meeting_end:
        parts.append(f"会议时间：{meeting_start} 至 {meeting_end}")
    if theme:
        parts.append(f"主题：{theme}")
    if tone:
        parts.append(f"总基调：{tone}")
    if fiscal:
        parts.append(f"财政政策：{fiscal}")
    if monetary:
        parts.append(f"货币政策：{monetary}")
    if primary_task:
        parts.append(f"首要任务：{primary_task}")
    if keywords:
        parts.append(f"关键词：{keywords}")
    if summary:
        parts.append("")
        parts.append("摘要：")
        parts.append(summary)
    body = "\n".join(parts)
    meta = {
        "source_name": "derived_from_cewc_annual",
        "source_url": source_url,
        "meeting_date": meeting_end.isoformat() if meeting_end else None,
    }
    return body, meta


def upsert_full_text(cur, year: int, body: str, meta: dict) -> None:
    sql = """
        INSERT INTO cewc_full_text
            (apply_year, meeting_date, raw_text, source_url, source_name, text_bytes)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            meeting_date = VALUES(meeting_date),
            raw_text     = VALUES(raw_text),
            source_url   = VALUES(source_url),
            source_name  = VALUES(source_name),
            text_bytes   = VALUES(text_bytes)
    """
    cur.execute(sql, (
        year,
        meta.get("meeting_date") or None,
        body,
        meta.get("source_url") or None,
        meta.get("source_name") or "manual",
        len(body.encode("utf-8")),
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Import CEWC full-text into MySQL")
    parser.add_argument("--years", default=None,
                        help="逗号分隔的年份列表，如 2008,2009；默认全部")
    parser.add_argument("--no-fallback", action="store_true",
                        help="关闭兜底，缺失年份不入库")
    args = parser.parse_args()

    if args.years:
        target_years = [int(y) for y in args.years.split(",")]
    else:
        target_years = list(APPLY_YEAR_RANGE)

    FULL_TEXT_DIR.mkdir(parents=True, exist_ok=True)

    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        ok_md, ok_fallback, skipped = [], [], []
        with conn.cursor() as cur:
            for year in target_years:
                loaded = load_md_for_year(year)
                if loaded:
                    body, meta = loaded
                    upsert_full_text(cur, year, body, meta)
                    ok_md.append((year, len(body.encode("utf-8")),
                                  meta.get("source_name", "manual")))
                    continue
                if args.no_fallback:
                    skipped.append(year)
                    continue
                fb = build_fallback_text(cur, year)
                if fb is None:
                    skipped.append(year)
                    continue
                body, meta = fb
                upsert_full_text(cur, year, body, meta)
                ok_fallback.append((year, len(body.encode("utf-8"))))
        conn.commit()

        print(f"=== 入库结果 ===")
        print(f"\n[OK] 来自 .md 文件（{len(ok_md)} 年）:")
        for y, n, src in ok_md:
            print(f"  {y}: {n:>6} bytes  src={src}")
        print(f"\n[FALLBACK] 来自 cewc_annual 拼接（{len(ok_fallback)} 年）:")
        for y, n in ok_fallback:
            print(f"  {y}: {n:>6} bytes  src=derived_from_cewc_annual")
        if skipped:
            print(f"\n[SKIP] 未入库（{len(skipped)} 年）: {skipped}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
