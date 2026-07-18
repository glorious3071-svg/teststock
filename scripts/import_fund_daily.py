#!/usr/bin/env python3
"""Import sector / theme ETF daily quotes from Tushare fund_daily (doc_id=127).

数据源：
  - Tushare fund_daily：场内 ETF 日行情（不复权价 + 含分红再投的 pct_chg）

筛选范围：
  - passive_etf 中 list_status='L' & 非 QDII & 上市 >180 天
  - index_name REGEXP 主流行业/题材关键词（医药/银行/新能源/半导体/AI 等）

落表：fund_daily（schema 见 sql/fund_daily_schema.sql）

幂等策略：
  - 已有 ETF：按「DB 已有最大 trade_date + 1」做 start_date 续传，跳过历史
  - 新 ETF：从 max(list_date, 20100101) 开始全量拉

字段映射：
  Tushare 字段       DB 列          说明
  --------------     ----------     ---------------------
  change             change_pt      MySQL 保留字，列名重命名
  pct_chg            pct_chg        含分红再投，年度累计回报推荐用此字段
  vol (手)           vol            与 fund_daily 接口一致
  amount (千元)      amount         与 fund_daily 接口一致

限速：4 年/chunk + 1.0s sleep；836 只 × ~3 chunk ≈ 2500 次调用，预计 40 分钟。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

from tushare_client import create_client

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "sql" / "fund_daily_schema.sql"

REQUEST_SLEEP = 1.0
GLOBAL_START_DATE = "20100101"  # 2010 前 ETF 仅 2-3 只，无 backtest 价值
CHUNK_YEARS = 4
ETF_MIN_LISTING_DAYS = 180

# 主流行业/题材关键词（覆盖年度框架可能用到的方向）
SECTOR_KEYWORDS_REGEX = (
    "医药|生物|医疗|银行|证券|券商|军工|国防|新能源|光伏|锂电|"
    "动力电池|电池|半导体|芯片|集成电路|消费|食品|白酒|科技|信息|"
    "计算机|软件|人工智能|AI|机器人|有色|金属|煤炭|能源|钢铁|"
    "化工|电力|公用|通信|5G|地产|房地产"
)
OVERSEAS_KEYWORDS_REGEX = "港股|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|东南亚|沙特"

# Tushare fund_daily 返回字段
TUSHARE_FIELDS = [
    "ts_code", "trade_date", "open", "high", "low", "close",
    "pre_close", "change", "pct_chg", "vol", "amount",
]

# fund_daily 落表列顺序（与 schema 对齐）
DB_PRICE_COLS = [
    "open", "high", "low", "close", "pre_close",
    "change_pt", "pct_chg", "vol", "amount",
]


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


def latest_possible_trade_date() -> str:
    today = date.today()
    if today.weekday() == 5:
        today -= timedelta(days=1)
    elif today.weekday() == 6:
        today -= timedelta(days=2)
    return today.strftime("%Y%m%d")


def apply_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def fetch_sector_etf_list(conn) -> list[tuple[str, str, str, date | None]]:
    """从 passive_etf 拉行业/题材 ETF 清单（活的 + 上市≥180d + 非 QDII）。

    排除 .OF（场外申购代码）：fund_daily 接口只对场内代码 .SH/.SZ 返回行情，
    .OF 是同一只 ETF 的场外口，调用返回空，仅消耗 sleep 时间。
    """
    sql = """
        SELECT ts_code, extname, index_name, list_date
        FROM passive_etf
        WHERE list_status = %s
          AND (etf_type IS NULL OR etf_type != %s)
          AND (list_date IS NULL OR list_date <= DATE_SUB(CURDATE(), INTERVAL %s DAY))
          AND ts_code NOT LIKE %s
          AND ts_code NOT LIKE '513%%'
          AND ts_code NOT LIKE '520%%'
          AND COALESCE(extname, '') NOT REGEXP %s
          AND COALESCE(index_name, '') NOT REGEXP %s
          AND COALESCE(index_ts_code, '') NOT LIKE '%%.HI'
          AND COALESCE(index_ts_code, '') NOT LIKE '%%.OTH'
          AND index_name REGEXP %s
        ORDER BY list_date, ts_code
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                "L",
                "QDII",
                ETF_MIN_LISTING_DAYS,
                "%.OF",
                OVERSEAS_KEYWORDS_REGEX,
                OVERSEAS_KEYWORDS_REGEX,
                SECTOR_KEYWORDS_REGEX,
            ),
        )
        return list(cur.fetchall())


