#!/usr/bin/env python3.11
"""
券商研报下载器

数据源：东方财富研报中心 API
URL: https://reportapi.eastmoney.com/report/list

支持类型：
  - qType=0: 宏观/策略研报
  - qType=1: 行业研报
  - qType=2: 个股研报

输出：
  - data/reports/ 目录
  - metadata.json: 研报元数据（标题/日期/机构/评级/摘要）
  - 可选：完整研报内容（需额外下载）

用法：
  python3.11 scripts/download_research_reports.py --type macro --year 2023
  python3.11 scripts/download_research_reports.py --type industry --year 2024
  python3.11 scripts/download_research_reports.py --type all --year 2023
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "reports"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def fetch_reports(
    year: int,
    q_type: int = 0,
    page_size: int = 100,
    max_pages: int = 100,
) -> list[dict]:
    """
    抓取指定年份的研报元数据

    Args:
        year: 年份
        q_type: 0=宏观/策略，1=行业，2=个股
        page_size: 每页数量
        max_pages: 最大页数

    Returns:
        研报列表
    """
    url = "https://reportapi.eastmoney.com/report/list"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/report/",
    }

    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"

    all_reports = []
    page_no = 1

    while page_no <= max_pages:
        params = {
            "industryCode": "*",
            "pageSize": page_size,
            "beginTime": start_date,
            "endTime": end_date,
            "pageNo": page_no,
            "fields": "title,author,orgName,emRating,date,summary,content",
            "qType": q_type,
        }

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                print(f"  页码 {page_no}: 状态码 {resp.status_code}")
                break

            data = resp.json()
            if "data" not in data or "list" not in data["data"]:
                break

            reports = data["data"]["list"]
            if not reports:
                break

            all_reports.extend(reports)

            total_pages = data.get("TotalPage", 0)
            total_hits = data.get("hits", 0)

            if page_no == 1:
                print(f"  总页数: {total_pages}, 总条数: {total_hits}")

            if page_no >= total_pages:
                break

            page_no += 1

            # 避免请求过快
            import time
            time.sleep(0.5)

        except Exception as e:
            print(f"  页码 {page_no} 错误: {e}")
            break

    return all_reports


def save_reports(reports: list[dict], output_path: Path) -> None:
    """保存研报到 JSON 文件"""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)
    print(f"  保存 {len(reports)} 条研报到: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="下载券商研报")
    parser.add_argument("--type", choices=["macro", "industry", "stock", "all"],
                        default="macro",
                        help="研报类型: macro=宏观/策略，industry=行业，stock=个股，all=全部")
    parser.add_argument("--year", type=int, default=2023,
                        help="年份 (default: 2023)")
    parser.add_argument("--max-pages", type=int, default=100,
                        help="每种类型最大页数 (default: 100)")
    args = parser.parse_args()

    print(f"下载 {args.year} 年研报，类型: {args.type}")

    type_map = {
        "macro": [0],
        "industry": [1],
        "stock": [2],
        "all": [0, 1, 2],
    }

    for q_type in type_map[args.type]:
        type_name = ["macro", "industry", "stock"][q_type]
        print(f"\n=== {type_name} 研报 (qType={q_type}) ===")

        reports = fetch_reports(
            year=args.year,
            q_type=q_type,
            max_pages=args.max_pages,
        )

        if reports:
            output_file = DATA_DIR / f"reports_{args.year}_{type_name}.json"
            save_reports(reports, output_file)

            # 统计
            org_count = len(set(r.get("orgName", "") for r in reports if r.get("orgName")))
            print(f"  机构数量: {org_count}")
            print(f"  最早日期: {min(r.get('date', '') for r in reports if r.get('date'))}")
            print(f"  最晚日期: {max(r.get('date', '') for r in reports if r.get('date'))}")


if __name__ == "__main__":
    main()
