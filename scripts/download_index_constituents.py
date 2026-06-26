#!/usr/bin/env python3
"""
下载 CSI 主题指数（930/931）和申万行业指数（801）的成份股 + 权重。

用法：
    python3 scripts/download_index_constituents.py                 # 全量
    python3 scripts/download_index_constituents.py --index 931233  # 单个指数
    python3 scripts/download_index_constituents.py --type csi      # 只跑 CSI
    python3 scripts/download_index_constituents.py --type si       # 只跑申万

数据来源：
    CSI: akshare index_stock_cons_weight_csindex（中证指数官网 XLS）
    SI:  akshare index_component_sw（申万宏源官网 API）

写入表：index_constituent（自动建表）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import akshare as ak
import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

SLEEP_AKSHARE = 1.0  # akshare 限速（秒）

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


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS index_constituent (
    index_code   VARCHAR(20)    NOT NULL COMMENT '指数代码',
    trade_date   DATE           NOT NULL COMMENT '权重生效日期',
    con_code     VARCHAR(20)    NOT NULL COMMENT '成份股代码（Tushare 格式）',
    weight       DECIMAL(10,4)  NULL     COMMENT '权重 %',
    con_name     VARCHAR(100)   NULL     COMMENT '成份股名称',
    created_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_code, trade_date, con_code),
    KEY idx_con_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def ensure_schema(conn: pymysql.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


# ── 股票代码转换 ────────────────────────────────────────────────────────────────

def normalize_csi_code(raw: str) -> str | None:
    """CSI 成份股代码 → Tushare 格式（600xxx.SH / 000xxx.SZ）
    港股代码（如 00700.HK）无法用 Tushare daily_basic，返回 None。
    """
    raw = str(raw).strip()
    # 已有后缀
    if re.fullmatch(r"\d{6}\.[A-Z]{2}", raw):
        return raw
    # 港股（纯数字 + HK 交易所标注）
    if raw.endswith(".HK") or (len(raw) == 5 and raw.isdigit()):
        return None
    # A 股纯数字
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith(("6", "5")):
            return f"{raw}.SH"
        return f"{raw}.SZ"
    return None


def normalize_sw_code(raw: str) -> str | None:
    """申万成份股代码 → Tushare 格式"""
    raw = str(raw).strip()
    if re.fullmatch(r"\d{6}\.[A-Z]{2}", raw):
        return raw
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith(("6", "5")):
            return f"{raw}.SH"
        return f"{raw}.SZ"
    return None


# ── 指数代码列表 ────────────────────────────────────────────────────────────────

def get_index_codes(conn: pymysql.Connection, index_type: str) -> list[str]:
    """从 index_daily 获取需要处理的指数代码列表"""
    conditions = {
        "csi": "ts_code LIKE '930%%' OR ts_code LIKE '931%%'",
        "si":  "ts_code LIKE '801%%'",
        "all": "ts_code LIKE '930%%' OR ts_code LIKE '931%%' OR ts_code LIKE '801%%'",
    }
    where = conditions.get(index_type, conditions["all"])
    with conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT ts_code FROM index_daily WHERE {where} ORDER BY ts_code")
        return [r[0] for r in cur.fetchall()]


# ── CSI 成份股下载 ──────────────────────────────────────────────────────────────

def download_csi(index_code: str) -> pd.DataFrame:
    """下载单个 CSI 指数成份股，返回标准化 DataFrame"""
    symbol = index_code.split(".")[0]  # 931233.CSI → 931233
    df = ak.index_stock_cons_weight_csindex(symbol=symbol)
    if df.empty:
        return df

    records = []
    for _, row in df.iterrows():
        con_code = normalize_csi_code(str(row.get("成分券代码", "")))
        if con_code is None:
            continue  # 港股跳过
        records.append({
            "index_code": index_code,
            "trade_date": row["日期"],
            "con_code":   con_code,
            "weight":     float(row["权重"]),
            "con_name":   str(row.get("成分券名称", "")),
        })
    return pd.DataFrame(records)


# ── 申万成份股下载 ──────────────────────────────────────────────────────────────

def download_sw(index_code: str) -> pd.DataFrame:
    """下载单个申万行业指数成份股，返回标准化 DataFrame"""
    symbol = index_code.split(".")[0]  # 801010.SI → 801010
    df = ak.index_component_sw(symbol=symbol)
    if df.empty:
        return df

    weight_date = date.today()  # 申万 API 返回的是"最新"权重，用今天日期
    records = []
    for _, row in df.iterrows():
        con_code = normalize_sw_code(str(row.get("证券代码", "")))
        if con_code is None:
            continue
        records.append({
            "index_code": index_code,
            "trade_date": weight_date,
            "con_code":   con_code,
            "weight":     float(row["最新权重"]),
            "con_name":   str(row.get("证券名称", "")),
        })
    return pd.DataFrame(records)


# ── 批量写入 ────────────────────────────────────────────────────────────────────

def upsert_constituents(conn: pymysql.Connection, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = """
        INSERT INTO index_constituent (index_code, trade_date, con_code, weight, con_name)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            weight = VALUES(weight),
            con_name = VALUES(con_name)
    """
    rows = [
        (r["index_code"], r["trade_date"], r["con_code"], r["weight"], r["con_name"])
        for _, r in df.iterrows()
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


# ── 主函数 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="下载指数成份股+权重")
    parser.add_argument("--index", default=None, help="单个指数代码，如 931233.CSI")
    parser.add_argument("--type", default="all", choices=["csi", "si", "all"],
                        help="指数类型：csi / si / all（默认 all）")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        ensure_schema(conn)

        if args.index:
            codes = [args.index]
        else:
            codes = get_index_codes(conn, args.type)

        print(f"共 {len(codes)} 个指数需要下载成份股")
        total_stocks = 0
        errors = []

        for i, code in enumerate(codes, 1):
            try:
                if code.endswith(".CSI"):
                    df = download_csi(code)
                elif code.endswith(".SI"):
                    df = download_sw(code)
                else:
                    print(f"[{i:3d}/{len(codes)}] {code}: 未知后缀，跳过")
                    continue

                n = upsert_constituents(conn, df)
                total_stocks += n
                print(f"[{i:3d}/{len(codes)}] {code}: {n} 只成份股")
                time.sleep(SLEEP_AKSHARE)
            except Exception as e:
                print(f"[{i:3d}/{len(codes)}] {code}: 错误 - {e}")
                errors.append((code, str(e)))

        print(f"\n完成：共写入 {total_stocks} 条成份股记录，失败 {len(errors)} 个")
        if errors:
            print("失败列表：")
            for code, err in errors:
                print(f"  {code}: {err}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
