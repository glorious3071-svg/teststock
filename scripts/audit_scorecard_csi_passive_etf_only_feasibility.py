#!/usr/bin/env python3
"""Audit feasibility boundaries for domestic passive ETF-only scorecard goals."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from bisect import bisect_right
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import monthly_boundaries, shifted_boundary
from scripts.backtest_scorecard_csi_midyear_risk import INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD
from scripts.search_scorecard_csi_passive_etf_only import (
    END_YEAR,
    EXECUTION_LAGS,
    MONEY_ETF_WHITELIST,
    MONTH_PHASES,
    START_YEAR,
    classify_etf,
)

OUT_DIR = ROOT / "data" / "backtests"


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date) -> float | None:
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def series_drawdown(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date) -> float | None:
    values = [px for day, px in rows if start <= day <= end]
    if len(values) < 2:
        return None
    return max_drawdown(values)


def load_universe(include_money_etfs: bool) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[dt.date, float]]]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
                FROM passive_etf e
                WHERE e.list_status='L'
                  AND (e.etf_type IS NULL OR e.etf_type!='QDII')
                  AND e.ts_code NOT LIKE '%%.OF'
                ORDER BY e.list_date, e.ts_code
                """
            )
            meta_rows = cur.fetchall()
            metas = {
                str(code): {
                    "code": str(code),
                    "name": str(name or code),
                    "index_code": str(index_code or ""),
                    "index_name": str(index_name or ""),
                    "list_date": list_date.isoformat() if list_date else None,
                    "category": classify_etf(str(code), str(name or ""), str(index_name or "")),
                }
                for code, name, index_code, index_name, list_date in meta_rows
            }
            if include_money_etfs:
                for code, (name, index_name) in MONEY_ETF_WHITELIST.items():
                    metas.setdefault(
                        code,
                        {
                            "code": code,
                            "name": name,
                            "index_code": "",
                            "index_name": index_name,
                            "list_date": None,
                            "category": "money",
                        },
                    )

            codes = list(metas)
            series: dict[str, list[tuple[dt.date, float]]] = {code: [] for code in codes}
            for start in range(0, len(codes), 400):
                chunk = codes[start : start + 400]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT ts_code, trade_date, close
                    FROM fund_daily
                    WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                    ORDER BY ts_code, trade_date
                    """,
                    chunk,
                )
                for code, trade_date, close in cur.fetchall():
                    series.setdefault(str(code), []).append((trade_date, float(close)))
    finally:
        conn.close()
    return metas, series


def audit_2008_drawdowns(
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[dt.date, float]]],
) -> list[dict[str, Any]]:
    start = dt.date(2008, 1, 1)
    end = dt.date(2008, 12, 31)
    rows = []
    for code, meta in metas.items():
        px0 = price_at(series.get(code, []), dt.date(2008, 1, 2))
        px1 = price_at(series.get(code, []), end)
        if px0 is None or px1 is None:
            continue
        dd = series_drawdown(series[code], start, end)
        if dd is None:
            continue
        rows.append(
            {
                "code": code,
                "name": meta["name"],
                "index_name": meta["index_name"],
                "category": meta["category"],
                "list_date": meta["list_date"],
                "calendar_return": px1 / px0 - 1.0,
                "max_drawdown": dd,
            }
        )
    return sorted(rows, key=lambda row: row["max_drawdown"], reverse=True)


def first_defensive_assets(
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[dt.date, float]]],
) -> list[dict[str, Any]]:
    rows = []
    for code, meta in metas.items():
        if meta["category"] not in {"money", "bond", "gold"}:
            continue
        data = series.get(code, [])
        if not data:
            continue
        rows.append(
            {
                "code": code,
                "name": meta["name"],
                "index_name": meta["index_name"],
                "category": meta["category"],
                "first_trade_date": data[0][0].isoformat(),
                "last_trade_date": data[-1][0].isoformat(),
                "rows": len(data),
            }
        )
    return sorted(rows, key=lambda row: row["first_trade_date"])


def oracle_monthly_cases(
    metas: dict[str, dict[str, Any]],
    series: dict[str, list[tuple[dt.date, float]]],
) -> list[dict[str, Any]]:
    trade_dates = sorted({day for rows in series.values() for day, _px in rows})
    cases = []
    for phase in MONTH_PHASES:
        periods = monthly_boundaries(START_YEAR, END_YEAR, phase)
        for lag in EXECUTION_LAGS:
            capital = INITIAL_CAPITAL
            curve = [capital]
            rows = []
            for start_snapshot, end_snapshot in periods:
                start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
                end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
                candidates = []
                for code, meta in metas.items():
                    data = series.get(code, [])
                    start_px = price_at(data, start_exec)
                    end_px = price_at(data, end_exec)
                    if start_px and end_px and start_px > 0:
                        candidates.append((end_px / start_px - 1.0, code, meta))
                if not candidates:
                    period_return = 0.0
                    code = ""
                    meta = {"name": "", "category": ""}
                else:
                    period_return, code, meta = max(candidates, key=lambda item: item[0])
                capital *= 1.0 + period_return
                curve.append(capital)
                rows.append(
                    {
                        "start_exec": start_exec.isoformat(),
                        "end_exec": end_exec.isoformat(),
                        "code": code,
                        "name": meta["name"],
                        "category": meta["category"],
                        "period_return": period_return,
                        "capital": capital,
                    }
                )
            case_mdd = max_drawdown(curve)
            cases.append(
                {
                    "phase_month_offset": phase,
                    "execution_lag_days": lag,
                    "final_capital": capital,
                    "final_capital_wan": capital / 10_000.0,
                    "max_drawdown": case_mdd,
                    "target_met": capital >= TARGET_CAPITAL and case_mdd >= TARGET_MDD,
                    "rows": rows,
                }
            )
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit domestic ETF-only feasibility boundaries.")
    parser.add_argument("--include-money-etfs", action="store_true")
    parser.add_argument("--output-prefix", default=str(OUT_DIR / "scorecard_csi_passive_etf_only_feasibility"))
    args = parser.parse_args()

    metas, series = load_universe(args.include_money_etfs)
    drawdowns_2008 = audit_2008_drawdowns(metas, series)
    defensive_assets = first_defensive_assets(metas, series)
    oracle_cases = oracle_monthly_cases(metas, series)

    prefix = Path(args.output_prefix)
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "objective": "Feasibility audit for domestic ETF-only scorecard target.",
        "include_money_etfs": args.include_money_etfs,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "universe": {
            "etf_count": len(metas),
            "with_price_count": sum(1 for code in metas if series.get(code)),
            "category_counts": {
                category: sum(1 for meta in metas.values() if meta["category"] == category)
                for category in ["equity", "bond", "gold", "money"]
            },
        },
        "defensive_assets_first_available": defensive_assets[:30],
        "drawdowns_2008": drawdowns_2008,
        "oracle_monthly_summary": {
            "case_count": len(oracle_cases),
            "pass_count": sum(1 for case in oracle_cases if case["target_met"]),
            "min_final_capital_wan": min(case["final_capital_wan"] for case in oracle_cases),
            "median_final_capital_wan": sorted(case["final_capital_wan"] for case in oracle_cases)[len(oracle_cases) // 2],
            "worst_max_drawdown": min(case["max_drawdown"] for case in oracle_cases),
            "best_max_drawdown": max(case["max_drawdown"] for case in oracle_cases),
        },
        "oracle_monthly_cases": oracle_cases,
    }

    json_path = Path(f"{prefix}.json")
    drawdown_csv = Path(f"{prefix}_2008_drawdowns.csv")
    oracle_csv = Path(f"{prefix}_oracle_monthly_cases.csv")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with drawdown_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["code", "name", "index_name", "category", "list_date", "calendar_return", "max_drawdown"],
        )
        writer.writeheader()
        writer.writerows(drawdowns_2008)

    with oracle_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["phase_month_offset", "execution_lag_days", "final_capital_wan", "max_drawdown", "target_met"],
        )
        writer.writeheader()
        for case in oracle_cases:
            writer.writerow({field: case[field] for field in writer.fieldnames})

    summary = payload["oracle_monthly_summary"]
    print(
        "oracle_monthly "
        f"pass={summary['pass_count']}/{summary['case_count']} "
        f"min={summary['min_final_capital_wan']:.1f}w "
        f"worst_mdd={summary['worst_max_drawdown']*100:.1f}% "
        f"best_mdd={summary['best_max_drawdown']*100:.1f}%"
    )
    if defensive_assets:
        first = defensive_assets[0]
        print(
            "first_defensive_etf "
            f"{first['code']} {first['name']} {first['category']} {first['first_trade_date']}"
        )
    if drawdowns_2008:
        best_2008 = drawdowns_2008[0]
        print(
            "best_2008_drawdown "
            f"{best_2008['code']} {best_2008['name']} "
            f"mdd={best_2008['max_drawdown']*100:.1f}% "
            f"return={best_2008['calendar_return']*100:.1f}%"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {drawdown_csv}")
    print(f"Wrote {oracle_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
