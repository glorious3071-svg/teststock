#!/usr/bin/env python3
"""Audit observable crash-warning features for scorecard+CSI portfolios.

This script is diagnostic. It expands the best current phase-ensemble candidate
across all month phases and execution lags, labels large-loss months, and checks
which pre-month features separate bad months from ordinary months.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import (
    EXECUTION_LAGS,
    MONTH_PHASES,
    cash_return,
    load_price_series,
    month_end_shift,
    monthly_boundaries,
    period_return,
    shifted_boundary,
)
from scripts.backtest_scorecard_csi_midyear_risk import (
    CS300_CODE,
    END_YEAR,
    INITIAL_CAPITAL,
    START_YEAR,
    load_hybrid_holdings,
)
from scripts.backtest_scorecard_csi_phase_ensemble import RULES as PHASE_RULES, ensemble_state
from scripts.backtest_scorecard_csi_vol_target import load_us10y_yields, us10y_duration_return

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_crash_feature_audit.json"
OUT_ROWS_CSV = OUT_DIR / "scorecard_csi_crash_feature_rows.csv"
OUT_FEATURES_CSV = OUT_DIR / "scorecard_csi_crash_feature_summary.csv"

PHASE_RULE_NAME = "phase12_lever120_us10y"
LARGE_LOSS_THRESHOLD = -0.08


@dataclass(frozen=True)
class FeatureScore:
    feature: str
    count: int
    bad_count: int
    ok_median: float | None
    bad_median: float | None
    separation: float | None
    best_direction: str | None
    best_threshold: float | None
    best_precision: float | None
    best_recall: float | None
    best_flag_rate: float | None
    bad_capture_count: int | None
    false_positive_count: int | None


def phase_rule():
    for rule in PHASE_RULES:
        if rule.name == PHASE_RULE_NAME:
            return rule
    raise KeyError(PHASE_RULE_NAME)


def price_at(rows: list[tuple[date, float]], boundary: date) -> float | None:
    i = bisect_right(rows, (boundary, math.inf)) - 1
    return rows[i][1] if i >= 0 else None


def trailing_values(rows: list[tuple[date, float]], boundary: date, days: int) -> list[tuple[date, float]]:
    i = bisect_right(rows, (boundary, math.inf))
    return rows[max(0, i - days) : i]


def trailing_returns(rows: list[tuple[date, float]], boundary: date, days: int) -> list[float]:
    values = trailing_values(rows, boundary, days + 1)
    returns = []
    for prev, cur in zip(values, values[1:]):
        if prev[1] > 0:
            returns.append(cur[1] / prev[1] - 1.0)
    return returns


def annualized_vol(rows: list[tuple[date, float]], boundary: date, days: int) -> float | None:
    returns = trailing_returns(rows, boundary, days)
    if len(returns) < max(5, min(days, 20)):
        return None
    return statistics.pstdev(returns) * math.sqrt(252.0)


def trailing_drawdown(rows: list[tuple[date, float]], boundary: date, days: int) -> float | None:
    values = [value for _day, value in trailing_values(rows, boundary, days)]
    current = price_at(rows, boundary)
    if not values or current is None:
        return None
    high = max(values)
    return current / high - 1.0 if high > 0 else None


def moving_average_distance(rows: list[tuple[date, float]], boundary: date, days: int) -> float | None:
    values = [value for _day, value in trailing_values(rows, boundary, days)]
    current = price_at(rows, boundary)
    if len(values) < max(5, min(days, 20)) or current is None:
        return None
    ma = statistics.mean(values)
    return current / ma - 1.0 if ma > 0 else None


def percentile_rank(values: list[float], current: float) -> float | None:
    clean = [value for value in values if value is not None and value > 0]
    if len(clean) < 20:
        return None
    return sum(1 for value in clean if value <= current) / len(clean)


def load_basic_series(conn) -> dict[str, list[tuple[date, float]]]:
    out = {"pb": [], "pe_ttm": [], "turnover_rate": []}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, pb, pe_ttm, turnover_rate
            FROM index_dailybasic
            WHERE ts_code=%s
            ORDER BY trade_date
            """,
            (CS300_CODE,),
        )
        for trade_date, pb, pe_ttm, turnover_rate in cur.fetchall():
            if pb is not None:
                out["pb"].append((trade_date, float(pb)))
            if pe_ttm is not None:
                out["pe_ttm"].append((trade_date, float(pe_ttm)))
            if turnover_rate is not None:
                out["turnover_rate"].append((trade_date, float(turnover_rate)))
    return out


