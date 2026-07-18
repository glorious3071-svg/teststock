#!/usr/bin/env python3
"""Audit defensive assets before using them in scorecard portfolio overlays."""

from __future__ import annotations

import json
import math
import sys
from bisect import bisect_right
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "defensive_asset_candidates.json"

CS300_CODE = "000300.SH"
CASH_ANNUAL_RATE = 0.02
CANDIDATES = {
    "cash_2pct": {"table": "cash", "code": "cash", "label": "Cash at 2% annualized"},
    "bond_5y_index": {"table": "index_daily", "code": "000140.CSI", "label": "5Y China treasury index"},
    "us10y_duration_proxy": {"table": "us_tycr_daily", "code": "y10", "label": "US 10Y treasury duration proxy"},
    "gold_spot": {"table": "gold_daily", "code": "AU9999.SGE", "label": "SGE Au99.99 spot gold"},
    "spx_us": {"table": "us_index_daily", "code": "SPX.US", "label": "S&P 500"},
}


def max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def annualized_return(final_value: float, years: float) -> float:
    if years <= 0:
        return 0.0
    return final_value ** (1.0 / years) - 1.0


def load_series(conn, table: str, code: str) -> list[tuple[date, float]]:
    if table == "cash":
        return []
    if table == "us_tycr_daily":
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT trade_date, {code}
                FROM us_tycr_daily
                WHERE {code} IS NOT NULL
                ORDER BY trade_date
                """
            )
            return [(row[0], float(row[1])) for row in cur.fetchall() if row[1] is not None]
    code_col = "symbol" if table == "gold_daily" else "ts_code"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT trade_date, close
            FROM {table}
            WHERE {code_col}=%s AND close IS NOT NULL
            ORDER BY trade_date
            """,
            (code,),
        )
        return [(row[0], float(row[1])) for row in cur.fetchall() if row[1]]


def price_at(series: list[tuple[date, float]], boundary: date) -> float | None:
    i = bisect_right(series, (boundary, math.inf)) - 1
    return series[i][1] if i >= 0 else None


def period_return(series: list[tuple[date, float]], start: date, end: date) -> float | None:
    start_price = price_at(series, start)
    end_price = price_at(series, end)
    if not start_price or not end_price or start_price <= 0:
        return None
    return end_price / start_price - 1.0


def cash_period_return(start: date, end: date) -> float:
    return CASH_ANNUAL_RATE * max((end - start).days, 0) / 365.25


def us10y_duration_return(series: list[tuple[date, float]], start: date, end: date, duration: float = 7.0) -> float | None:
    start_yield = price_at(series, start)
    end_yield = price_at(series, end)
    if start_yield is None or end_yield is None:
        return None
    carry = start_yield / 100.0 * max((end - start).days, 0) / 365.25
    price_return = -duration * (end_yield - start_yield) / 100.0
    return carry + price_return


def yearly_periods(start_year: int = 2006, end_year: int = 2025) -> list[tuple[date, date]]:
    return [(date(year - 1, 12, 31), date(year, 12, 31)) for year in range(start_year, end_year + 1)]


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = mean(xs)
    my = mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return num / den_x / den_y


def summarize_candidate(
    name: str,
    spec: dict[str, str],
    series: list[tuple[date, float]],
    cs300_series: list[tuple[date, float]],
    periods: list[tuple[date, date]],
) -> dict[str, Any]:
    returns = []
    cs_returns = []
    curve = [1.0]
    for start, end in periods:
        if spec["table"] == "cash":
            ret = cash_period_return(start, end)
        elif spec["table"] == "us_tycr_daily":
            value = us10y_duration_return(series, start, end)
            if value is None:
                continue
            ret = value
        else:
            value = period_return(series, start, end)
            if value is None:
                continue
            ret = value
        cs_ret = period_return(cs300_series, start, end)
        if cs_ret is None:
            continue
        returns.append(ret)
        cs_returns.append(cs_ret)
        curve.append(curve[-1] * (1.0 + ret))

    first_date = series[0][0].isoformat() if series else None
    last_date = series[-1][0].isoformat() if series else None
    years = len(returns)
    final_value = curve[-1]
    return {
        "name": name,
        "label": spec["label"],
        "code": spec["code"],
        "table": spec["table"],
        "first_date": first_date,
        "last_date": last_date,
        "tested_years": years,
        "coverage_ratio": years / len(periods),
        "mean_annual_return": mean(returns) if returns else None,
        "annualized_return": annualized_return(final_value, years) if returns else None,
        "max_drawdown": max_drawdown(curve) if returns else None,
        "correlation_to_cs300": pearson(returns, cs_returns),
        "worst_annual_return": min(returns) if returns else None,
        "best_annual_return": max(returns) if returns else None,
        "final_multiple": final_value,
    }


def main() -> int:
    conn = get_connection()
    try:
        cs300_series = load_series(conn, "index_daily", CS300_CODE)
        periods = yearly_periods()
        rows = []
        for name, spec in CANDIDATES.items():
            series = load_series(conn, spec["table"], spec["code"])
            rows.append(summarize_candidate(name, spec, series, cs300_series, periods))
    finally:
        conn.close()

    payload = {
        "objective": "Audit defensive candidates before adding a non-cash risk-off leg.",
        "period": "2006-2025 annual boundaries",
        "candidates": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Defensive asset candidates")
    for row in rows:
        ann = row["annualized_return"]
        mdd = row["max_drawdown"]
        corr = row["correlation_to_cs300"]
        print(
            f"  {row['name']:<14} coverage={row['coverage_ratio'] * 100:5.1f}% "
            f"ann={(ann * 100 if ann is not None else 0):6.2f}% "
            f"mdd={(mdd * 100 if mdd is not None else 0):6.2f}% "
            f"corr_cs300={(corr if corr is not None else 0):6.2f}"
        )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
