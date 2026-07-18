#!/usr/bin/env python3
"""Audit whether China ETF option history is usable for executable backtests."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from calendar import monthrange
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql

from db.connection import get_connection, mysql_config
from scripts.import_cn_etf_option_snapshot import DAILY_COLS, apply_schema, parse_date, upsert_daily
from tushare_client import create_client

OUT_DIR = ROOT / "data" / "portfolio"
DEFAULT_EXCHANGES = ["SSE", "SZSE"]


def month_iter(start: str, end: str, step_months: int) -> list[date]:
    start_year, start_month = [int(part) for part in start.split("-")]
    end_year, end_month = [int(part) for part in end.split("-")]
    current = start_year * 12 + start_month - 1
    stop = end_year * 12 + end_month - 1
    out = []
    while current <= stop:
        year = current // 12
        month = current % 12 + 1
        out.append(date(year, month, monthrange(year, month)[1]))
        current += step_months
    return out


def resolve_trade_date(conn, month_end: date) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(trade_date)
            FROM index_daily
            WHERE ts_code='000300.SH' AND trade_date <= %s
            """,
            (month_end,),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def fetch_opt_daily(client, trade_date: date, exchange: str) -> pd.DataFrame:
    raw = client.query_http(
        "opt_daily",
        {"trade_date": trade_date.strftime("%Y%m%d"), "exchange": exchange},
        timeout=120,
    )
    data = raw.get("data") or {}
    items = data.get("items") or []
    fields = data.get("fields") or []
    if not items:
        return pd.DataFrame(columns=DAILY_COLS)
    df = pd.DataFrame(items, columns=fields)
    df["trade_date"] = df["trade_date"].map(parse_date)
    return df[DAILY_COLS].drop_duplicates(["ts_code", "trade_date"])


def basic_match_count(conn, ts_codes: list[str]) -> int:
    if not ts_codes:
        return 0
    count = 0
    with conn.cursor() as cur:
        for start in range(0, len(ts_codes), 500):
            chunk = ts_codes[start : start + 500]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT COUNT(*) FROM cn_option_basic WHERE ts_code IN ({placeholders})",
                chunk,
            )
            count += int(cur.fetchone()[0] or 0)
    return count


def archive_match_count(conn, ts_codes: list[str]) -> int:
    if not ts_codes:
        return 0
    count = 0
    with conn.cursor() as cur:
        for start in range(0, len(ts_codes), 500):
            chunk = ts_codes[start : start + 500]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT COUNT(*) FROM cn_option_contract_archive WHERE option_ts_code IN ({placeholders})",
                chunk,
            )
            count += int(cur.fetchone()[0] or 0)
    return count


def audit_sample(client, conn, trade_date: date, exchanges: list[str], write_cache: bool) -> dict[str, Any]:
    frames = []
    exchange_counts: dict[str, int] = {}
    for exchange in exchanges:
        df = fetch_opt_daily(client, trade_date, exchange)
        frames.append(df)
        exchange_counts[exchange] = len(df)
    non_empty = [frame.dropna(axis=1, how="all") for frame in frames if not frame.empty]
    daily = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame(columns=DAILY_COLS)
    daily = daily.drop_duplicates(["ts_code", "trade_date"]) if not daily.empty else daily
    if write_cache and not daily.empty:
        upsert_daily(conn, daily)
    ts_codes = sorted(daily["ts_code"].dropna().unique().tolist()) if not daily.empty else []
    matched = basic_match_count(conn, ts_codes)
    archive_matched = archive_match_count(conn, ts_codes)
    total = len(ts_codes)
    best_matched = max(matched, archive_matched)
    return {
        "trade_date": trade_date.isoformat(),
        "exchange_counts": exchange_counts,
        "option_daily_rows": int(len(daily)),
        "distinct_contracts": int(total),
        "basic_matched_contracts": int(matched),
        "basic_match_rate": matched / total if total else 0.0,
        "archive_matched_contracts": int(archive_matched),
        "archive_match_rate": archive_matched / total if total else 0.0,
        "best_matched_contracts": int(best_matched),
        "best_match_rate": best_matched / total if total else 0.0,
        "status": (
            "no_option_daily"
            if total == 0
            else "contract_terms_available"
            if best_matched == total
            else "partial_contract_terms"
            if best_matched > 0
            else "daily_prices_without_contract_terms"
        ),
        "contract_terms_source": (
            "none"
            if best_matched == 0
            else "cn_option_contract_archive"
            if archive_matched >= matched
            else "cn_option_basic"
        ),
    }


