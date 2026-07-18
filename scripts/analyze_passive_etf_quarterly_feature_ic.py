#!/usr/bin/env python3
"""Point-in-time feature IC audit for domestic passive equity ETFs.

Every month can be a three-month rebalance anchor.  The ETF universe, tracker
choice, features, and liquidity are frozen using data observable at the anchor;
future three-month ETF returns are labels only.  Results are split into eras so
one short recent ETF boom cannot make a feature look generally reliable.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.domestic_equity_etf import load_equity_etf_return_universe
from backtest.monthly_online_selector import rank_correlation
from backtest.phase_schedule import shift_month_end
from db.connection import get_connection


FEATURES = (
    "momentum_1m",
    "momentum_3m",
    "momentum_6m",
    "momentum_12m",
    "momentum_12m_skip1m",
    "relative_strength_3m",
    "relative_strength_6m",
    "residual_momentum_6m",
    "trend_1m_3m_consistency",
    "distance_high_12m",
    "volatility_1m",
    "volatility_3m",
    "volatility_6m",
    "downside_volatility_3m",
    "drawdown_3m",
    "drawdown_6m",
    "max_drawdown_6m",
    "positive_day_ratio_3m",
    "amount_acceleration_1m_6m",
    "amount_crowding_percentile_3y",
    "log_amount_1m",
    "amihud_illiquidity_3m",
    "return_amount_correlation_3m",
    "market_beta_6m",
    "market_correlation_6m",
    "listing_age_years",
    "historical_var_5pct_3m",
    "historical_cvar_5pct_3m",
    "historical_var_5pct_6m",
    "historical_cvar_5pct_6m",
    "maximum_daily_loss_3m",
    "negative_day_ratio_3m",
    "return_skewness_3m",
    "return_excess_kurtosis_3m",
    "return_autocorrelation_3m",
    "ulcer_index_6m",
    "days_since_high_6m",
    "volatility_acceleration_1m_3m",
)


@dataclass(frozen=True)
class DailySeries:
    dates: tuple[date, ...]
    factors: tuple[float, ...]
    returns: tuple[float, ...]
    amounts: tuple[float, ...]


def parse_date(raw: str) -> date:
    return date.fromisoformat(raw)


def era(day: date) -> str:
    if day.year <= 2012:
        return "2005_2012"
    if day.year <= 2018:
        return "2013_2018"
    return "2019_latest"


def market_regime(benchmark: dict[date, float], snapshot: date) -> tuple[str, float | None]:
    values = [value for day, value in benchmark.items() if day <= snapshot]
    if len(values) < 126:
        return "unknown", None
    trailing = math.prod(1.0 + value for value in values[-126:]) - 1.0
    if trailing >= 0.10:
        return "bull", trailing
    if trailing <= -0.10:
        return "bear", trailing
    return "neutral", trailing


def safe_return(values: tuple[float, ...], end: int, observations: int) -> float | None:
    if end < observations + 1 or values[end - observations - 1] <= 0:
        return None
    return values[end - 1] / values[end - observations - 1] - 1.0


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 20 or len(xs) != len(ys):
        return None
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    if sx <= 0 or sy <= 0:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    return statistics.mean((x - mx) * (y - my) for x, y in zip(xs, ys)) / sx / sy


def max_drawdown(values: tuple[float, ...]) -> float | None:
    if not values:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def percentile(values: tuple[float, ...], current: float) -> float | None:
    if len(values) < 126:
        return None
    return sum(value <= current for value in values) / len(values)


def lower_tail(values: list[float], probability: float = 0.05) -> tuple[float, float]:
    ordered = sorted(values)
    count = max(1, int(math.ceil(len(ordered) * probability)))
    tail = ordered[:count]
    return tail[-1], statistics.mean(tail)


def standardized_moment(values: list[float], order: int) -> float:
    center = statistics.mean(values)
    scale = statistics.pstdev(values)
    if scale <= 0:
        return 0.0
    moment = statistics.mean(((value - center) / scale) ** order for value in values)
    return moment - 3.0 if order == 4 else moment


def lag_one_correlation(values: list[float]) -> float | None:
    return correlation(values[:-1], values[1:]) if len(values) >= 21 else None


def load_daily(conn, codes: list[str]) -> dict[str, DailySeries]:
    raw: dict[str, list[tuple[date, float, float]]] = {code: [] for code in codes}
    with conn.cursor() as cur:
        for start in range(0, len(codes), 250):
            chunk = codes[start : start + 250]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, pct_chg, amount
                FROM fund_daily
                WHERE ts_code IN ({placeholders})
                  AND close IS NOT NULL AND pct_chg IS NOT NULL AND amount IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            for code, day, pct_chg, amount in cur.fetchall():
                raw[str(code)].append((day, float(pct_chg) / 100.0, float(amount)))
    output = {}
    for code, rows in raw.items():
        factor = 100.0
        factors = []
        for _day, daily_return, _amount in rows:
            factor *= max(1.0 + daily_return, 1e-9)
            factors.append(factor)
        output[code] = DailySeries(
            tuple(row[0] for row in rows),
            tuple(factors),
            tuple(row[1] for row in rows),
            tuple(row[2] for row in rows),
        )
    return output


def benchmark_returns(conn) -> dict[date, float]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, pct_chg
            FROM index_daily
            WHERE ts_code='000300.SH' AND pct_chg IS NOT NULL
            ORDER BY trade_date
            """
        )
        return {day: float(value) / 100.0 for day, value in cur.fetchall()}


