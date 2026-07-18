"""Walk-forward ridge selection from point-in-time passive ETF snapshots."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from backtest.monthly_online_selector import _average_ranks


DEFAULT_DATASET = (
    Path(__file__).resolve().parents[1]
    / "data/backtests/passive_etf_quarterly_supervised_dataset.json"
)
ENRICHED_V2_DATASET = (
    Path(__file__).resolve().parents[1]
    / "data/backtests/passive_etf_quarterly_enriched_v2_dataset.json"
)
FUNDAMENTAL_V3_DATASET = (
    Path(__file__).resolve().parents[1]
    / "data/backtests/passive_etf_quarterly_fundamental_v3_dataset.json"
)
CONSTITUENT_V4_DATASET = (
    Path(__file__).resolve().parents[1]
    / "data/backtests/passive_etf_quarterly_constituent_v4_dataset.json"
)
SHARE_V5_DATASET = (
    Path(__file__).resolve().parents[1]
    / "data/backtests/passive_etf_quarterly_share_v5_dataset.json"
)

STABLE_FEATURES = (
    "market_beta_6m",
    "volatility_1m",
    "distance_high_12m",
    "log_amount_1m",
    "volatility_3m",
    "volatility_6m",
    "max_drawdown_6m",
    "downside_volatility_3m",
    "momentum_12m_skip1m",
    "momentum_12m",
)

PRICE_RISK_FEATURES = (
    "momentum_1m",
    "momentum_3m",
    "momentum_6m",
    "momentum_12m",
    "momentum_12m_skip1m",
    "relative_strength_3m",
    "relative_strength_6m",
    "residual_momentum_6m",
    "distance_high_12m",
    "volatility_1m",
    "volatility_3m",
    "volatility_6m",
    "downside_volatility_3m",
    "drawdown_3m",
    "drawdown_6m",
    "max_drawdown_6m",
    "positive_day_ratio_3m",
    "market_beta_6m",
    "market_correlation_6m",
)


@dataclass(frozen=True)
class SupervisedEtfPolicy:
    name: str
    features: tuple[str, ...]
    history_periods: int
    ridge_alpha: float
    top_n: int
    drawdown_penalty: float
    minimum_listing_age_years: float = 0.0


SUPERVISED_ETF_POLICIES = (
    SupervisedEtfPolicy(
        "direct_supervised_stable_h120_a10_top1_dd2",
        STABLE_FEATURES,
        120,
        10.0,
        1,
        2.0,
    ),
    SupervisedEtfPolicy(
        "direct_supervised_price_h24_a05_top1_dd2",
        PRICE_RISK_FEATURES,
        24,
        0.5,
        1,
        2.0,
    ),
    SupervisedEtfPolicy(
        "direct_supervised_price_h24_a05_top3_dd2",
        PRICE_RISK_FEATURES,
        24,
        0.5,
        3,
        2.0,
    ),
    SupervisedEtfPolicy(
        "direct_supervised_price_h24_a05_top1_dd2_age05",
        PRICE_RISK_FEATURES,
        24,
        0.5,
        1,
        2.0,
        0.5,
    ),
)

_DATASET_CACHE: dict[Path, list[dict[str, Any]]] = {}
_SELECTION_CACHE: dict[tuple[str, date, int], dict[str, float]] = {}


def load_candidate_observations(path: Path = DEFAULT_DATASET) -> list[dict[str, Any]]:
    resolved = path.resolve()
    if resolved not in _DATASET_CACHE:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        _DATASET_CACHE[resolved] = list(payload["candidate_observations"])
    return _DATASET_CACHE[resolved]


def _percentile_column(values: Sequence[Any]) -> np.ndarray:
    usable = [
        (index, float(value))
        for index, value in enumerate(values)
        if value is not None and np.isfinite(float(value))
    ]
    output = np.zeros(len(values), dtype=float)
    if len(usable) <= 1:
        return output
    ranks = _average_ranks([value for _index, value in usable])
    denominator = len(usable) - 1
    for (index, _value), rank in zip(usable, ranks):
        output[index] = 2.0 * rank / denominator - 1.0
    return output


def _matrix(rows: Sequence[dict[str, Any]], features: Sequence[str]) -> np.ndarray:
    return np.column_stack(
        [_percentile_column([row.get(feature) for row in rows]) for feature in features]
    )


def _target(rows: Sequence[dict[str, Any]], drawdown_penalty: float) -> np.ndarray:
    utility = [
        float(row["forward_return_3m"])
        + drawdown_penalty * float(row["forward_max_drawdown_3m"])
        for row in rows
    ]
    return _percentile_column(utility)


def select_supervised_etfs(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    policy: SupervisedEtfPolicy,
) -> dict[str, float]:
    """Select using only labels whose three-month window has already ended."""

    cache_key = (policy.name, snapshot, id(observations))
    cached = _SELECTION_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        grouped[date.fromisoformat(str(row["snapshot"]))].append(row)
    if policy.minimum_listing_age_years > 0:
        grouped = {
            day: (
                eligible
                if len(eligible := [
                    row
                    for row in rows
                    if float(row.get("listing_age_years") or 0.0)
                    >= policy.minimum_listing_age_years
                ])
                >= 5
                else rows
            )
            for day, rows in grouped.items()
        }
    current = grouped.get(snapshot, [])
    known_dates = sorted(
        day
        for day, rows in grouped.items()
        if date.fromisoformat(str(rows[0]["end_snapshot"])) <= snapshot
    )[-policy.history_periods :]
    if not current or len(known_dates) < min(12, policy.history_periods):
        _SELECTION_CACHE[cache_key] = {}
        return {}
    xs = []
    ys = []
    for day in known_dates:
        rows = grouped[day]
        matrix = _matrix(rows, policy.features)
        scale = max(len(rows), 1) ** -0.5
        xs.append(matrix * scale)
        ys.append(_target(rows, policy.drawdown_penalty) * scale)
    x = np.vstack(xs)
    y = np.concatenate(ys)
    try:
        with np.errstate(all="ignore"):
            gram = x.T @ x + np.eye(x.shape[1]) * policy.ridge_alpha
            coefficients = np.linalg.solve(gram, x.T @ y)
            scores = _matrix(current, policy.features) @ coefficients
    except np.linalg.LinAlgError:
        return {}
    if not np.all(np.isfinite(coefficients)) or not np.all(np.isfinite(scores)):
        return {}
    order = sorted(
        range(len(current)),
        key=lambda index: (-float(scores[index]), str(current[index]["ts_code"])),
    )[: policy.top_n]
    selected_scores = np.asarray([float(scores[index]) for index in order])
    shifted = selected_scores - min(float(np.min(selected_scores)), 0.0) + 0.10
    shifted = np.maximum(shifted, 0.01)
    total = float(shifted.sum())
    weights = {
        str(current[index]["ts_code"]): float(weight) / total
        for index, weight in zip(order, shifted)
    }
    _SELECTION_CACHE[cache_key] = dict(weights)
    return weights


def select_static_stable_combo(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Select the lowest-beta, lowest-volatility, lowest-autocorrelation ETF."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def low_rank(feature: str) -> dict[str, float]:
        ordered = sorted(
            (
                (str(row["ts_code"]), float(row.get(feature) or 0.0))
                for row in current
            ),
            key=lambda item: (item[1], item[0]),
        )
        denominator = max(len(ordered) - 1, 1)
        return {
            code: 1.0 - index / denominator
            for index, (code, _value) in enumerate(ordered)
        }

    beta = low_rank("market_beta_6m")
    volatility = low_rank("volatility_3m")
    autocorrelation = low_rank("return_autocorrelation_3m")
    code = max(
        beta,
        key=lambda item: (
            beta[item] + volatility[item] + autocorrelation[item],
            item,
        ),
    )
    return {code: 1.0}


def select_static_stable_combo_top3(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Diversified stable rank blend selected by the cross-era screen."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        ordered = sorted(
            (
                (str(row["ts_code"]), float(row.get(feature) or 0.0))
                for row in current
            ),
            key=lambda item: (item[1], item[0]),
        )
        denominator = max(len(ordered) - 1, 1)
        raw = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
        return raw if higher_is_better else {code: 1.0 - value for code, value in raw.items()}

    ranks = (
        rank("market_beta_6m", False),
        rank("distance_high_12m", True),
        rank("return_autocorrelation_3m", False),
        rank("volatility_3m", False),
    )
    scores = {
        str(row["ts_code"]): sum(values[str(row["ts_code"])] for values in ranks) / len(ranks)
        for row in current
    }
    selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[:3]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()}


def select_weighted_stable_combo_top3(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Top3 weighted stable scorecard from the cross-era grid search."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        ordered = sorted(
            (
                (str(row["ts_code"]), float(row.get(feature) or 0.0))
                for row in current
            ),
            key=lambda item: (item[1], item[0]),
        )
        denominator = max(len(ordered) - 1, 1)
        raw = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
        return raw if higher_is_better else {code: 1.0 - value for code, value in raw.items()}

    components = (
        (rank("market_beta_6m", False), 1.0 / 9.0),
        (rank("distance_high_12m", True), 2.0 / 9.0),
        (rank("return_autocorrelation_3m", False), 2.0 / 9.0),
        (rank("volatility_3m", False), 4.0 / 9.0),
    )
    scores = {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }
    selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[:3]
    powered = {code: max(scores[code], 0.01) ** 4.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()}


def select_weighted_stable_combo_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Top1 weighted stable scorecard from the cross-era grid search."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        ordered = sorted(
            (
                (str(row["ts_code"]), float(row.get(feature) or 0.0))
                for row in current
            ),
            key=lambda item: (item[1], item[0]),
        )
        denominator = max(len(ordered) - 1, 1)
        raw = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
        return raw if higher_is_better else {code: 1.0 - value for code, value in raw.items()}

    components = (
        (rank("market_beta_6m", False), 4.0 / 13.0),
        (rank("distance_high_12m", True), 1.0 / 13.0),
        (rank("return_autocorrelation_3m", False), 4.0 / 13.0),
        (rank("volatility_3m", False), 4.0 / 13.0),
    )
    scores = {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }
    selected = max(scores, key=lambda code: (scores[code], code))
    return {selected: 1.0}


def weighted_stable_combo_v2_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Return point-in-time v2 scores without reading forward labels."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        usable = sorted(
            (
                (str(row["ts_code"]), float(row[feature]))
                for row in current
                if row.get(feature) is not None
                and np.isfinite(float(row[feature]))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if len(usable) <= 1:
            return {str(row["ts_code"]): 0.5 for row in current}
        denominator = len(usable) - 1
        raw = {code: index / denominator for index, (code, _value) in enumerate(usable)}
        return {
            str(row["ts_code"]): (
                raw.get(str(row["ts_code"]), 0.5)
                if higher_is_better
                else 1.0 - raw.get(str(row["ts_code"]), 0.5)
            )
            for row in current
        }

    # Grid ratios 2, 0.5, 2, 2, 0.25 reduce to integer weights 8:2:8:8:1.
    components = (
        (rank("market_beta_6m", False), 8.0 / 27.0),
        (rank("distance_high_12m", True), 2.0 / 27.0),
        (rank("return_autocorrelation_3m", False), 8.0 / 27.0),
        (rank("volatility_3m", False), 8.0 / 27.0),
        (rank("ulcer_index_6m", False), 1.0 / 27.0),
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def select_weighted_stable_combo_v2_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Tail-risk-enriched Top1 scorecard from the three-era stability screen."""

    scores = weighted_stable_combo_v2_scores(observations, snapshot)
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (scores[code], code))
    return {selected: 1.0}


