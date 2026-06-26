#!/usr/bin/env python3.11
"""
补充研报正文内容

分两阶段：
1. 阶段一：下载元数据（已完成）
2. 阶段二：爬取研报详情页，补充正文

用法：
  # 补充 2024 年研报正文（单线程）
  python3.11 scripts/supplement_report_content.py --year 2024 --type stock

  # 补充 2020-2024 年所有研报正文（5 线程）
  python3.11 scripts/supplement_report_content.py --years 2020,2021,2022,2023,2024 --type all --threads 5

  # 只补充没有正文的研报
  python3.11 scripts/supplement_report_content.py --year 2024 --type stock --skip-existing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pymysql
import requests

ROOT = Path(__file__).resolve().parents[1]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/report/",
}

REQUEST_DELAY = 1.0  # 请求间隔（秒）


def get_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
        charset="utf8mb4",
    )


def fetch_reports_to_update(conn, year: int, report_type: str, skip_existing: bool) -> list[dict]:
    """获取需要更新正文的研报列表"""
    cur = conn.cursor()

    if skip_existing:
        # 只查询没有正文的
        sql = """
            SELECT id, title, org_name, report_date, report_type
            FROM broker_research_report
            WHERE YEAR(report_date) = %s
              AND (report_type = %s OR %s = 'all')
              AND (content IS NULL OR content = '')
            ORDER BY report_date DESC
        """
        cur.execute(sql, (year, report_type, report_type))
    else:
        # 查询所有
        sql = """
            SELECT id, title, org_name, report_date, report_type
            FROM broker_research_report
            WHERE YEAR(report_date) = %s
              AND (report_type = %s OR %s = 'all')
            ORDER BY report_date DESC
        """
        cur.execute(sql, (year, report_type, report_type))

    rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "org_name": r[2],
            "report_date": r[3],
            "report_type": r[4],
        }
        for r in rows
    ]


def search_report_content(title: str) -> str | None:
    """
    搜索研报正文

    由于东方财富列表 API 不返回正文，需要：
    1. 用标题搜索研报详情
    2. 爬取详情页 HTML 提取正文

    目前东方财富反爬较严，暂时返回 None
    """
    # TODO: 实现正文爬取
    # 目前东方财富研报 API 限制较多，需要：
    # 1. 使用代理 IP
    # 2. 使用浏览器指纹
    # 3. 控制请求频率

    return None


def update_report_content(conn, report_id: int, content: str):
    """更新研报正文"""
    cur = conn.cursor()
    cur.execute(
        "UPDATE broker_research_report SET content = %s WHERE id = %s",
        (content, report_id),
    )
    conn.commit()


def process_report(conn, report: dict) -> bool:
    """处理单条研报"""
    title = report["title"]
    report_id = report["id"]

    # 尝试获取正文
    content = search_report_content(title)

    if content:
        update_report_content(conn, report_id, content)
        print(f"  ✓ 更新 ID={report_id}: {title[:40]}")
        return True
    else:
        print(f"  ✗ 跳过 ID={report_id}: {title[:40]} (未找到正文)")
        return False


def main():
    parser = argparse.ArgumentParser(description="补充研报正文内容")
    parser.add_argument("--year", type=int, default=2024, help="年份")
    parser.add_argument("--years", type=str, default="", help="多个年份，逗号分隔")
    parser.add_argument("--type", choices=["macro", "industry", "stock", "all"],
                        default="stock", help="研报类型")
    parser.add_argument("--threads", type=int, default=1, help="线程数")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已有正文的研报")
    parser.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数")
    args = parser.parse_args()

    global REQUEST_DELAY
    REQUEST_DELAY = args.delay

    years = [int(y.strip()) for y in args.years.split(",")] if args.years else [args.year]

    conn = get_conn()
    total_updated = 0
    total_skipped = 0

    for year in years:
        print(f"\n=== {year} 年 {args.type} 研报 ===")

        reports = fetch_reports_to_update(conn, year, args.type, args.skip_existing)
        print(f"  待处理：{len(reports)} 条")

        if args.threads == 1:
            for report in reports:
                if process_report(conn, report):
                    total_updated += 1
                else:
                    total_skipped += 1
                time.sleep(REQUEST_DELAY)
        else:
            # 多线程处理
            with ThreadPoolExecutor(max_workers=args.threads) as executor:
                futures = {
                    executor.submit(process_report, conn, report): report
                    for report in reports
                }

                for future in as_completed(futures):
                    try:
                        if future.result():
                            total_updated += 1
                        else:
                            total_skipped += 1
                    except Exception as e:
                        print(f"  ✗ 处理失败：{e}")
                        total_skipped += 1

    print(f"\n{'=' * 60}")
    print(f"完成！")
    print(f"  更新：{total_updated} 条")
    print(f"  跳过：{total_skipped} 条")
    print(f"{'=' * 60}")

    conn.close()


if __name__ == "__main__":
    main()
