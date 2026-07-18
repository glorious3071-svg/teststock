#!/usr/bin/env python3
"""Two-stage walk-forward ETF selection: tail safety, then return rank."""

from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.search_passive_etf_walkforward_ridge import (
    DATASET,
    FEATURE_SETS,
    cross_section_matrix,
    fallback_scores,
    path_summary,
    ranked_target,
)
from backtest.monthly_online_selector import _average_ranks


OUTPUT = ROOT / "data/backtests/passive_etf_two_stage_risk_return_screen_report.json"
TAIL_FEATURES = (
    "volatility_3m",
    "volatility_6m",
    "downside_volatility_3m",
    "historical_cvar_5pct_3m",
    "historical_cvar_5pct_6m",
    "volatility_1m",
    "historical_var_5pct_6m",
    "historical_var_5pct_3m",
    "max_drawdown_6m",
    "maximum_daily_loss_3m",
    "ulcer_index_6m",
    "market_beta_6m",
    "listing_age_years",
    "distance_high_12m",
    "drawdown_6m",
    "drawdown_3m",
)


@dataclass(frozen=True)
class TwoStagePolicy:
    name: str
    risk_history_periods: int
    risk_alpha: float
    safe_fraction: float
    top_n: int
    return_history_periods: int = 24
    return_alpha: float = 0.5


def rank_target(values: list[float]) -> np.ndarray:
    ranks = _average_ranks(values)
    denominator = max(len(values) - 1, 1)
    return np.asarray([2.0 * rank / denominator - 1.0 for rank in ranks], dtype=float)


def fit(
    history: list[dict[str, Any]],
    matrix_key: str,
    target_key: str,
    history_periods: int,
    alpha: float,
) -> np.ndarray | None:
    history = history[-history_periods:]
    if len(history) < min(12, history_periods):
        return None
    xs = []
    ys = []
    for item in history:
        matrix = item[matrix_key]
        target = item[target_key]
        scale = max(len(target), 1) ** -0.5
        xs.append(matrix * scale)
        ys.append(target * scale)
    x = np.vstack(xs)
    y = np.concatenate(ys)
    try:
        with np.errstate(all="ignore"):
            coefficients = np.linalg.solve(
                x.T @ x + np.eye(x.shape[1]) * alpha,
                x.T @ y,
            )
    except np.linalg.LinAlgError:
        return None
    return coefficients if np.all(np.isfinite(coefficients)) else None


def prepare(grouped: list[tuple[date, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    output = []
    for snapshot, rows in grouped:
        output.append(
            {
                "snapshot": snapshot,
                "end_snapshot": date.fromisoformat(str(rows[0]["end_snapshot"])),
                "rows": rows,
                "risk_matrix": cross_section_matrix(rows, TAIL_FEATURES),
                "return_matrix": cross_section_matrix(rows, FEATURE_SETS["price_risk"]),
                "risk_target": rank_target(
                    [float(row["forward_max_drawdown_3m"]) for row in rows]
                ),
                "return_target": ranked_target(rows, 0.0),
            }
        )
    return output


def fallback_risk_scores(item: dict[str, Any]) -> np.ndarray:
    # Signs follow the three-era IC audit: lower volatility/beta/ulcer is safer;
    # less-negative tail loss and drawdown values are safer.
    signs = np.asarray(
        [-1, -1, -1, 1, 1, -1, 1, 1, 1, 1, -1, -1, 1, 1, 1, 1],
        dtype=float,
    )
    return item["risk_matrix"] @ signs / len(signs)


def evaluate(prepared: list[dict[str, Any]], policy: TwoStagePolicy) -> dict[str, Any]:
    predictions = []
    for current in prepared:
        history = [
            item for item in prepared if item["end_snapshot"] <= current["snapshot"]
        ]
        risk_coefficients = fit(
            history,
            "risk_matrix",
            "risk_target",
            policy.risk_history_periods,
            policy.risk_alpha,
        )
        return_coefficients = fit(
            history,
            "return_matrix",
            "return_target",
            policy.return_history_periods,
            policy.return_alpha,
        )
        risk_scores = (
            current["risk_matrix"] @ risk_coefficients
            if risk_coefficients is not None
            else fallback_risk_scores(current)
        )
        return_scores = (
            current["return_matrix"] @ return_coefficients
            if return_coefficients is not None
            else fallback_scores(current["rows"])
        )
        safe_count = max(
            policy.top_n,
            int(math.ceil(len(current["rows"]) * policy.safe_fraction)),
        )
        safe = sorted(
            range(len(current["rows"])),
            key=lambda index: (-float(risk_scores[index]), str(current["rows"][index]["ts_code"])),
        )[:safe_count]
        selected = sorted(
            safe,
            key=lambda index: (-float(return_scores[index]), str(current["rows"][index]["ts_code"])),
        )[: policy.top_n]
        scores = np.asarray([float(return_scores[index]) for index in selected])
        shifted = np.maximum(scores - min(float(np.min(scores)), 0.0) + 0.10, 0.01)
        weights = shifted / shifted.sum()
        predictions.append(
            {
                "snapshot": current["snapshot"].isoformat(),
                "basket_return": sum(
                    float(weight) * float(current["rows"][index]["forward_return_3m"])
                    for weight, index in zip(weights, selected)
                ),
                "basket_average_drawdown": sum(
                    float(weight)
                    * float(current["rows"][index]["forward_max_drawdown_3m"])
                    for weight, index in zip(weights, selected)
                ),
            }
        )
    return {"policy": asdict(policy), "summary": path_summary(predictions)}


def policies() -> list[TwoStagePolicy]:
    output = []
    for history in (24, 60, 120):
        for alpha in (0.5, 2.0, 10.0):
            for safe_fraction in (0.20, 0.40, 0.60, 0.80):
                for top_n in (1, 3):
                    output.append(
                        TwoStagePolicy(
                            f"two_stage_rh{history}_ra{alpha:g}_safe{int(safe_fraction*100)}_top{top_n}",
                            history,
                            alpha,
                            safe_fraction,
                            top_n,
                        )
                    )
    return output


def main() -> int:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    grouped_map: dict[date, list[dict[str, Any]]] = {}
    for row in payload["candidate_observations"]:
        grouped_map.setdefault(date.fromisoformat(str(row["snapshot"])), []).append(row)
    prepared = prepare(sorted(grouped_map.items()))
    results = [evaluate(prepared, policy) for policy in policies()]
    results.sort(
        key=lambda item: (
            item["summary"]["min_capital_factor"],
            item["summary"]["worst_average_constituent_drawdown"],
        ),
        reverse=True,
    )
    OUTPUT.write_text(
        json.dumps(
            {"method": "walk-forward tail-risk filter then walk-forward return rank", "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for item in results[:30]:
        summary = item["summary"]
        print(
            f"{item['policy']['name']:<45} "
            f"min={summary['min_capital_factor']:7.2f}x "
            f"median={summary['median_capital_factor']:7.2f}x "
            f"avg_dd={summary['worst_average_constituent_drawdown']*100:6.2f}%"
        )
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
