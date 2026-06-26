#!/usr/bin/env python3
"""Show CEWC tags for one or more years (latest prompt_version by default).

用法：
    python3 scripts/show_cewc_tags.py 2009
    python3 scripts/show_cewc_tags.py 2008 2009 2010
    python3 scripts/show_cewc_tags.py 2009 --version v1   # 指定 prompt 版本
    python3 scripts/show_cewc_tags.py --all               # 所有年份按 category 汇总
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv


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


def fetch_year_tags(cur, year: int, version: str | None) -> list[tuple]:
    """取该年最新一次 LLM 抽取的全部标签"""
    if version:
        cur.execute(
            """
            SELECT tag_category, tag_name, tag_value, confidence, evidence,
                   model_version, prompt_version, extracted_at
            FROM cewc_tags
            WHERE apply_year = %s AND prompt_version = %s
            ORDER BY tag_category, tag_name
            """,
            (year, version),
        )
        return cur.fetchall()

    # 取该年最新批次（同一 extracted_at 算一批）
    cur.execute(
        "SELECT MAX(extracted_at) FROM cewc_tags WHERE apply_year = %s",
        (year,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return []
    latest = row[0]
    cur.execute(
        """
        SELECT tag_category, tag_name, tag_value, confidence, evidence,
               model_version, prompt_version, extracted_at
        FROM cewc_tags
        WHERE apply_year = %s AND extracted_at = %s
        ORDER BY tag_category, tag_name
        """,
        (year, latest),
    )
    return cur.fetchall()


def show_year(cur, year: int, version: str | None) -> None:
    rows = fetch_year_tags(cur, year, version)
    if not rows:
        print(f"\n=== {year} 年 — 无标签数据 ===")
        return

    meta = rows[0]
    model, prompt, ts = meta[5], meta[6], meta[7]
    print(f"\n=== {year} 年 — {len(rows)} 条标签 "
          f"（model={model}, prompt={prompt}, 抽取于 {ts}）===")

    by_cat: dict[str, list] = defaultdict(list)
    for cat, name, val, conf, ev, *_ in rows:
        by_cat[cat].append((name, val, conf, ev))

    for cat in sorted(by_cat.keys()):
        print(f"\n  [{cat}]")
        for name, val, conf, ev in by_cat[cat]:
            conf_str = f"({conf:.2f})" if conf is not None else "(  - )"
            print(f"    {name:<28} = {val}  {conf_str}")
            if ev:
                ev_short = ev[:80] + ("…" if len(ev) > 80 else "")
                print(f"        ↳ {ev_short}")


def show_all_summary(cur) -> None:
    """按年份 + category 汇总最新批次标签数"""
    cur.execute(
        """
        SELECT t.apply_year, t.tag_category, COUNT(*) AS n
        FROM cewc_tags t
        JOIN (
          SELECT apply_year, MAX(extracted_at) AS latest
          FROM cewc_tags GROUP BY apply_year
        ) m ON t.apply_year = m.apply_year AND t.extracted_at = m.latest
        GROUP BY t.apply_year, t.tag_category
        ORDER BY t.apply_year, t.tag_category
        """
    )
    rows = cur.fetchall()
    if not rows:
        print("（无数据）")
        return
    by_year: dict[int, dict[str, int]] = defaultdict(dict)
    cats = set()
    for y, c, n in rows:
        by_year[int(y)][c] = int(n)
        cats.add(c)
    cats = sorted(cats)
    print(f"\n=== 跨年标签数汇总（最新批次） ===\n")
    hdr = f"{'year':<6}" + "".join(f"{c:>20}" for c in cats) + f"{'总计':>8}"
    print(hdr)
    print("-" * len(hdr))
    for y in sorted(by_year):
        row = f"{y:<6}"
        total = 0
        for c in cats:
            n = by_year[y].get(c, 0)
            total += n
            row += f"{n:>20}"
        row += f"{total:>8}"
        print(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show CEWC tags")
    parser.add_argument("years", nargs="*", type=int, help="年份列表")
    parser.add_argument("--version", default=None, help="指定 prompt 版本")
    parser.add_argument("--all", action="store_true", help="跨年汇总")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        with conn.cursor() as cur:
            if args.all:
                show_all_summary(cur)
                return
            if not args.years:
                # 默认显示所有年份概览
                show_all_summary(cur)
                return
            for y in args.years:
                show_year(cur, y, args.version)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
