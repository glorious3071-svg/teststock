#!/usr/bin/env python3.11
"""
修复 broker_research_report 表结构

1. 删除旧表
2. 重建表（优化字段名）
3. 重新导入数据（从 JSON 文件）
"""

import pymysql
import json
import os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "reports"


def get_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
        charset="utf8mb4",
    )


def drop_and_recreate_table(conn):
    """删除并重建表"""
    cur = conn.cursor()

    # 删除旧表
    print("删除旧表...")
    cur.execute("DROP TABLE IF EXISTS broker_research_report")

    # 重建表
    print("重建表...")
    cur.execute("""
        CREATE TABLE broker_research_report (
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
    print("表重建完成")


def import_json_files(conn):
    """从 JSON 文件导入数据"""
    cur = conn.cursor()

    sql = """
        INSERT INTO broker_research_report
            (report_date, title, author, org_name, industry, stock_name, stock_code, rating, summary, report_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    total = 0
    for json_file in DATA_DIR.glob("reports_*.json"):
        print(f"导入 {json_file.name}...")

        with open(json_file, "r", encoding="utf-8") as f:
            reports = json.load(f)

        rows = []
        for r in reports:
            try:
                report_date = r.get("date", "")
                if report_date:
                    report_date = datetime.strptime(report_date, "%Y-%m-%d").date()
                else:
                    report_date = None

                author = ",".join(r.get("author", [])) if r.get("author") else None

                # 解析评级
                rating = r.get("emRating", "")
                if isinstance(rating, list):
                    rating = ",".join(rating)
                elif rating and "评级" in rating:
                    rating = rating.split("评级")[-1].strip()

                # 推断报告类型
                report_type = "stock"
                if "宏观" in r.get("title", "") or "macro" in str(r.get("industry", "")):
                    report_type = "macro"
                elif "行业" in r.get("title", "") or "industry" in str(r.get("industry", "")):
                    report_type = "industry"

                rows.append((
                    report_date,
                    r.get("title", ""),
                    author[:500] if author else None,
                    r.get("orgName", ""),
                    r.get("industry", ""),
                    r.get("stockName", ""),
                    r.get("stockCode", ""),
                    rating[:50] if rating else None,
                    r.get("summary", ""),
                    report_type,
                ))
            except Exception as e:
                continue

        if rows:
            cur.executemany(sql, rows)
            conn.commit()
            total += len(rows)
            print(f"  入库 {len(rows)} 条")

    print(f"\n总入库数：{total}")


def main():
    conn = get_conn()

    try:
        drop_and_recreate_table(conn)
        import_json_files(conn)
        print("\n完成！")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