def features_at(
    series: DailySeries,
    snapshot: date,
    benchmark: dict[date, float],
    list_date: date,
) -> dict[str, float | None]:
    end = bisect_right(series.dates, snapshot)
    if end < 64:
        return {}
    factors = series.factors
    returns = series.returns
    amounts = series.amounts
    r1 = safe_return(factors, end, 21)
    r3 = safe_return(factors, end, 63)
    r6 = safe_return(factors, end, 126)
    r12 = safe_return(factors, end, 252)
    r12_skip = (
        factors[end - 22] / factors[end - 253] - 1.0
        if end >= 253 and factors[end - 253] > 0
        else None
    )
    market_dates = series.dates[max(0, end - 126) : end]
    paired = [
        (returns[index], benchmark[series.dates[index]])
        for index in range(max(0, end - 126), end)
        if series.dates[index] in benchmark
    ]
    beta = None
    market_corr = None
    market_r3 = None
    market_r6 = None
    if len(paired) >= 60:
        asset = [item[0] for item in paired]
        market = [item[1] for item in paired]
        market_variance = statistics.pvariance(market)
        if market_variance > 0:
            beta = statistics.mean(
                (x - statistics.mean(asset)) * (y - statistics.mean(market))
                for x, y in paired
            ) / market_variance
        market_corr = correlation(asset, market)
        market_r6 = math.prod(1.0 + value for value in market) - 1.0
        market_r3 = math.prod(1.0 + value for value in market[-63:]) - 1.0
    ret_3m = list(returns[end - 63 : end])
    ret_6m = list(returns[max(0, end - 126) : end])
    amount_1m = statistics.mean(amounts[end - 21 : end])
    amount_6m = statistics.mean(amounts[max(0, end - 126) : end])
    amount_history = amounts[max(0, end - 756) : end]
    amount_changes = [
        amounts[index] / amounts[index - 1] - 1.0
        for index in range(end - 62, end)
        if amounts[index - 1] > 0
    ]
    amount_return_corr = correlation(ret_3m[-len(amount_changes) :], amount_changes)
    downside = [min(value, 0.0) for value in ret_3m]
    var_3m, cvar_3m = lower_tail(ret_3m)
    var_6m, cvar_6m = lower_tail(ret_6m)
    amihud = statistics.mean(
        abs(ret) / max(amount, 1.0)
        for ret, amount in zip(ret_3m, amounts[end - 63 : end])
    )
    high_window = factors[max(0, end - 252) : end]
    drawdown_path_6m = []
    six_month_start = max(0, end - 126)
    six_month_values = factors[six_month_start:end]
    running_high = six_month_values[0]
    for value in six_month_values:
        running_high = max(running_high, value)
        drawdown_path_6m.append(value / running_high - 1.0)
    high_6m = max(six_month_values)
    days_since_high = next(
        (
            offset
            for offset, value in enumerate(reversed(six_month_values))
            if value >= high_6m * (1.0 - 1e-12)
        ),
        len(six_month_values) - 1,
    )
    vol_1m = statistics.pstdev(returns[end - 21 : end]) * math.sqrt(252.0)
    vol_3m = statistics.pstdev(ret_3m) * math.sqrt(252.0)
    return {
        "momentum_1m": r1,
        "momentum_3m": r3,
        "momentum_6m": r6,
        "momentum_12m": r12,
        "momentum_12m_skip1m": r12_skip,
        "relative_strength_3m": r3 - market_r3 if r3 is not None and market_r3 is not None else None,
        "relative_strength_6m": r6 - market_r6 if r6 is not None and market_r6 is not None else None,
        "residual_momentum_6m": (
            r6 - beta * market_r6
            if r6 is not None and beta is not None and market_r6 is not None
            else None
        ),
        "trend_1m_3m_consistency": (
            min(r1, r3 / 3.0) if r1 is not None and r3 is not None else None
        ),
        "distance_high_12m": factors[end - 1] / max(high_window) - 1.0,
        "volatility_1m": vol_1m,
        "volatility_3m": vol_3m,
        "volatility_6m": statistics.pstdev(ret_6m) * math.sqrt(252.0),
        # Downside deviation is the root mean square of returns below zero.
        # pstdev(min(r, 0)) subtracts the truncated-series mean and therefore
        # systematically understates downside risk.
        "downside_volatility_3m": math.sqrt(
            statistics.mean(value * value for value in downside)
        ) * math.sqrt(252.0),
        "drawdown_3m": factors[end - 1] / max(factors[end - 63 : end]) - 1.0,
        "drawdown_6m": factors[end - 1] / max(factors[max(0, end - 126) : end]) - 1.0,
        "max_drawdown_6m": max_drawdown(factors[max(0, end - 126) : end]),
        "positive_day_ratio_3m": sum(value > 0 for value in ret_3m) / len(ret_3m),
        "amount_acceleration_1m_6m": amount_1m / amount_6m - 1.0 if amount_6m > 0 else None,
        "amount_crowding_percentile_3y": percentile(amount_history, amount_1m),
        "log_amount_1m": math.log1p(max(amount_1m, 0.0)),
        "amihud_illiquidity_3m": math.log1p(amihud * 1e12),
        "return_amount_correlation_3m": amount_return_corr,
        "market_beta_6m": beta,
        "market_correlation_6m": market_corr,
        "listing_age_years": max((snapshot - list_date).days / 365.25, 0.0),
        "historical_var_5pct_3m": var_3m,
        "historical_cvar_5pct_3m": cvar_3m,
        "historical_var_5pct_6m": var_6m,
        "historical_cvar_5pct_6m": cvar_6m,
        "maximum_daily_loss_3m": min(ret_3m),
        "negative_day_ratio_3m": sum(value < 0 for value in ret_3m) / len(ret_3m),
        "return_skewness_3m": standardized_moment(ret_3m, 3),
        "return_excess_kurtosis_3m": standardized_moment(ret_3m, 4),
        "return_autocorrelation_3m": lag_one_correlation(ret_3m),
        "ulcer_index_6m": math.sqrt(
            statistics.mean(value * value for value in drawdown_path_6m)
        ),
        "days_since_high_6m": float(days_since_high),
        "volatility_acceleration_1m_3m": vol_1m / vol_3m - 1.0 if vol_3m > 0 else None,
    }


