#!/usr/bin/env python3
"""Add point-in-time exchange ETF share and subscription-flow features.

Only ``etf_share_size_snapshot`` is used.  The less reliable proxy obtained by
dividing ``fund_nav.net_asset`` by unit NAV is intentionally excluded.  A
snapshot is usable only after its conservative ``available_date`` and after
the ETF's listing date.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection


SOURCE = ROOT / "data/backtests/passive_etf_quarterly_constituent_v4_dataset.json"
OUTPUT = ROOT / "data/backtests/passive_etf_quarterly_share_v5_dataset.json"
FEATURES = (
    "etf_share_log_total_wan",
    "etf_size_log_total_wan",
    "etf_share_growth_1q",
    "etf_share_growth_2q",
    "etf_share_growth_4q",
    "etf_size_growth_1q",
    "etf_size_growth_2q",
    "etf_size_growth_4q",
    "etf_subscription_flow_1q",
    "etf_subscription_flow_2q",
    "etf_subscription_flow_4q",
    "etf_share_observation_age_days",
)


def finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0


def observations_available(
    history: list[dict[str, Any]],
    snapshot: date,
    list_date: date | None,
) -> list[dict[str, Any]]:
    """Return observations known by snapshot and no earlier than listing."""

    return [
        row
        for row in history
        if row["available_date"] <= snapshot
        and (list_date is None or row["trade_date"] >= list_date)
    ]


def ratio_growth(latest: Any, prior: Any) -> float | None:
    if not finite_positive(latest) or not finite_positive(prior):
        return None
    return float(latest) / float(prior) - 1.0


def month_distance(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def prior_observation(
    available: list[dict[str, Any]], months: int
) -> dict[str, Any] | None:
    if not available:
        return None
    latest = available[-1]
    return next(
        (
            row
            for row in reversed(available[:-1])
            if month_distance(row["trade_date"], latest["trade_date"]) >= months
        ),
        None,
    )


def adjusted_subscription_flow(
    latest_size: Any,
    prior_size: Any,
    price_return: float | None,
) -> float | None:
    size_growth = ratio_growth(latest_size, prior_size)
    if size_growth is None or price_return is None or 1.0 + price_return <= 0:
        return None
    return (1.0 + size_growth) / (1.0 + price_return) - 1.0


class ReturnLookup:
    def __init__(self, rows: list[tuple[str, date, float]]) -> None:
        grouped: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for code, trade_date, pct_chg in rows:
            daily_return = float(pct_chg) / 100.0
            if daily_return <= -1.0 or not math.isfinite(daily_return):
                continue
            grouped[str(code)].append((trade_date, daily_return))
        self._dates: dict[str, list[date]] = {}
        self._cumulative_logs: dict[str, list[float]] = {}
        for code, series in grouped.items():
            series.sort()
            dates = []
            cumulative = [0.0]
            for trade_date, daily_return in series:
                dates.append(trade_date)
                cumulative.append(cumulative[-1] + math.log1p(daily_return))
            self._dates[code] = dates
            self._cumulative_logs[code] = cumulative

    def period_return(self, code: str, start: date, end: date) -> float | None:
        dates = self._dates.get(code)
        cumulative = self._cumulative_logs.get(code)
        if not dates or cumulative is None or end <= start:
            return None
        left = bisect.bisect_right(dates, start)
        right = bisect.bisect_right(dates, end)
        if right <= left:
            return None
        return math.expm1(cumulative[right] - cumulative[left])


def build_features(
    code: str,
    snapshot: date,
    list_date: date | None,
    history: list[dict[str, Any]],
    returns: ReturnLookup,
) -> dict[str, float | None]:
    available = observations_available(history, snapshot, list_date)
    output: dict[str, float | None] = {feature: None for feature in FEATURES}
    if not available:
        return output
    latest = available[-1]
    output["etf_share_log_total_wan"] = (
        math.log(float(latest["total_share_wan"]))
        if finite_positive(latest.get("total_share_wan"))
        else None
    )
    output["etf_size_log_total_wan"] = (
        math.log(float(latest["total_size_wan"]))
        if finite_positive(latest.get("total_size_wan"))
        else None
    )
    output["etf_share_observation_age_days"] = float(
        (snapshot - latest["trade_date"]).days
    )
    for quarters in (1, 2, 4):
        prior = prior_observation(available, quarters * 3)
        if prior is None:
            continue
        output[f"etf_share_growth_{quarters}q"] = ratio_growth(
            latest.get("total_share_wan"), prior.get("total_share_wan")
        )
        output[f"etf_size_growth_{quarters}q"] = ratio_growth(
            latest.get("total_size_wan"), prior.get("total_size_wan")
        )
        price_return = returns.period_return(
            code, prior["trade_date"], latest["trade_date"]
        )
        output[f"etf_subscription_flow_{quarters}q"] = adjusted_subscription_flow(
            latest.get("total_size_wan"), prior.get("total_size_wan"), price_return
        )
    return output


def load_inputs(conn, codes: set[str]):
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    list_dates: dict[str, date | None] = {}
    return_rows: list[tuple[str, date, float]] = []
    if not codes:
        return histories, list_dates, ReturnLookup(return_rows)
    for start in range(0, len(codes), 400):
        chunk = sorted(codes)[start : start + 400]
        placeholders = ",".join(["%s"] * len(chunk))
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ts_code, trade_date, available_date, total_share_wan,
                       total_size_wan
                FROM etf_share_size_snapshot
                WHERE ts_code IN ({placeholders})
                ORDER BY ts_code, available_date, trade_date
                """,
                chunk,
            )
            for code, trade_date, available_date, shares, size in cur.fetchall():
                histories[str(code)].append(
                    {
                        "trade_date": trade_date,
                        "available_date": available_date,
                        "total_share_wan": float(shares),
                        "total_size_wan": float(size) if size is not None else None,
                    }
                )
            cur.execute(
                f"SELECT ts_code, list_date FROM passive_etf WHERE ts_code IN ({placeholders})",
                chunk,
            )
            list_dates.update({str(code): listed for code, listed in cur.fetchall()})
            cur.execute(
                f"""
                SELECT ts_code, trade_date, pct_chg
                FROM fund_daily
                WHERE ts_code IN ({placeholders}) AND pct_chg IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            return_rows.extend(
                (str(code), trade_date, float(pct_chg))
                for code, trade_date, pct_chg in cur.fetchall()
            )
    return histories, list_dates, ReturnLookup(return_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    source = args.source if args.source.is_absolute() else ROOT / args.source
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = list(payload["candidate_observations"])
    codes = {str(row["ts_code"]) for row in rows}
    conn = get_connection()
    try:
        histories, list_dates, returns = load_inputs(conn, codes)
    finally:
        conn.close()

    matched = 0
    usable_flow = 0
    for row in rows:
        code = str(row["ts_code"])
        features = build_features(
            code,
            date.fromisoformat(str(row["snapshot"])),
            list_dates.get(code),
            histories.get(code, []),
            returns,
        )
        row.update(features)
        if features["etf_share_log_total_wan"] is not None:
            matched += 1
        if features["etf_subscription_flow_1q"] is not None:
            usable_flow += 1

    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                **{key: value for key, value in payload.items() if key != "candidate_observations"},
                "share_feature_source": "tushare_etf_share_size via etf_share_size_snapshot",
                "share_feature_point_in_time_rule": (
                    "available_date<=snapshot; trade_date>=list_date; "
                    "available_date=trade_date+1 calendar day"
                ),
                "share_features": list(FEATURES),
                "share_feature_match_count": matched,
                "share_feature_match_rate": matched / len(rows) if rows else 0.0,
                "share_flow_1q_usable_count": usable_flow,
                "candidate_observations": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Wrote {output}; candidates={len(rows)} matched={matched} "
        f"flow_1q={usable_flow}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