def load_margin_series(conn) -> list[tuple[date, float]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, SUM(rzrqye)
            FROM margin_daily
            WHERE rzrqye IS NOT NULL
            GROUP BY trade_date
            ORDER BY trade_date
            """
        )
        return [(trade_date, float(value)) for trade_date, value in cur.fetchall() if value is not None]


def scalar_at(rows: list[tuple[date, float]], boundary: date) -> float | None:
    return price_at(rows, boundary)


def scalar_return(rows: list[tuple[date, float]], boundary: date, days: int) -> float | None:
    current = price_at(rows, boundary)
    past_values = trailing_values(rows, boundary, days + 1)
    if current is None or len(past_values) < max(5, min(days, 20)):
        return None
    past = past_values[0][1]
    if past <= 0:
        return None
    return current / past - 1.0


def feature_snapshot(
    series: dict[str, list[tuple[date, float]]],
    basic: dict[str, list[tuple[date, float]]],
    margin: list[tuple[date, float]],
    snapshot: date,
) -> dict[str, float | None]:
    cs300 = series[CS300_CODE]
    features: dict[str, float | None] = {}
    for months in [1, 3, 6, 12]:
        features[f"cs300_ret_{months}m"] = period_return(series, CS300_CODE, month_end_shift(snapshot, -months), snapshot)
    for days in [20, 60, 120]:
        features[f"cs300_vol_{days}d"] = annualized_vol(cs300, snapshot, days)
        features[f"cs300_dd_{days}d"] = trailing_drawdown(cs300, snapshot, days)
    for days in [60, 120, 250]:
        features[f"cs300_dist_ma{days}"] = moving_average_distance(cs300, snapshot, days)
    for name, rows in basic.items():
        current = scalar_at(rows, snapshot)
        features[name] = current
        history = [value for _day, value in trailing_values(rows, snapshot, 750)]
        features[f"{name}_pct_3y"] = percentile_rank(history, current) if current is not None else None
        if name == "turnover_rate":
            features["turnover_20d_chg"] = scalar_return(rows, snapshot, 20)
            features["turnover_60d_chg"] = scalar_return(rows, snapshot, 60)
    features["margin_balance"] = scalar_at(margin, snapshot)
    features["margin_20d_chg"] = scalar_return(margin, snapshot, 20)
    features["margin_60d_chg"] = scalar_return(margin, snapshot, 60)
    features["margin_120d_chg"] = scalar_return(margin, snapshot, 120)
    return features


def build_rows(conn) -> list[dict[str, Any]]:
    rule = phase_rule()
    series = load_price_series(conn)
    yields = load_us10y_yields(conn)
    basic = load_basic_series(conn)
    margin = load_margin_series(conn)
    trade_dates = [d for d, _px in series[CS300_CODE]]
    holdings = load_hybrid_holdings()
    rows: list[dict[str, Any]] = []

    for phase in MONTH_PHASES:
        for lag in EXECUTION_LAGS:
            capital = INITIAL_CAPITAL
            peak = capital
            for start_snapshot, end_snapshot in monthly_boundaries(START_YEAR, END_YEAR, phase):
                start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
                end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
                target_pct, equity_return, _sleeves, reasons = ensemble_state(
                    conn,
                    series,
                    holdings,
                    rule,
                    start_snapshot,
                    start_exec,
                    end_exec,
                    capital / peak - 1.0,
                )
                def_return = us10y_duration_return(yields, start_exec, end_exec)
                equity_weight = target_pct / 100.0
                non_equity_return = cash_return(start_exec, end_exec) if equity_weight > 1.0 else def_return
                month_return = equity_weight * equity_return + (1.0 - equity_weight) * non_equity_return
                capital *= 1.0 + month_return
                peak = max(peak, capital)
                item: dict[str, Any] = {
                    "phase_month_offset": phase,
                    "execution_lag_days": lag,
                    "snapshot": start_snapshot.isoformat(),
                    "start_exec": start_exec.isoformat(),
                    "end_exec": end_exec.isoformat(),
                    "target_equity_pct": target_pct,
                    "equity_return": equity_return,
                    "defensive_return": def_return,
                    "month_return": month_return,
                    "capital": capital,
                    "portfolio_drawdown": capital / peak - 1.0,
                    "large_loss": month_return <= LARGE_LOSS_THRESHOLD,
                    "rebalance_reasons": "|".join(sorted(set(reasons))),
                }
                item.update(feature_snapshot(series, basic, margin, start_snapshot))
                rows.append(item)
    return rows


def quantiles(values: list[float]) -> list[float]:
    clean = sorted(values)
    if not clean:
        return []
    qs = []
    for pct in [0.05, 0.10, 0.15, 0.20, 0.25, 0.75, 0.80, 0.85, 0.90, 0.95]:
        idx = min(len(clean) - 1, max(0, int(round((len(clean) - 1) * pct))))
        qs.append(clean[idx])
    return sorted(set(qs))


def score_feature(rows: list[dict[str, Any]], feature: str) -> FeatureScore:
    usable = [row for row in rows if isinstance(row.get(feature), (int, float))]
    bad = [row for row in usable if row["large_loss"]]
    ok = [row for row in usable if not row["large_loss"]]
    bad_count = len(bad)
    if not usable or not bad or not ok:
        return FeatureScore(feature, len(usable), bad_count, None, None, None, None, None, None, None, None, None, None)

    bad_values = [float(row[feature]) for row in bad]
    ok_values = [float(row[feature]) for row in ok]
    ok_median = statistics.median(ok_values)
    bad_median = statistics.median(bad_values)
    separation = bad_median - ok_median
    best: tuple[float, str, float, float, float, int, int] | None = None
    for threshold in quantiles([float(row[feature]) for row in usable]):
        for direction in ["lte", "gte"]:
            flagged = [
                row for row in usable
                if (float(row[feature]) <= threshold if direction == "lte" else float(row[feature]) >= threshold)
            ]
            if not flagged:
                continue
            captured = sum(1 for row in flagged if row["large_loss"])
            false_pos = len(flagged) - captured
            precision = captured / len(flagged)
            recall = captured / bad_count
            flag_rate = len(flagged) / len(usable)
            # Prefer high recall, then precision, while penalizing broad flags.
            score = recall * 2.0 + precision - flag_rate * 0.25
            candidate = (score, direction, threshold, precision, recall, flag_rate, captured, false_pos)
            if best is None or candidate > best:
                best = candidate
    if best is None:
        return FeatureScore(feature, len(usable), bad_count, ok_median, bad_median, separation, None, None, None, None, None, None, None)
    _score, direction, threshold, precision, recall, flag_rate, captured, false_pos = best
    return FeatureScore(
        feature,
        len(usable),
        bad_count,
        ok_median,
        bad_median,
        separation,
        direction,
        threshold,
        precision,
        recall,
        flag_rate,
        captured,
        false_pos,
    )


def write_outputs(rows: list[dict[str, Any]], scores: list[FeatureScore]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with OUT_ROWS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    summary_fields = list(FeatureScore.__dataclass_fields__.keys())
    with OUT_FEATURES_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        for score in scores:
            writer.writerow(score.__dict__)
    payload = {
        "objective": "Audit observable pre-month features for large-loss scorecard+CSI phase-ensemble months.",
        "phase_rule": PHASE_RULE_NAME,
        "large_loss_threshold": LARGE_LOSS_THRESHOLD,
        "row_count": len(rows),
        "large_loss_count": sum(1 for row in rows if row["large_loss"]),
        "top_features": [score.__dict__ for score in scores[:20]],
        "rows_csv": str(OUT_ROWS_CSV),
        "features_csv": str(OUT_FEATURES_CSV),
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    conn = get_connection()
    try:
        rows = build_rows(conn)
    finally:
        conn.close()
    ignored = {
        "phase_month_offset",
        "execution_lag_days",
        "snapshot",
        "start_exec",
        "end_exec",
        "target_equity_pct",
        "equity_return",
        "defensive_return",
        "month_return",
        "capital",
        "portfolio_drawdown",
        "large_loss",
        "rebalance_reasons",
    }
    features = [key for key in rows[0] if key not in ignored]
    scores = [score_feature(rows, feature) for feature in features]
    scores.sort(
        key=lambda item: (
            item.best_recall or 0.0,
            item.best_precision or 0.0,
            -(item.best_flag_rate or 1.0),
            abs(item.separation or 0.0),
        ),
        reverse=True,
    )
    write_outputs(rows, scores)
    print(
        f"Crash feature audit rows={len(rows)} large_loss={sum(1 for row in rows if row['large_loss'])} "
        f"threshold={LARGE_LOSS_THRESHOLD:.1%}"
    )
    for score in scores[:12]:
        sep = score.separation if score.separation is not None else 0.0
        print(
            f"  {score.feature:<24} dir={score.best_direction or '-':<3} "
            f"thr={(score.best_threshold if score.best_threshold is not None else 0):>8.4f} "
            f"recall={(score.best_recall or 0) * 100:5.1f}% "
            f"precision={(score.best_precision or 0) * 100:5.1f}% "
            f"flag={(score.best_flag_rate or 0) * 100:5.1f}% "
            f"bad_med={(score.bad_median if score.bad_median is not None else 0):>8.4f} "
            f"ok_med={(score.ok_median if score.ok_median is not None else 0):>8.4f} "
            f"sep={sep:>8.4f}"
        )
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_ROWS_CSV}")
    print(f"Wrote {OUT_FEATURES_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
