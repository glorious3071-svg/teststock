#!/usr/bin/env python3
"""Screen point-in-time ETF eligibility filters before walk-forward ranking."""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.search_passive_etf_walkforward_ridge import (
    DATASET,
    RidgePolicy,
    evaluate,
)


OUTPUT = ROOT / "data/backtests/passive_etf_filtered_universe_screen_report.json"


@dataclass(frozen=True)
class EligibilityFilter:
    name: str
    minimum_liquidity_percentile: float
    minimum_listing_age_years: float
    maximum_volatility_percentile: float
    maximum_beta_percentile: float


MODELS = (
    RidgePolicy("stable_h120_a10_top1_dd2", "stable", 120, 10.0, 1, 2.0),
    RidgePolicy("price_h24_a05_top1_dd2", "price_risk", 24, 0.5, 1, 2.0),
    RidgePolicy("price_h24_a05_top3_dd2", "price_risk", 24, 0.5, 3, 2.0),
)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def apply_filter(
    grouped: list[tuple[date, list[dict]]],
    rule: EligibilityFilter,
) -> list[tuple[date, list[dict]]]:
    output = []
    for snapshot, rows in grouped:
        liquid = [float(row.get("log_amount_1m") or 0.0) for row in rows]
        volatility = [float(row.get("volatility_1m") or 0.0) for row in rows]
        beta = [float(row.get("market_beta_6m") or 1.0) for row in rows]
        liquidity_floor = percentile(liquid, rule.minimum_liquidity_percentile)
        volatility_cap = percentile(volatility, rule.maximum_volatility_percentile)
        beta_cap = percentile(beta, rule.maximum_beta_percentile)
        eligible = [
            row
            for row in rows
            if float(row.get("log_amount_1m") or 0.0) >= liquidity_floor
            and float(row.get("listing_age_years") or 0.0) >= rule.minimum_listing_age_years
            and float(row.get("volatility_1m") or 0.0) <= volatility_cap
            and float(row.get("market_beta_6m") or 1.0) <= beta_cap
        ]
        output.append((snapshot, eligible if len(eligible) >= 5 else rows))
    return output


def rules() -> list[EligibilityFilter]:
    output = []
    for liquidity in (0.0, 0.20, 0.40, 0.60):
        for age in (0.0, 0.5, 1.0, 2.0):
            for volatility in (0.60, 0.80, 1.0):
                for beta in (0.60, 0.80, 1.0):
                    output.append(
                        EligibilityFilter(
                            f"liq{int(liquidity*100)}_age{age:g}_vol{int(volatility*100)}_beta{int(beta*100)}",
                            liquidity,
                            age,
                            volatility,
                            beta,
                        )
                    )
    return output


def main() -> int:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    grouped_map: dict[date, list[dict]] = {}
    for row in payload["candidate_observations"]:
        grouped_map.setdefault(date.fromisoformat(str(row["snapshot"])), []).append(row)
    grouped = sorted(grouped_map.items())
    results = []
    for rule in rules():
        filtered = apply_filter(grouped, rule)
        for model in MODELS:
            result = evaluate(filtered, model)
            result["eligibility_filter"] = asdict(rule)
            results.append(result)
    results.sort(
        key=lambda item: (
            item["summary"]["min_capital_factor"],
            item["summary"]["median_capital_factor"],
            item["summary"]["worst_average_constituent_drawdown"],
        ),
        reverse=True,
    )
    OUTPUT.write_text(
        json.dumps(
            {"method": "point-in-time eligibility filters plus strict walk-forward ridge", "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:30]:
        summary = item["summary"]
        print(
            f"{item['eligibility_filter']['name']:<34} {item['policy']['name']:<30} "
            f"min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"avg_dd={summary['worst_average_constituent_drawdown']*100:6.2f}%"
        )
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
