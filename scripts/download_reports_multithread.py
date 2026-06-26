#!/usr/bin/env python3.11
"""
券商研报多线程下载器

数据源：东方财富研报中心 API
URL: https://reportapi.eastmoney.com/report/list

支持类型：
  - qType=0: 宏观/策略研报（注：实际返回个股研报，宏观需爬网页）
  - qType=1: 行业研报
  - qType=2: 个股研报

用法：
  # 单线程下载（默认）
  python3.11 scripts/download_reports_multithread.py --type stock --year 2023

  # 多线程下载（5 个线程）
  python3.11 scripts/download_reports_multithread.py --type stock --year 2023 --threads 5

  # 批量下载多年
  python3.11 scripts/download_reports_multithread.py --type stock --years 2020,2021,2022,2023

  # 下载全部类型
  python3.11 scripts/download_reports_multithread.py --type all --year 2023 --threads 3

输出：
  - data/reports/reports_{year}_{type}.json
  - 每条研报包含：title, author, orgName, date, industry, rating 等
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

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "reports"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# API 配置
API_URL = "https://reportapi.eastmoney.com/report/list"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/report/",
}
PAGE_SIZE = 100
REQUEST_DELAY = 0.5  # 请求间隔（秒）


def fetch_page(
    year: int,
    page_no: int,
    q_type: int = 0,
    max_retries: int = 3,
) -> dict[str, Any]:
    """
    抓取单页研报

    Args:
        year: 年份
        page_no: 页码（从 1 开始）
        q_type: 0=宏观/策略，1=行业，2=个股
        max_retries: 最大重试次数

    Returns:
        {page_no, reports, total_pages, error}
    """
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
                print(f"    页码 {page_no}: 返回格式异常")
                return {
                    "page_no": page_no,
                    "reports": [],
                    "total_pages": 0,
                    "error": "No data key",
                }

            # data 可能是 dict 或 list
            if isinstance(data["data"], list):
                reports = data["data"]
                total_pages = data.get("TotalPage", 0)
            elif isinstance(data["data"], dict) and "list" in data["data"]:
                reports = data["data"]["list"]
                total_pages = data.get("TotalPage", 0)
            else:
                reports = []
                total_pages = 0

            return {
                "page_no": page_no,
                "reports": reports,
                "total_pages": total_pages,
                "error": None,
            }

        except Exception as e:
            print(f"    页码 {page_no} 第 {attempt + 1} 次尝试失败：{e}")
            if attempt < max_retries - 1:
                time.sleep(REQUEST_DELAY * 2)

    return {
        "page_no": page_no,
        "reports": [],
        "total_pages": 0,
        "error": "Max retries exceeded",
    }


def fetch_year_reports(
    year: int,
    q_type: int = 0,
    max_pages: int = 100,
    threads: int = 1,
) -> list[dict]:
    """
    抓取指定年份的研报

    Args:
        year: 年份
        q_type: 0=宏观/策略，1=行业，2=个股
        max_pages: 最大页数
        threads: 线程数

    Returns:
        研报列表
    """
    type_name = ["macro", "industry", "stock"][q_type]
    print(f"\n=== {year} 年 {type_name} 研报 ===")

    # 先抓第一页获取总页数
    first_page = fetch_page(year, 1, q_type)
    if first_page["error"]:
        print(f"  获取总页数失败：{first_page['error']}")
        return []

    total_pages = min(first_page["total_pages"], max_pages)
    print(f"  总页数：{total_pages}")

    all_reports = first_page["reports"]

    if threads == 1:
        # 单线程模式
        for page_no in range(2, total_pages + 1):
            result = fetch_page(year, page_no, q_type)
            if not result["error"]:
                all_reports.extend(result["reports"])
                if page_no % 10 == 0:
                    print(f"  已下载 {len(all_reports)} 条 / {total_pages} 页")
            time.sleep(REQUEST_DELAY)
    else:
        # 多线程模式
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {
                executor.submit(fetch_page, year, page_no, q_type): page_no
                for page_no in range(2, total_pages + 1)
            }

            completed = 0
            for future in as_completed(futures):
                result = future.result()
                if not result["error"]:
                    all_reports.extend(result["reports"])

                completed += 1
                if completed % 10 == 0:
                    print(f"  已下载 {len(all_reports)} 条 / 完成 {completed}/{total_pages} 页")

    print(f"  完成：共 {len(all_reports)} 条")
    return all_reports


def save_reports(reports: list[dict], output_path: Path) -> None:
    """保存研报到 JSON 文件"""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)
    print(f"  保存到：{output_path}")


def main():
    parser = argparse.ArgumentParser(description="多线程下载券商研报")
    parser.add_argument(
        "--type",
        choices=["macro", "industry", "stock", "all"],
        default="stock",
        help="研报类型：macro=宏观/策略，industry=行业，stock=个股，all=全部 (default: stock)",
    )
    parser.add_argument("--year", type=int, default=2023, help="年份 (default: 2023)")
    parser.add_argument(
        "--years",
        type=str,
        default="",
        help="多个年份，逗号分隔，如 2020,2021,2022,2023 (优先于 --year)",
    )
    parser.add_argument(
        "--threads", type=int, default=1, help="线程数 (default: 1, 建议 3-5)"
    )
    parser.add_argument(
        "--max-pages", type=int, default=100, help="每种类型最大页数 (default: 100)"
    )
    parser.add_argument(
        "--delay", type=float, default=0.5, help="请求间隔秒数 (default: 0.5)"
    )
    args = parser.parse_args()

    global REQUEST_DELAY
    REQUEST_DELAY = args.delay

    # 解析年份
    if args.years:
        years = [int(y.strip()) for y in args.years.split(",")]
    else:
        years = [args.year]

    # 解析类型
    type_map = {
        "macro": [0],
        "industry": [1],
        "stock": [2],
        "all": [0, 1, 2],
    }

    total_reports = 0
    start_time = time.time()

    for year in years:
        for q_type in type_map[args.type]:
            type_name = ["macro", "industry", "stock"][q_type]

            reports = fetch_year_reports(
                year=year,
                q_type=q_type,
                max_pages=args.max_pages,
                threads=args.threads,
            )

            if reports:
                output_file = DATA_DIR / f"reports_{year}_{type_name}.json"
                save_reports(reports, output_file)
                total_reports += len(reports)

                # 统计
                org_count = len(set(r.get("orgName", "") for r in reports if r.get("orgName")))
                date_list = [r.get("date", "") for r in reports if r.get("date")]
                print(f"  机构数量：{org_count}")
                if date_list:
                    print(f"  日期范围：{min(date_list)} ~ {max(date_list)}")

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"完成！")
    print(f"  总研报数：{total_reports}")
    print(f"  耗时：{elapsed:.1f} 秒")
    print(f"  保存目录：{DATA_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
