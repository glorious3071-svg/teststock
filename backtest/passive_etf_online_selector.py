"""Point-in-time online IC weights for domestic passive ETF selection."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Sequence


DEFAULT_HISTORY_PATH = (
    Path(__file__).resolve().parents[1]
    / "data/backtests/passive_etf_quarterly_feature_ic_regime_report.json"
)

ONLINE_ETF_FEATURES = (
    "market_beta_6m",
    "volatility_1m",
    "distance_high_12m",
    "volatility_3m",
    "max_drawdown_6m",
    "momentum_12m_skip1m",
    "momentum_12m",
    "drawdown_3m",
    "positive_day_ratio_3m",
    "momentum_1m",
    "residual_momentum_6m",
    "relative_strength_3m",
    "momentum_3m",
    "drawdown_6m",
    "market_correlation_6m",
)

_OBSERVATIONS: list[dict[str, Any]] | None = None


def load_observations(path: Path = DEFAULT_HISTORY_PATH) -> list[dict[str, Any]]:
    global _OBSERVATIONS
    if _OBSERVATIONS is None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        _OBSERVATIONS = list(payload["observations"])
    return _OBSERVATIONS


def learned_online_feature_weights(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    regime: str,
    *,
    history_periods: int = 60,
    min_history_periods: int = 20,
    min_abs_median_ic: float = 0.03,
    min_direction_consistency: float = 0.55,
) -> dict[str, dict[str, float]]:
    """Learn weights only from labels whose holding windows have ended."""

    grouped: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in observations:
        if row.get("market_regime") != regime:
            continue
        end_snapshot = date.fromisoformat(str(row["end_snapshot"]))
        if end_snapshot > snapshot:
            continue
        feature = str(row["feature"])
        if feature not in ONLINE_ETF_FEATURES:
            continue
        grouped[feature].append((end_snapshot, float(row["ic"])))

    learned = {}
    for feature, dated_values in grouped.items():
        values = [
            value
            for _end, value in sorted(dated_values, key=lambda item: item[0])[-history_periods:]
        ]
        if len(values) < min_history_periods:
            continue
        median_ic = statistics.median(values)
        if abs(median_ic) < min_abs_median_ic:
            continue
        orientation = 1.0 if median_ic > 0 else -1.0
        consistency = sum(value * orientation > 0 for value in values) / len(values)
        if consistency < min_direction_consistency:
            continue
        reliability = abs(median_ic) * max(2.0 * consistency - 1.0, 0.0)
        if reliability <= 0:
            continue
        learned[feature] = {
            "orientation": orientation,
            "median_ic": median_ic,
            "consistency": consistency,
            "weight": reliability,
            "history_count": float(len(values)),
        }
    return learned
