#!/usr/bin/env python3
"""
从成份股 PE/PB + 权重，计算各指数的加权 PE_TTM / PB，写入 index_dailybasic。

用法：
    python3 scripts/calc_index_pe_pb.py                   # 全量（所有指数所有日期）
    python3 scripts/calc_index_pe_pb.py --since 20240101  # 只算指定日期之后
    python3 scripts/calc_index_pe_pb.py --index 931233.CSI  # 只算单个指数

前置依赖：
    1. index_constituent 表已有数据（download_index_constituents.py）
    2. stock_daily_basic 表已有成份股数据（download_stock_pe_pb.py）

写入表：index_dailybasic（pe_ttm, pb 字段，ON DUPLICATE KEY UPDATE）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

# ── 数据库 ────────────────────────────────────────────────────────────────────

def mysql_config() -> dict:
    return {
        "host":     os.getenv("MYSQL_HOST",     "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER",     "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


# ── 核心计算 ────────────────────────────────────────────────────────────────────

def get_indices_to_calc(conn: pymysql.Connection, index_code: str | None) -> list[str]:
    """获取需要计算 PE/PB 的指数列表（在 index_constituent 中有数据的）"""
    with conn.cursor() as cur:
        if index_code:
            cur.execute(
                "SELECT DISTINCT index_code FROM index_constituent WHERE index_code = %s",
                (index_code,)
            )
        else:
            cur.execute("SELECT DISTINCT index_code FROM index_constituent ORDER BY index_code")
        return [r[0] for r in cur.fetchall()]


def get_constituents(conn: pymysql.Connection, index_code: str) -> list[tuple[str, float]]:
    """获取指数最新成份股 + 权重：[(con_code, weight), ...]"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT con_code, weight
            FROM index_constituent
            WHERE index_code = %s
              AND trade_date = (SELECT MAX(trade_date) FROM index_constituent WHERE index_code = %s)
        """, (index_code, index_code))
        return [(r[0], float(r[1]) if r[1] else 0.0) for r in cur.fetchall()]


def get_trade_dates(conn: pymysql.Connection, index_code: str, since: str | None) -> list[str]:
    """获取指数在 index_daily 中存在的交易日列表（排除已计算的）"""
    params = [index_code]
    where_extra = ""
    if since:
        where_extra = "AND d.trade_date >= %s"
        params.append(since)

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.trade_date
            FROM index_daily d
            WHERE d.ts_code = %s {where_extra}
            ORDER BY d.trade_date
        """, params)
        return [str(r[0]) for r in cur.fetchall()]


def calc_weighted_pe_pb(
    conn: pymysql.Connection,
    constituents: list[tuple[str, float]],
    trade_dates: list[str],
) -> list[tuple[str, float | None, float | None]]:
    """对每个交易日，计算加权 PE_TTM 和 PB。
    返回：[(trade_date, pe_ttm, pb), ...]
    """
    if not constituents or not trade_dates:
        return []

    con_codes = [c[0] for c in constituents]
    weight_map = {c[0]: c[1] for c in constituents}

    # 一次性查出所有成份股在指定日期的 PE/PB
    placeholders = ",".join(["%s"] * len(con_codes))
    dt_placeholders = ",".join(["%s"] * len(trade_dates))

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ts_code, trade_date, pe_ttm, pb
            FROM stock_daily_basic
            WHERE ts_code IN ({placeholders})
              AND trade_date IN ({dt_placeholders})
              AND (pe_ttm IS NOT NULL OR pb IS NOT NULL)
        """, con_codes + trade_dates)
        rows = cur.fetchall()

    # 按日期分组
    from collections import defaultdict
    by_date: dict[str, list[tuple[str, float | None, float | None]]] = defaultdict(list)
    for ts_code, trade_date, pe_ttm, pb in rows:
        by_date[str(trade_date)].append((
            ts_code,
            float(pe_ttm) if pe_ttm is not None else None,
            float(pb) if pb is not None else None,
        ))

    results = []
    for td in trade_dates:
        stock_data = by_date.get(td, [])
        # 加权 PE_TTM（排除 None 和 <= 0 的值）
        pe_sum = 0.0
        pe_wt  = 0.0
        pb_sum = 0.0
        pb_wt  = 0.0
        for ts_code, pe_ttm, pb in stock_data:
            w = weight_map.get(ts_code, 0.0)
            if pe_ttm is not None and pe_ttm > 0:
                pe_sum += w * pe_ttm
                pe_wt  += w
            if pb is not None and pb > 0:
                pb_sum += w * pb
                pb_wt  += w

        pe_ttm_val = pe_sum / pe_wt if pe_wt > 0 else None
        pb_val     = pb_sum / pb_wt if pb_wt > 0 else None
        results.append((td, pe_ttm_val, pb_val))

    return results


def upsert_index_dailybasic(
    conn: pymysql.Connection,
    index_code: str,
    results: list[tuple[str, float | None, float | None]],
) -> int:
    """将计算结果写入 index_dailybasic（pe_ttm + pb）"""
    if not results:
        return 0
    sql = """
        INSERT INTO index_dailybasic (ts_code, trade_date, pe_ttm, pb)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            pe_ttm = COALESCE(VALUES(pe_ttm), pe_ttm),
            pb     = COALESCE(VALUES(pb), pb)
    """
    rows = [
        (index_code, td,
         round(pe_ttm, 4) if pe_ttm is not None else None,
         round(pb, 4) if pb is not None else None)
        for td, pe_ttm, pb in results
        if pe_ttm is not None or pb is not None
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


# ── 主函数 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="计算指数加权 PE_TTM / PB")
    parser.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD 或 YYYYMMDD")
    parser.add_argument("--index", default=None, help="单个指数代码")
    args = parser.parse_args()

    # 标准化日期格式
    since = args.since
    if since and len(since) == 8 and since.isdigit():
        since = f"{since[:4]}-{since[4:6]}-{since[6:8]}"

    conn = pymysql.connect(**mysql_config())
    try:
        indices = get_indices_to_calc(conn, args.index)
        print(f"共 {len(indices)} 个指数需要计算 PE/PB")

        total_written = 0
        errors = []

        for i, index_code in enumerate(indices, 1):
            try:
                constituents = get_constituents(conn, index_code)
                if not constituents:
                    print(f"[{i:3d}/{len(indices)}] {index_code}: 无成份股数据，跳过")
                    continue

                trade_dates = get_trade_dates(conn, index_code, since)
                if not trade_dates:
                    print(f"[{i:3d}/{len(indices)}] {index_code}: 无需计算（已是最新或无行情数据）")
                    continue

                results = calc_weighted_pe_pb(conn, constituents, trade_dates)
                n = upsert_index_dailybasic(conn, index_code, results)
                total_written += n

                # 统计有 PE/PB 的日期数
                pe_dates = sum(1 for _, pe, _ in results if pe is not None)
                pb_dates = sum(1 for _, _, pb in results if pb is not None)
                print(f"[{i:3d}/{len(indices)}] {index_code}: {n} 行写入，PE {pe_dates} 天 / PB {pb_dates} 天（共 {len(trade_dates)} 天）")
            except Exception as e:
                print(f"[{i:3d}/{len(indices)}] {index_code}: 错误 - {e}")
                errors.append((index_code, str(e)))

        print(f"\n完成：共写入 {total_written} 行，失败 {len(errors)} 个")
        if errors:
            print("失败列表（前 20）：")
            for code, err in errors[:20]:
                print(f"  {code}: {err}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