def missing_trade_date_row(month_end: date) -> dict[str, Any]:
    return {
        "month_end": month_end.isoformat(),
        "trade_date": None,
        "option_daily_rows": 0,
        "distinct_contracts": 0,
        "basic_matched_contracts": 0,
        "basic_match_rate": 0.0,
        "archive_matched_contracts": 0,
        "archive_match_rate": 0.0,
        "best_matched_contracts": 0,
        "best_match_rate": 0.0,
        "status": "missing_trade_date",
        "contract_terms_source": "none",
    }


def modeling_implication(contract_terms_gap: bool, archive_terms_gap: bool) -> str:
    if not contract_terms_gap:
        return "Sampled opt_daily history has matched contract terms for every sampled date."
    if not archive_terms_gap:
        return (
            "Current opt_basic misses expired contracts, but cn_option_contract_archive reconstructs "
            "the sampled historical contract terms from SSE event records."
        )
    return (
        "opt_daily history is available, but sampled expired-contract terms are still incomplete; "
        "historical executable option backtests need broader event backfill or another contract-master source."
    )


def write_outputs(report: dict[str, Any], rows: list[dict[str, Any]], output_prefix: Path) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    csv_path = output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return json_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit historical CN ETF option daily/basic coverage.")
    parser.add_argument("--start", default="2015-02", help="YYYY-MM start month")
    parser.add_argument("--end", default="2026-07", help="YYYY-MM end month")
    parser.add_argument("--step-months", type=int, default=6)
    parser.add_argument("--exchanges", nargs="+", default=DEFAULT_EXCHANGES)
    parser.add_argument("--write-cache", action="store_true", help="Persist sampled opt_daily rows into cn_option_daily.")
    parser.add_argument("--output-prefix")
    args = parser.parse_args()

    client = create_client()
    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        samples: list[dict[str, Any]] = []
        for month_end in month_iter(args.start, args.end, args.step_months):
            trade_date = resolve_trade_date(conn, month_end)
            if not trade_date:
                samples.append(missing_trade_date_row(month_end))
                continue
            item = audit_sample(client, conn, trade_date, args.exchanges, args.write_cache)
            samples.append({"month_end": month_end.isoformat(), **item})
            print(
                f"{month_end} trade={trade_date} daily={item['option_daily_rows']} "
                f"contracts={item['distinct_contracts']} basic={item['basic_matched_contracts']} "
                f"archive={item['archive_matched_contracts']} "
                f"status={item['status']}"
            )
    finally:
        conn.close()

    with_daily = [row for row in samples if row["option_daily_rows"] > 0]
    with_basic_terms = [row for row in samples if row["basic_matched_contracts"] > 0]
    with_archive_terms = [row for row in samples if row["archive_matched_contracts"] > 0]
    with_any_terms = [row for row in samples if row["best_matched_contracts"] > 0]
    fully_matched = [
        row
        for row in with_daily
        if row["best_matched_contracts"] >= row["distinct_contracts"] and row["distinct_contracts"] > 0
    ]
    contract_terms_gap = len(with_daily) > 0 and len(fully_matched) < len(with_daily)
    archive_terms_gap = (
        len(with_daily) > 0
        and any(row["archive_matched_contracts"] < row["distinct_contracts"] for row in with_daily)
    )
    report = {
        "strategy": "cn_option_history_coverage_audit",
        "start": args.start,
        "end": args.end,
        "step_months": args.step_months,
        "exchanges": args.exchanges,
        "write_cache": args.write_cache,
        "sample_count": len(samples),
        "samples_with_option_daily": len(with_daily),
        "samples_with_basic_contract_terms": len(with_basic_terms),
        "samples_with_archive_contract_terms": len(with_archive_terms),
        "samples_with_any_contract_terms": len(with_any_terms),
        "samples_fully_matched_contract_terms": len(fully_matched),
        "historical_daily_available": len(with_daily) > 0,
        "historical_contract_terms_available": not contract_terms_gap and len(with_daily) > 0,
        "contract_terms_gap": contract_terms_gap,
        "archive_terms_gap": archive_terms_gap,
        "modeling_implication": modeling_implication(contract_terms_gap, archive_terms_gap),
        "samples": samples,
    }
    output_prefix = (
        Path(args.output_prefix)
        if args.output_prefix
        else OUT_DIR / f"cn_option_history_coverage_audit_{date.today().strftime('%Y%m%d')}"
    )
    if not output_prefix.is_absolute():
        output_prefix = ROOT / output_prefix
    json_path, csv_path = write_outputs(report, samples, output_prefix)
    print(
        f"coverage: samples={len(samples)} with_daily={len(with_daily)} "
        f"with_basic_terms={len(with_basic_terms)} with_archive_terms={len(with_archive_terms)} "
        f"fully_matched={len(fully_matched)} gap={report['contract_terms_gap']}"
    )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
