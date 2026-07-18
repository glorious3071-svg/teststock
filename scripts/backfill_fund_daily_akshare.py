#!/usr/bin/env python3
"""Backfill explicit domestic ETF price gaps from East Money via AkShare.

This is a narrow fallback for periods that Tushare ``fund_daily`` does not
return. Existing rows are never overwritten, so the repository's primary
Tushare history remains authoritative wherever it is available.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import akshare as ak

from db.connection import get_connection


def compact_date(value: str) -> str:
    return date.fromisoformat(value).strftime("%Y%m%d") if "-" in value else value


def fetch_rows(code: str, start_date: str, end_date: str) -> list[tuple]:
    symbol = code.split(".", 1)[0]
    last_error: Exception | None = None
    frame = None
    for attempt in range(1, 6):
        try:
            frame = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date=compact_date(start_date),
                end_date=compact_date(end_date),
                adjust="",
            )
            break
        except Exception as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(attempt * 2)
    source = "eastmoney"
    if frame is None:
        market = "sh" if code.endswith(".SH") else "sz"
        try:
            sina = ak.fund_etf_hist_sina(symbol=f"{market}{symbol}")
            start = date.fromisoformat(start_date) if "-" in start_date else date.fromisoformat(
                f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
            )
            end = date.fromisoformat(end_date) if "-" in end_date else date.fromisoformat(
                f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            )
            sina = sina[(sina["date"] >= start) & (sina["date"] <= end)]
            frame = sina.rename(
                columns={
                    "date": "日期",
                    "open": "开盘",
                    "close": "收盘",
                    "high": "最高",
                    "low": "最低",
                    "volume": "成交量",
                    "amount": "成交额",
                }
            )
            source = "sina"
        except Exception:
            raise last_error or RuntimeError("AkShare ETF history request failed")
    if frame.empty:
        return []
    rows = []
    previous_close: float | None = None
    for item in frame.to_dict("records"):
        close = float(item["收盘"])
        supplied_pre_close = item.get("prevclose")
        pre_close = previous_close or (
            float(supplied_pre_close)
            if supplied_pre_close is not None and str(supplied_pre_close) != "nan"
            else None
        )
        change = close - pre_close if pre_close is not None else float(item.get("涨跌额") or 0.0)
        pct_chg = (
            (close / pre_close - 1.0) * 100.0
            if pre_close not in (None, 0.0)
            else float(item.get("涨跌幅") or 0.0)
        )
        rows.append(
            (
                code,
                str(item["日期"]),
                float(item["开盘"]),
                float(item["最高"]),
                float(item["最低"]),
                close,
                pre_close,
                change,
                pct_chg,
                float(item.get("成交量") or 0.0) / (100.0 if source == "sina" else 1.0),
                float(item.get("成交额") or 0.0) / 1000.0,
            )
        )
        previous_close = close
    return rows


def insert_missing(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT IGNORE INTO fund_daily
            (ts_code, trade_date, open, high, low, close, pre_close,
             change_pt, pct_chg, vol, amount)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        inserted = cur.executemany(sql, rows)
    conn.commit()
    return int(inserted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ts-code", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    args = parser.parse_args()

    if not args.ts_code.endswith((".SH", ".SZ")):
        raise ValueError("only domestic SH/SZ ETF codes are supported")
    rows = fetch_rows(args.ts_code, args.start_date, args.end_date)
    conn = get_connection()
    try:
        inserted = insert_missing(conn, rows)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM fund_daily WHERE ts_code=%s",
                (args.ts_code,),
            )
            first_date, last_date, count = cur.fetchone()
    finally:
        conn.close()
    print(
        f"source=akshare_eastmoney code={args.ts_code} fetched={len(rows)} "
        f"inserted={inserted} coverage={first_date}..{last_date} rows={count}"
    )
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