def forward_return(series: DailySeries, start: date, end: date) -> float | None:
    left = bisect_right(series.dates, start)
    right = bisect_right(series.dates, end)
    if left <= 0 or right <= left or series.factors[left - 1] <= 0:
        return None
    return series.factors[right - 1] / series.factors[left - 1] - 1.0


def forward_path_labels(
    series: DailySeries,
    start: date,
    end: date,
) -> dict[str, float] | None:
    """Return labels for a frozen three-month holding window.

    These values are deliberately kept out of ``features_at``.  They become
    available to a walk-forward learner only after ``end`` has passed.
    """

    left = bisect_right(series.dates, start)
    right = bisect_right(series.dates, end)
    if left <= 0 or right <= left or series.factors[left - 1] <= 0:
        return None
    start_factor = series.factors[left - 1]
    path = (start_factor,) + tuple(series.factors[left:right])
    return {
        "forward_return_3m": path[-1] / start_factor - 1.0,
        "forward_max_drawdown_3m": float(max_drawdown(path) or 0.0),
        "forward_worst_from_start_3m": min(value / start_factor - 1.0 for value in path),
    }


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean_ic": None, "median_ic": None, "positive_rate": None}
    return {
        "count": len(values),
        "mean_ic": statistics.mean(values),
        "median_ic": statistics.median(values),
        "positive_rate": sum(value > 0 for value in values) / len(values),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_date, default=date(2005, 3, 31))
    parser.add_argument("--end", type=parse_date, default=date.today())
    parser.add_argument("--step-months", type=int, default=1)
    parser.add_argument("--output-prefix", default="data/backtests/passive_etf_quarterly_feature_ic")
    args = parser.parse_args()

    conn = get_connection()
    observations: list[dict[str, Any]] = []
    candidate_observations: list[dict[str, Any]] = []
    try:
        metas_by_index, _price_series = load_equity_etf_return_universe(conn)
        metas = [meta for values in metas_by_index.values() for meta in values]
        daily = load_daily(conn, sorted(meta.code for meta in metas))
        benchmark = benchmark_returns(conn)
        snapshot = args.start
        while snapshot <= args.end:
            end_snapshot = shift_month_end(snapshot, 3)
            regime, benchmark_return_6m = market_regime(benchmark, snapshot)
            # One point-in-time liquid tracker per index prevents duplicate funds
            # from overweighting the same economic exposure in the IC sample.
            representatives = []
            for index_code, index_metas in metas_by_index.items():
                eligible = []
                for meta in index_metas:
                    series = daily.get(meta.code)
                    if not series or meta.list_date > snapshot or meta.first_trade_date > snapshot:
                        continue
                    cut = bisect_right(series.dates, snapshot)
                    if cut < 64:
                        continue
                    liquidity = statistics.mean(series.amounts[cut - 21 : cut])
                    eligible.append((liquidity, meta.code, meta))
                if eligible:
                    representatives.append(max(eligible, key=lambda item: (item[0], item[1]))[2])
            rows = []
            outcomes = {}
            for meta in representatives:
                series = daily[meta.code]
                labels = (
                    forward_path_labels(series, snapshot, end_snapshot)
                    if end_snapshot <= args.end
                    else None
                )
                item = {"ts_code": meta.code, "index_code": meta.index_code}
                item.update(features_at(series, snapshot, benchmark, meta.list_date))
                candidate_observations.append(
                    {
                        "snapshot": snapshot.isoformat(),
                        "end_snapshot": end_snapshot.isoformat(),
                        "era": era(snapshot),
                        "market_regime": regime,
                        "market_return_6m": benchmark_return_6m,
                        **item,
                        **(
                            labels
                            if labels is not None
                            else {
                                "forward_return_3m": None,
                                "forward_max_drawdown_3m": None,
                                "forward_worst_from_start_3m": None,
                            }
                        ),
                    }
                )
                if labels is None:
                    continue
                rows.append(item)
                outcomes[meta.code] = labels["forward_return_3m"]
            if len(rows) >= 5:
                for feature in FEATURES:
                    usable = [row for row in rows if row.get(feature) is not None]
                    if len(usable) < 5:
                        continue
                    ic = rank_correlation(
                        [float(row[feature]) for row in usable],
                        [outcomes[str(row["ts_code"])] for row in usable],
                    )
                    if ic is not None:
                        observations.append(
                            {
                                "snapshot": snapshot.isoformat(),
                                "end_snapshot": end_snapshot.isoformat(),
                                "era": era(snapshot),
                                "market_regime": regime,
                                "market_return_6m": benchmark_return_6m,
                                "feature": feature,
                                "candidate_count": len(usable),
                                "ic": ic,
                            }
                        )
            snapshot = shift_month_end(snapshot, max(args.step_months, 1))
    finally:
        conn.close()

    grouped: dict[str, list[float]] = defaultdict(list)
    grouped_era: dict[tuple[str, str], list[float]] = defaultdict(list)
    grouped_regime: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in observations:
        grouped[row["feature"]].append(float(row["ic"]))
        grouped_era[(row["feature"], row["era"])].append(float(row["ic"]))
        grouped_regime[(row["feature"], row["market_regime"])].append(float(row["ic"]))
    summaries = []
    era_names = ("2005_2012", "2013_2018", "2019_latest")
    for feature in FEATURES:
        total = summarize(grouped[feature])
        eras = {name: summarize(grouped_era[(feature, name)]) for name in era_names}
        regimes = {
            name: summarize(grouped_regime[(feature, name)])
            for name in ("bull", "neutral", "bear")
        }
        direction = 1.0 if float(total["median_ic"] or 0.0) >= 0 else -1.0
        era_medians = [float(item["median_ic"]) for item in eras.values() if item["median_ic"] is not None]
        summaries.append(
            {
                "feature": feature,
                **total,
                "stable_era_count": sum(value * direction > 0 for value in era_medians),
                "era_count": len(era_medians),
                "eras": eras,
                "regimes": regimes,
            }
        )
    summaries.sort(
        key=lambda item: (
            item["stable_era_count"],
            abs(float(item["median_ic"] or 0.0)),
            abs(float(item["mean_ic"] or 0.0)),
        ),
        reverse=True,
    )

    prefix = Path(args.output_prefix)
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_summary.csv")
    dataset_path = Path(f"{prefix}_dataset.json")
    json_path.write_text(
        json.dumps(
            {
                "method": "monthly anchors, point-in-time domestic passive ETF features, next 3m returns",
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "observations": observations,
                "summaries": summaries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    dataset_path.write_text(
        json.dumps(
            {
                "method": (
                    "monthly point-in-time domestic passive ETF candidates; "
                    "three-month labels released only after end_snapshot; "
                    "latest unlabeled snapshots retained for live inference"
                ),
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "features": list(FEATURES),
                "candidate_observations": candidate_observations,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["feature", "count", "mean_ic", "median_ic", "positive_rate", "stable_era_count", "era_count"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in summaries:
            writer.writerow({field: item[field] for field in fields})
    for item in summaries:
        print(
            f"{item['feature']:<38} n={item['count']:>3} "
            f"median={float(item['median_ic'] or 0):+.4f} "
            f"mean={float(item['mean_ic'] or 0):+.4f} "
            f"eras={item['stable_era_count']}/{item['era_count']}"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {dataset_path} ({len(candidate_observations)} candidates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
