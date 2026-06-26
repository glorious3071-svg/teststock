#!/usr/bin/env python3.11
"""
券商研报多线程下载器（直接写入数据库）

数据源：东方财富研报中心 API
URL: https://reportapi.eastmoney.com/report/list

支持类型：
  - qType=0: 宏观/策略研报
  - qType=1: 行业研报
  - qType=2: 个股研报

数据库：teststock.broker_research_report

用法：
  # 单线程下载并入库
  python3.11 scripts/import_research_reports.py --type stock --year 2023

  # 多线程下载并入库（5 个线程）
  python3.11 scripts/import_research_reports.py --type stock --year 2023 --threads 5

  # 批量下载多年
  python3.11 scripts/import_research_reports.py --type stock --years 2020,2021,2022,2023

  # 下载全部类型
  python3.11 scripts/import_research_reports.py --type all --year 2023 --threads 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
import requests

ROOT = Path(__file__).resolve().parents[1]

# API 配置
API_URL = "https://reportapi.eastmoney.com/report/list"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/report/",
}
PAGE_SIZE = 100
REQUEST_DELAY = 0.5


def get_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
        charset="utf8mb4",
    )


def ensure_table(conn):
    """创建表（如果不存在）"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS broker_research_report (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            report_date     DATE           NULL      COMMENT '研报日期',
            title           VARCHAR(500)   NULL      COMMENT '研报标题',
            author          VARCHAR(500)   NULL      COMMENT '分析师',
            org_name        VARCHAR(200)   NULL      COMMENT '机构名称',
            industry        VARCHAR(100)   NULL      COMMENT '行业',
            stock_name      VARCHAR(100)   NULL      COMMENT '股票名称',
            stock_code      VARCHAR(20)    NULL      COMMENT '股票代码',
            rating          VARCHAR(50)    NULL      COMMENT '评级',
            summary         TEXT           NULL      COMMENT '摘要',
            report_type     VARCHAR(20)    NULL      COMMENT '类型: macro/industry/stock',
            source          VARCHAR(50)    DEFAULT 'eastmoney' COMMENT '数据源',
            created_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_report_date (report_date),
            KEY idx_org_name (org_name),
            KEY idx_stock_code (stock_code),
            KEY idx_report_type (report_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='券商研报（东方财富数据源）'
    """)
    conn.commit()


def fetch_page(
    year: int,
    page_no: int,
    q_type: int = 0,
    max_retries: int = 3,
) -> dict[str, Any]:
    """抓取单页研报"""
    for attempt in range(max_retries):
        try:
            params = {
                "industryCode": "*",
                "pageSize": PAGE_SIZE,
                "beginTime": f"{year}-01-01",
                "endTime": f"{year}-12-31",
                "pageNo": page_no,
                "fields": "title,author,orgName,emRating,date,industry,industryCode,stockName,stockCode,summary",
                "qType": q_type,
            }

            resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)

            if resp.status_code != 200:
                print(f"    页码 {page_no}: 状态码 {resp.status_code}")
                time.sleep(REQUEST_DELAY * 2)
                continue

            data = resp.json()

            if "data" not in data:
                return {"page_no": page_no, "reports": [], "total_pages": 0, "error": "No data"}

            if isinstance(data["data"], list):
                reports = data["data"]
                total_pages = data.get("TotalPage", 0)
            elif isinstance(data["data"], dict) and "list" in data["data"]:
                reports = data["data"]["list"]
                total_pages = data.get("TotalPage", 0)
            else:
                reports = []
                total_pages = 0

            return {"page_no": page_no, "reports": reports, "total_pages": total_pages, "error": None}

        except Exception as e:
            print(f"    页码 {page_no} 第 {attempt + 1} 次尝试失败：{e}")
            if attempt < max_retries - 1:
                time.sleep(REQUEST_DELAY * 2)

    return {"page_no": page_no, "reports": [], "total_pages": 0, "error": "Max retries"}


def insert_reports(conn, reports: list[dict], q_type: int) -> int:
    """插入研报到数据库"""
    type_name = ["macro", "industry", "stock"][q_type]

    sql = """
        INSERT INTO broker_research_report
            (trade_date, title, abstr, report_type, author, name, ts_code, inst_csname, ind_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    rows = []
    for r in reports:
        try:
            trade_date = r.get("date", "")
            if trade_date:
                trade_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
            else:
                trade_date = None

            author = ",".join(r.get("author", [])) if r.get("author") else None

            rows.append((
                trade_date,
                r.get("title", ""),
                r.get("summary", ""),
                type_name,
                author[:200] if author else None,
                r.get("stockName", ""),
                r.get("stockCode", ""),
                r.get("orgName", ""),
                r.get("industry", ""),
            ))
        except Exception as e:
            print(f"    解析错误：{e}")
            continue

    if rows:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        conn.commit()

    return len(rows)


def fetch_and_insert_year(
    year: int,
    q_type: int = 0,
    max_pages: int = 100,
    threads: int = 1,
) -> int:
    """抓取并入库指定年份的研报"""
    type_name = ["macro", "industry", "stock"][q_type]
    print(f"\n=== {year} 年 {type_name} 研报 ===")

    # 获取总页数
    first_page = fetch_page(year, 1, q_type)
    if first_page["error"]:
        print(f"  获取总页数失败：{first_page['error']}")
        return 0

    total_pages = min(first_page["total_pages"], max_pages)
    print(f"  总页数：{total_pages}")

    conn = get_conn()
    ensure_table(conn)

    total_inserted = insert_reports(conn, first_page["reports"], q_type)

    if threads == 1:
        # 单线程
        for page_no in range(2, total_pages + 1):
            result = fetch_page(year, page_no, q_type)
            if not result["error"]:
                n = insert_reports(conn, result["reports"], q_type)
                total_inserted += n
                if page_no % 10 == 0:
                    print(f"  已入库 {total_inserted} 条 / {total_pages} 页")
            time.sleep(REQUEST_DELAY)
    else:
        # 多线程
        all_pages = []
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {
                executor.submit(fetch_page, year, page_no, q_type): page_no
                for page_no in range(2, total_pages + 1)
            }

            for future in as_completed(futures):
                result = future.result()
                if not result["error"]:
                    all_pages.append(result["reports"])

        # 批量入库
        for page_reports in all_pages:
            n = insert_reports(conn, page_reports, q_type)
            total_inserted += n

    conn.close()
    print(f"  完成：共入库 {total_inserted} 条")
    return total_inserted


def main():
    parser = argparse.ArgumentParser(description="多线程下载券商研报并入库")
    parser.add_argument("--type", choices=["macro", "industry", "stock", "all"],
                        default="stock", help="研报类型")
    parser.add_argument("--year", type=int, default=2023, help="年份")
    parser.add_argument("--years", type=str, default="", help="多个年份，逗号分隔")
    parser.add_argument("--threads", type=int, default=1, help="线程数（建议 3-5）")
    parser.add_argument("--max-pages", type=int, default=100, help="最大页数")
    parser.add_argument("--delay", type=float, default=0.5, help="请求间隔秒数")
    args = parser.parse_args()

    global REQUEST_DELAY
    REQUEST_DELAY = args.delay

    years = [int(y.strip()) for y in args.years.split(",")] if args.years else [args.year]

    type_map = {"macro": [0], "industry": [1], "stock": [2], "all": [0, 1, 2]}

    total = 0
    start_time = time.time()

    # 创建表
    conn = get_conn()
    ensure_table(conn)
    conn.close()

    for year in years:
        for q_type in type_map[args.type]:
            n = fetch_and_insert_year(year, q_type, args.max_pages, args.threads)
            total += n

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"完成！")
    print(f"  总入库数：{total}")
    print(f"  耗时：{elapsed:.1f} 秒")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