def fetch_domestic_passive_etf_list(conn) -> list[tuple[str, str, str, date | None]]:
    """Fetch all live domestic exchange ETF codes that Tushare fund_daily can query."""
    sql = """
        SELECT ts_code, extname, index_name, list_date
        FROM passive_etf
        WHERE list_status = %s
          AND (etf_type IS NULL OR etf_type != %s)
          AND (list_date IS NULL OR list_date <= DATE_SUB(CURDATE(), INTERVAL %s DAY))
          AND ts_code NOT LIKE %s
          AND ts_code NOT LIKE '513%%'
          AND ts_code NOT LIKE '520%%'
          AND COALESCE(extname, '') NOT REGEXP %s
          AND COALESCE(index_name, '') NOT REGEXP %s
          AND COALESCE(index_ts_code, '') NOT LIKE '%%.HI'
          AND COALESCE(index_ts_code, '') NOT LIKE '%%.OTH'
        ORDER BY list_date, ts_code
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                "L",
                "QDII",
                ETF_MIN_LISTING_DAYS,
                "%.OF",
                OVERSEAS_KEYWORDS_REGEX,
                OVERSEAS_KEYWORDS_REGEX,
            ),
        )
        return list(cur.fetchall())


def fetch_existing_last_dates(conn) -> dict[str, date]:
    """读出 fund_daily 表中每只 ETF 已覆盖到的最后日期（续传起点）。"""
    sql = "SELECT ts_code, MAX(trade_date) FROM fund_daily GROUP BY ts_code"
    with conn.cursor() as cur:
        cur.execute(sql)
        return {ts_code: last_d for ts_code, last_d in cur.fetchall() if last_d}


def resolve_start_date(
    ts_code: str,
    list_date: date | None,
    existing_last: dict[str, date],
) -> str:
    """起点：DB 已有 → last+1；否则 max(list_date, GLOBAL_START_DATE)。"""
    if ts_code in existing_last:
        next_d = existing_last[ts_code]
        from datetime import timedelta
        return (next_d + timedelta(days=1)).strftime("%Y%m%d")
    if list_date is None:
        return GLOBAL_START_DATE
    list_str = list_date.strftime("%Y%m%d")
    return max(list_str, GLOBAL_START_DATE)


def fetch_fund_daily_range(client, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    data = None
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            data = client.query_http(
                "fund_daily",
                {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
                timeout=120,
            )
            break
        except Exception as exc:
            last_exc = exc
            print(f"  fetch retry {attempt}/3 failed: {exc}")
            if attempt < 3:
                time.sleep(3 * attempt)
    if data is None:
        raise last_exc or RuntimeError("fund_daily request failed")
    items = data.get("data", {}).get("items") or []
    fields = data.get("data", {}).get("fields") or []
    if not items:
        return pd.DataFrame(columns=TUSHARE_FIELDS)
    return pd.DataFrame(items, columns=fields)


def fetch_fund_daily_all(client, ts_code: str, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    end_date = end_date or latest_possible_trade_date()
    if start_date > end_date:
        return pd.DataFrame(columns=TUSHARE_FIELDS)

    frames: list[pd.DataFrame] = []
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end_year = int(chunk_start[:4]) + CHUNK_YEARS - 1
        chunk_end = min(f"{chunk_end_year}1231", end_date)
        df = fetch_fund_daily_range(client, ts_code, chunk_start, chunk_end)
        time.sleep(REQUEST_SLEEP)
        if not df.empty:
            frames.append(df)
        if chunk_end >= end_date:
            break
        chunk_start = f"{int(chunk_end[:4]) + 1}0101"

    if not frames:
        return pd.DataFrame(columns=TUSHARE_FIELDS)
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["ts_code", "trade_date"]
    )


def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    """Tushare `change` → DB `change_pt`，归一化 trade_date。"""
    if df.empty:
        return df
    out = df.copy()
    out["trade_date"] = out["trade_date"].map(parse_trade_date)
    out = out.dropna(subset=["trade_date"])
    if "change" in out.columns:
        out = out.rename(columns={"change": "change_pt"})
    return out


def upsert_rows(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
        INSERT INTO fund_daily
            (ts_code, trade_date, {", ".join(DB_PRICE_COLS)})
        VALUES (%s, %s, {", ".join(["%s"] * len(DB_PRICE_COLS))})
        ON DUPLICATE KEY UPDATE
            {", ".join(f"{c}=VALUES({c})" for c in DB_PRICE_COLS)}
    """
    rows = [
        (
            r.ts_code,
            r.trade_date,
            *[to_float(getattr(r, c)) for c in DB_PRICE_COLS],
        )
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import ETF daily quotes from Tushare fund_daily.")
    parser.add_argument("--ts-codes", nargs="+", help="Explicit ETF codes to import, e.g. 510300.SH.")
    parser.add_argument("--start-date", help="Override start date as YYYYMMDD for explicit --ts-codes imports.")
    parser.add_argument("--end-date", help="Override end date as YYYYMMDD.")
    parser.add_argument(
        "--all-domestic-passive",
        action="store_true",
        help="Import every live non-QDII exchange passive ETF in passive_etf, not only sector/theme ETFs.",
    )
    parser.add_argument("--limit", type=int, help="Import only the first N selected ETFs for a bounded run.")
    args = parser.parse_args()

    client = create_client()
    DATA_DIR.mkdir(exist_ok=True)

    conn = pymysql.connect(**mysql_config())
    try:
        print("Applying schema (idempotent)...")
        apply_schema(conn)

        if args.ts_codes:
            etf_list = [(code, "", "explicit", None) for code in args.ts_codes]
            print("\n使用显式 ETF 清单...")
            print(f"  共 {len(etf_list)} 只 ETF: {', '.join(args.ts_codes)}")
        elif args.all_domestic_passive:
            print("\n查询全部境内在市被动 ETF 清单...")
            etf_list = fetch_domestic_passive_etf_list(conn)
            print(f"  共 {len(etf_list)} 只 ETF")
        else:
            print("\n查询行业/题材 ETF 清单...")
            etf_list = fetch_sector_etf_list(conn)
            print(f"  共 {len(etf_list)} 只 ETF 命中关键词")
        if args.limit is not None:
            etf_list = etf_list[: args.limit]
            print(f"  limit 后处理 {len(etf_list)} 只 ETF")

        print("\n读取 fund_daily 已有续传起点...")
        existing_last = fetch_existing_last_dates(conn)
        print(f"  已有覆盖：{len(existing_last)} 只 ETF")

        total_upserted = 0
        skipped_no_new = 0
        empty_fetch = 0
        failed_fetch = 0

        for idx, (ts_code, extname, index_name, list_date) in enumerate(etf_list, 1):
            start_date = args.start_date or resolve_start_date(ts_code, list_date, existing_last)
            end_date = args.end_date or latest_possible_trade_date()
            if start_date > end_date:
                skipped_no_new += 1
                continue

            print(f"[{idx}/{len(etf_list)}] {ts_code} {extname} ({index_name}) "
                  f"start={start_date}")
            try:
                raw = fetch_fund_daily_all(client, ts_code, start_date, end_date)
            except Exception as exc:
                failed_fetch += 1
                print(f"  FAIL: {exc}")
                continue
            if raw.empty:
                print("  rows: 0 (skip)")
                empty_fetch += 1
                continue
            prepared = prepare_df(raw)
            if prepared.empty:
                empty_fetch += 1
                continue
            print(
                f"  rows: {len(prepared)} "
                f"({prepared['trade_date'].min()} .. {prepared['trade_date'].max()})"
            )
            n = upsert_rows(conn, prepared)
            total_upserted += n

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT ts_code), COUNT(*),
                       MIN(trade_date), MAX(trade_date)
                FROM fund_daily
                """
            )
            n_codes, n_rows, first_d, last_d = cur.fetchone()
            print(f"\nfund_daily 当前覆盖：{n_codes} 只 ETF / {n_rows} 行 / "
                  f"{first_d} ~ {last_d}")

        print(f"\nTotal upserted: {total_upserted}, "
              f"已是最新跳过: {skipped_no_new}, 空回包: {empty_fetch}, "
              f"失败: {failed_fetch}")
    finally:
        conn.close()
    return 1 if failed_fetch else 0


if __name__ == "__main__":
    raise SystemExit(main())
