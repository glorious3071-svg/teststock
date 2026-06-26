#!/usr/bin/env python3
"""Import Shenwan industry indices (SW2021) daily quotes + valuation.

数据源：
  - Tushare index_classify (doc_id=86)：申万一级/二级行业清单
  - Tushare sw_daily        (doc_id=181)：申万行业日线行情 + PE/PB/MV 一体接口

落表：
  - sw_daily 价格部分     → index_daily         (与宽基共用一张表，ts_code 区分)
  - sw_daily 估值部分     → index_dailybasic    (与宽基共用一张表，pe_ttm 列填 NULL)

字段映射：
  Tushare 字段       DB 列              说明
  --------------     ----------------   -----------------------------
  change             change_pt          MySQL 保留字
  pct_change         pct_chg            与 index_daily 接口的 pct_chg 命名对齐
  vol (万股)         NULL               与 index_daily 的「手」单位不一致，暂不入
  amount (万元)      NULL               与 index_daily 的「千元」单位不一致，暂不入
  pe / pb            pe / pb            ⚠ sw_daily 的 pe 是动态市盈率，非 TTM
  pe_ttm             NULL               sw_daily 无此字段，留空
  total_mv           total_mv (元)      需 *10000 单位换算（万元→元）
  float_mv           float_mv (元)      同上

幂等：CREATE TABLE IF NOT EXISTS + ON DUPLICATE KEY UPDATE，重跑安全。
限速：4 年/chunk + 1.2s sleep，~150 个指数 × 7 chunk ≈ 1050 次调用，预计 25 分钟。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

DATA_DIR = ROOT / "data"
INDEX_DAILY_SCHEMA = ROOT / "sql" / "index_daily_schema.sql"
INDEX_DAILYBASIC_SCHEMA = ROOT / "sql" / "index_valuation_schema.sql"
CLASSIFY_CSV = DATA_DIR / "sw_industry_classify.csv"

REQUEST_SLEEP = 1.2
START_DATE = "20000101"
CHUNK_YEARS = 4
SW_SOURCE = "SW2021"
SW_LEVELS = ["L1", "L2"]

# sw_daily 返回字段（Tushare doc_id=181）
SW_DAILY_FIELDS = [
    "ts_code", "trade_date", "name",
    "open", "low", "high", "close", "change", "pct_change",
    "vol", "amount", "pe", "pb", "float_mv", "total_mv",
]

# index_daily 落表列（与 schema 对齐；vol/amount 单位差异，sw 不入这两列）
INDEX_DAILY_COLS = [
    "open", "high", "low", "close", "pre_close",
    "change_pt", "pct_chg", "vol", "amount",
]

# index_dailybasic 落表列（与 schema 对齐；pe_ttm/换手率等 sw_daily 不提供）
INDEX_DAILYBASIC_COLS = [
    "total_mv", "float_mv", "total_share", "float_share", "free_share",
    "turnover_rate", "turnover_rate_f", "pe", "pe_ttm", "pb",
]

WAN_TO_YUAN = 10_000.0  # 万元 → 元


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def nullify(value):
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "nat", "none"):
        return None
    return value


def to_float(value) -> float | None:
    v = nullify(value)
    return None if v is None else float(v)


def parse_trade_date(value) -> str | None:
    v = nullify(value)
    if v is None:
        return None
    text = str(int(v)) if isinstance(v, (int, float)) else str(v).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return pd.to_datetime(text).strftime("%Y-%m-%d")


def apply_schemas(conn) -> None:
    """两张目标表都用 IF NOT EXISTS 创建，已存在则不动。"""
    for path in (INDEX_DAILY_SCHEMA, INDEX_DAILYBASIC_SCHEMA):
        sql = path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
        conn.commit()


def fetch_classify(client, level: str) -> pd.DataFrame:
    data = client.query_http(
        "index_classify",
        {"src": SW_SOURCE, "level": level},
        timeout=120,
    )
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame()
    df = pd.DataFrame(items, columns=fields)
    df["level"] = level
    return df


def fetch_sw_daily_range(client, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    data = client.query_http(
        "sw_daily",
        {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        timeout=120,
    )
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame(columns=SW_DAILY_FIELDS)
    return pd.DataFrame(items, columns=fields)


def fetch_sw_daily_all(client, ts_code: str) -> pd.DataFrame:
    end_date = date.today().strftime("%Y%m%d")
    chunk_start = START_DATE
    frames: list[pd.DataFrame] = []
    while chunk_start <= end_date:
        chunk_end_year = int(chunk_start[:4]) + CHUNK_YEARS - 1
        chunk_end = min(f"{chunk_end_year}1231", end_date)
        df = fetch_sw_daily_range(client, ts_code, chunk_start, chunk_end)
        time.sleep(REQUEST_SLEEP)
        if not df.empty:
            frames.append(df)
        if chunk_end >= end_date:
            break
        chunk_start = f"{int(chunk_end[:4]) + 1}0101"

    if not frames:
        return pd.DataFrame(columns=SW_DAILY_FIELDS)
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["ts_code", "trade_date"]
    )


def split_for_db(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """sw_daily 一体接口的字段拆成 index_daily 价格 + index_dailybasic 估值。"""
    if df.empty:
        empty_price = pd.DataFrame(columns=["ts_code", "trade_date", *INDEX_DAILY_COLS])
        empty_basic = pd.DataFrame(columns=["ts_code", "trade_date", *INDEX_DAILYBASIC_COLS])
        return empty_price, empty_basic

    src = df.copy()
    src["trade_date"] = src["trade_date"].map(parse_trade_date)
    src = src.dropna(subset=["trade_date"])

    price = pd.DataFrame({
        "ts_code": src["ts_code"],
        "trade_date": src["trade_date"],
        "open": src["open"],
        "high": src["high"],
        "low": src["low"],
        "close": src["close"],
        "pre_close": None,           # sw_daily 无 pre_close 字段
        "change_pt": src["change"],
        "pct_chg": src["pct_change"],
        "vol": None,                 # 单位「万股」与 index_daily「手」不一致，留 NULL
        "amount": None,              # 单位「万元」与 index_daily「千元」不一致，留 NULL
    })

    basic = pd.DataFrame({
        "ts_code": src["ts_code"],
        "trade_date": src["trade_date"],
        "total_mv": src["total_mv"].map(lambda v: to_float(v) and to_float(v) * WAN_TO_YUAN),
        "float_mv": src["float_mv"].map(lambda v: to_float(v) and to_float(v) * WAN_TO_YUAN),
        "total_share": None,
        "float_share": None,
        "free_share": None,
        "turnover_rate": None,
        "turnover_rate_f": None,
        "pe": src["pe"],
        "pe_ttm": None,              # sw_daily 是动态 PE，无 TTM
        "pb": src["pb"],
    })
    return price, basic


def upsert_index_daily(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO index_daily
            (ts_code, trade_date, {", ".join(INDEX_DAILY_COLS)})
        VALUES (%s, %s, {", ".join(["%s"] * len(INDEX_DAILY_COLS))})
        ON DUPLICATE KEY UPDATE
            {", ".join(f"{c}=VALUES({c})" for c in INDEX_DAILY_COLS)}
    """
    rows = [
        (
            r.ts_code,
            r.trade_date,
            *[to_float(getattr(r, c)) for c in INDEX_DAILY_COLS],
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def upsert_index_dailybasic(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO index_dailybasic
            (ts_code, trade_date, {", ".join(INDEX_DAILYBASIC_COLS)})
        VALUES (%s, %s, {", ".join(["%s"] * len(INDEX_DAILYBASIC_COLS))})
        ON DUPLICATE KEY UPDATE
            {", ".join(f"{c}=VALUES({c})" for c in INDEX_DAILYBASIC_COLS)}
    """
    rows = [
        (
            r.ts_code,
            r.trade_date,
            *[to_float(getattr(r, c)) for c in INDEX_DAILYBASIC_COLS],
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    client = create_client()
    DATA_DIR.mkdir(exist_ok=True)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schemas (idempotent)...")
        apply_schemas(conn)

        print(f"\nFetching {SW_SOURCE} classify (L1 + L2)...")
        classify_frames = []
        for level in SW_LEVELS:
            df = fetch_classify(client, level)
            print(f"  {level}: {len(df)} 个指数")
            classify_frames.append(df)
            time.sleep(REQUEST_SLEEP)
        classify = pd.concat(classify_frames, ignore_index=True)
        classify.to_csv(CLASSIFY_CSV, index=False)
        print(f"  saved to {CLASSIFY_CSV}")

        index_codes = classify["index_code"].dropna().unique().tolist()
        print(f"\n开始拉取 {len(index_codes)} 个申万指数行情 + 估值 ...")
        total_price = 0
        total_basic = 0
        for idx, ts_code in enumerate(index_codes, 1):
            name = classify.loc[
                classify["index_code"] == ts_code, "industry_name"
            ].iloc[0]
            print(f"[{idx}/{len(index_codes)}] {ts_code} {name}")
            raw = fetch_sw_daily_all(client, ts_code)
            if raw.empty:
                print("  rows: 0 (skip)")
                continue
            price, basic = split_for_db(raw)
            print(
                f"  rows: {len(price)} "
                f"({price['trade_date'].min()} .. {price['trade_date'].max()})"
            )
            n_price = upsert_index_daily(conn, price)
            n_basic = upsert_index_dailybasic(conn, basic)
            total_price += n_price
            total_basic += n_basic

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT ts_code), COUNT(*), MIN(trade_date), MAX(trade_date)
                FROM index_daily WHERE ts_code LIKE %s
                """,
                ("%.SI",),
            )
            n_codes, n_rows, first_d, last_d = cur.fetchone()
            print(f"\nindex_daily 中 .SI 指数：{n_codes} 个 / {n_rows} 行 / {first_d} ~ {last_d}")

            cur.execute(
                """
                SELECT COUNT(DISTINCT ts_code), COUNT(*), MIN(trade_date), MAX(trade_date)
                FROM index_dailybasic WHERE ts_code LIKE %s
                """,
                ("%.SI",),
            )
            n_codes, n_rows, first_d, last_d = cur.fetchone()
            print(f"index_dailybasic 中 .SI 指数：{n_codes} 个 / {n_rows} 行 / {first_d} ~ {last_d}")

        print(f"\nTotal upserted — index_daily: {total_price}, index_dailybasic: {total_basic}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