def weighted_stable_combo_v3_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Return point-in-time v3 scores without reading forward labels."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        usable = sorted(
            (
                (str(row["ts_code"]), float(row[feature]))
                for row in current
                if row.get(feature) is not None
                and np.isfinite(float(row[feature]))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if len(usable) <= 1:
            return {str(row["ts_code"]): 0.5 for row in current}
        denominator = len(usable) - 1
        raw = {code: index / denominator for index, (code, _value) in enumerate(usable)}
        return {
            str(row["ts_code"]): (
                raw.get(str(row["ts_code"]), 0.5)
                if higher_is_better
                else 1.0 - raw.get(str(row["ts_code"]), 0.5)
            )
            for row in current
        }

    # Stable-grid ratio 4:1:4:4:1:1.  The profitability proxy is deliberately
    # limited to 1/15 so missing early index fundamentals remain neutral.
    components = (
        (rank("market_beta_6m", False), 4.0 / 15.0),
        (rank("distance_high_12m", True), 1.0 / 15.0),
        (rank("return_autocorrelation_3m", False), 4.0 / 15.0),
        (rank("volatility_3m", False), 4.0 / 15.0),
        (rank("ulcer_index_6m", False), 1.0 / 15.0),
        (rank("index_fundamental_roe_proxy", False), 1.0 / 15.0),
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def select_weighted_stable_combo_v3_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Top1 scorecard with a small index profitability-expectations term."""

    scores = weighted_stable_combo_v3_scores(observations, snapshot)
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (scores[code], code))
    return {selected: 1.0}


def weighted_stable_combo_v4_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """V3 plus a small, point-in-time 12-month book-growth reversal term."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        usable = sorted(
            (
                (str(row["ts_code"]), float(row[feature]))
                for row in current
                if row.get(feature) is not None
                and np.isfinite(float(row[feature]))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if len(usable) <= 1:
            return {str(row["ts_code"]): 0.5 for row in current}
        denominator = len(usable) - 1
        raw = {code: index / denominator for index, (code, _value) in enumerate(usable)}
        return {
            str(row["ts_code"]): (
                raw.get(str(row["ts_code"]), 0.5)
                if higher_is_better
                else 1.0 - raw.get(str(row["ts_code"]), 0.5)
            )
            for row in current
        }

    components = (
        (rank("market_beta_6m", False), 4.0 / 16.0),
        (rank("distance_high_12m", True), 1.0 / 16.0),
        (rank("return_autocorrelation_3m", False), 4.0 / 16.0),
        (rank("volatility_3m", False), 4.0 / 16.0),
        (rank("ulcer_index_6m", False), 1.0 / 16.0),
        (rank("index_fundamental_roe_proxy", False), 1.0 / 16.0),
        (rank("index_fundamental_book_growth_12m", False), 1.0 / 16.0),
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def select_weighted_stable_combo_v4_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_stable_combo_v4_scores(observations, snapshot)
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (scores[code], code))
    return {selected: 1.0}


def weighted_stable_combo_v5_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """V4 plus small constituent earnings-yield and concentration reversals."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        usable = sorted(
            (
                (str(row["ts_code"]), float(row[feature]))
                for row in current
                if row.get(feature) is not None
                and np.isfinite(float(row[feature]))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if len(usable) <= 1:
            return {str(row["ts_code"]): 0.5 for row in current}
        denominator = len(usable) - 1
        raw = {code: index / denominator for index, (code, _value) in enumerate(usable)}
        return {
            str(row["ts_code"]): (
                raw.get(str(row["ts_code"]), 0.5)
                if higher_is_better
                else 1.0 - raw.get(str(row["ts_code"]), 0.5)
            )
            for row in current
        }

    raw_components = (
        ("market_beta_6m", False, 4.0),
        ("distance_high_12m", True, 1.0),
        ("return_autocorrelation_3m", False, 4.0),
        ("volatility_3m", False, 4.0),
        ("ulcer_index_6m", False, 1.0),
        ("index_fundamental_roe_proxy", False, 1.0),
        ("index_fundamental_book_growth_12m", False, 1.0),
        ("index_constituent_earnings_yield", False, 1.0),
        ("index_constituent_weight_hhi", False, 1.0),
    )
    total_weight = sum(item[2] for item in raw_components)
    components = tuple(
        (rank(feature, direction), weight / total_weight)
        for feature, direction, weight in raw_components
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def select_weighted_stable_combo_v5_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_stable_combo_v5_scores(observations, snapshot)
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (scores[code], code))
    return {selected: 1.0}


def weighted_stable_combo_v9_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """V5 with the index ROE-reversal term reduced from 1.0 to 0.5.

    The one-dimensional ablation improves the fixed-rule return in all three
    historical eras.  All other V5 components and their point-in-time missing
    value treatment remain unchanged.
    """

    base = weighted_stable_combo_v5_scores(observations, snapshot)
    if not base:
        return {}
    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    usable = sorted(
        (
            (str(row["ts_code"]), float(row["index_fundamental_roe_proxy"]))
            for row in current
            if row.get("index_fundamental_roe_proxy") is not None
            and np.isfinite(float(row["index_fundamental_roe_proxy"]))
        ),
        key=lambda item: (item[1], item[0]),
    )
    if len(usable) <= 1:
        roe_reversal = {code: 0.5 for code in base}
    else:
        denominator = len(usable) - 1
        known = {
            code: 1.0 - index / denominator
            for index, (code, _value) in enumerate(usable)
        }
        roe_reversal = {code: known.get(code, 0.5) for code in base}
    return {
        code: (18.0 * base[code] - 0.5 * roe_reversal[code]) / 17.5
        for code in base
    }


def select_weighted_stable_combo_v9_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_stable_combo_v9_scores(observations, snapshot)
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (round(scores[code], 12), code))
    return {selected: 1.0}


def weighted_stable_combo_v10_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    roe_weight: float,
) -> dict[str, float]:
    """V5 risk-weight rebalance: beta 0.5, volatility 5, ROE 1 or 0.75."""

    if roe_weight not in (0.75, 1.0):
        raise ValueError("roe_weight must be 0.75 or 1.0")
    base = weighted_stable_combo_v5_scores(observations, snapshot)
    if not base:
        return {}
    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]

    def low_rank(feature: str) -> dict[str, float]:
        usable = sorted(
            (
                (str(row["ts_code"]), float(row[feature]))
                for row in current
                if row.get(feature) is not None
                and np.isfinite(float(row[feature]))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if len(usable) <= 1:
            return {code: 0.5 for code in base}
        denominator = len(usable) - 1
        known = {
            code: 1.0 - index / denominator
            for index, (code, _value) in enumerate(usable)
        }
        return {code: known.get(code, 0.5) for code in base}

    beta = low_rank("market_beta_6m")
    volatility = low_rank("volatility_3m")
    roe = low_rank("index_fundamental_roe_proxy")
    total_weight = 18.0 - 3.5 + 1.0 - (1.0 - roe_weight)
    return {
        code: (
            18.0 * base[code]
            - 3.5 * beta[code]
            + volatility[code]
            - (1.0 - roe_weight) * roe[code]
        )
        / total_weight
        for code in base
    }


def select_weighted_stable_combo_v10_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    roe_weight: float,
) -> dict[str, float]:
    scores = weighted_stable_combo_v10_scores(
        observations, snapshot, roe_weight
    )
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (round(scores[code], 12), code))
    return {selected: 1.0}


def weighted_stable_combo_v7_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    flow_weight: float,
) -> dict[str, float]:
    """V5 plus a tiny contrarian rank for point-in-time two-quarter ETF flow.

    V5's raw component weight is 18.  ``flow_weight`` deliberately stays much
    smaller because the share-flow IC is stable but weak.  Missing pre-2009
    exchange share observations are neutral and never backfilled.
    """

    base = weighted_stable_combo_v5_scores(observations, snapshot)
    if not base:
        return {}
    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    usable = sorted(
        (
            (str(row["ts_code"]), float(row["etf_subscription_flow_2q"]))
            for row in current
            if row.get("etf_subscription_flow_2q") is not None
            and np.isfinite(float(row["etf_subscription_flow_2q"]))
        ),
        key=lambda item: (item[1], item[0]),
    )
    if len(usable) <= 1:
        contrarian = {code: 0.5 for code in base}
    else:
        denominator = len(usable) - 1
        known = {
            code: 1.0 - index / denominator
            for index, (code, _value) in enumerate(usable)
        }
        contrarian = {code: known.get(code, 0.5) for code in base}
    return {
        code: (18.0 * base[code] + flow_weight * contrarian[code])
        / (18.0 + flow_weight)
        for code in base
    }


def select_weighted_stable_combo_v7_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    flow_weight: float,
) -> dict[str, float]:
    scores = weighted_stable_combo_v7_scores(observations, snapshot, flow_weight)
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (scores[code], code))
    return {selected: 1.0}


def weighted_stable_combo_v6_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Low-parameter four-feature score stable across all 12 start phases."""

    current = [
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    ]
    if not current:
        return {}

    def rank(feature: str, higher_is_better: bool) -> dict[str, float]:
        usable = sorted(
            (
                (str(row["ts_code"]), float(row[feature]))
                for row in current
                if row.get(feature) is not None
                and np.isfinite(float(row[feature]))
            ),
            key=lambda item: (item[1], item[0]),
        )
        if len(usable) <= 1:
            return {str(row["ts_code"]): 0.5 for row in current}
        denominator = len(usable) - 1
        raw = {code: index / denominator for index, (code, _value) in enumerate(usable)}
        return {
            str(row["ts_code"]): (
                raw.get(str(row["ts_code"]), 0.5)
                if higher_is_better
                else 1.0 - raw.get(str(row["ts_code"]), 0.5)
            )
            for row in current
        }

    components = (
        rank("distance_high_12m", True),
        rank("momentum_12m", False),
        rank("index_fundamental_roe_proxy", False),
        rank("index_fundamental_pb_change_6m", True),
    )
    return {
        str(row["ts_code"]): statistics.mean(
            values[str(row["ts_code"])] for values in components
        )
        for row in current
    }


def select_weighted_stable_combo_v6_top1(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_stable_combo_v6_scores(observations, snapshot)
    if not scores:
        return {}
    selected = max(scores, key=lambda code: (scores[code], code))
    return {selected: 1.0}


def policy_by_name(name: str) -> SupervisedEtfPolicy:
    for policy in SUPERVISED_ETF_POLICIES:
        if policy.name == name:
            return policy
    raise KeyError(name)
