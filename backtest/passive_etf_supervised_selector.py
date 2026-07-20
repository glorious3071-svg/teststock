"""Walk-forward ridge selection from point-in-time passive ETF snapshots."""

from __future__ import annotations

import json
import pickle
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

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
_CURRENT_OBSERVATIONS_CACHE: dict[
    tuple[int, int, int | None, int | None, date], tuple[dict[str, Any], ...]
] = {}
_PERSISTENT_CACHE_VERSION = 1


def _persistent_dataset_cache_path(path: Path) -> Path | None:
    resolved = path.resolve()
    known = {
        DEFAULT_DATASET.resolve(),
        ENRICHED_V2_DATASET.resolve(),
        FUNDAMENTAL_V3_DATASET.resolve(),
        CONSTITUENT_V4_DATASET.resolve(),
        SHARE_V5_DATASET.resolve(),
    }
    if resolved not in known:
        return None
    return (
        resolved.parent
        / "cache"
        / "passive_etf_candidate_observations"
        / f"{resolved.stem}.pkl"
    )


def _read_persistent_dataset_cache(path: Path, cache_path: Path) -> list[dict[str, Any]] | None:
    try:
        source_stat = path.stat()
        with cache_path.open("rb") as fh:
            payload = pickle.load(fh)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("cache_version") != _PERSISTENT_CACHE_VERSION:
        return None
    if payload.get("source_mtime_ns") != source_stat.st_mtime_ns:
        return None
    if payload.get("source_size") != source_stat.st_size:
        return None
    observations = payload.get("candidate_observations")
    return list(observations) if isinstance(observations, list) else None


def _write_persistent_dataset_cache(
    path: Path,
    cache_path: Path,
    observations: list[dict[str, Any]],
) -> None:
    try:
        source_stat = path.stat()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("wb") as fh:
            pickle.dump(
                {
                    "cache_version": _PERSISTENT_CACHE_VERSION,
                    "source_mtime_ns": source_stat.st_mtime_ns,
                    "source_size": source_stat.st_size,
                    "candidate_observations": observations,
                },
                fh,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
    except Exception:
        return


def load_candidate_observations(path: Path = DEFAULT_DATASET) -> list[dict[str, Any]]:
    resolved = path.resolve()
    if resolved not in _DATASET_CACHE:
        persistent_cache_path = _persistent_dataset_cache_path(resolved)
        cached = (
            _read_persistent_dataset_cache(resolved, persistent_cache_path)
            if persistent_cache_path is not None
            else None
        )
        if cached is None:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            cached = list(payload["candidate_observations"])
            if persistent_cache_path is not None:
                _write_persistent_dataset_cache(resolved, persistent_cache_path, cached)
        _DATASET_CACHE[resolved] = cached
    return _DATASET_CACHE[resolved]


def _current_observations(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> tuple[dict[str, Any], ...]:
    first_id = id(observations[0]) if observations else None
    last_id = id(observations[-1]) if observations else None
    cache_key = (id(observations), len(observations), first_id, last_id, snapshot)
    cached = _CURRENT_OBSERVATIONS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    current = tuple(
        row
        for row in observations
        if date.fromisoformat(str(row["snapshot"])) == snapshot
    )
    _CURRENT_OBSERVATIONS_CACHE[cache_key] = current
    current_first_id = id(current[0]) if current else None
    current_last_id = id(current[-1]) if current else None
    _CURRENT_OBSERVATIONS_CACHE[
        (id(current), len(current), current_first_id, current_last_id, snapshot)
    ] = current
    return current


def current_candidate_observations(
    path: Path,
    snapshot: date,
) -> tuple[dict[str, Any], ...]:
    return _current_observations(load_candidate_observations(path), snapshot)


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

    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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
    current = _current_observations(observations, snapshot)
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
    current = _current_observations(observations, snapshot)

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


def weighted_structural_mainline_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Point-in-time score for local ETF leadership in structural markets.

    The score deliberately uses only snapshot-known ETF and tracked-index
    fields: recent relative strength, ETF cross-section participation, trading
    activity/share-flow changes, valuation and earnings repair, policy score,
    lower benchmark correlation, and explicit crowding penalties.
    """

    current = _current_observations(observations, snapshot)
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
        ("relative_strength_3m", True, 2.4),
        ("relative_strength_6m", True, 2.0),
        ("momentum_3m", True, 1.6),
        ("momentum_6m", True, 1.2),
        ("positive_day_ratio_3m", True, 1.0),
        ("index_trend_acceleration_geometric_3m_vs_6m", True, 1.0),
        ("market_correlation_6m", False, 0.9),
        ("log_amount_1m", True, 0.6),
        ("amount_acceleration_1m_6m", True, 0.6),
        ("etf_share_growth_1q", True, 0.5),
        ("etf_subscription_flow_1q", True, 0.4),
        ("index_fundamental_earnings_growth_3m", True, 0.6),
        ("index_fundamental_roe_proxy", True, 0.4),
        ("index_pe_ttm_history_percentile_3y", False, 0.5),
        ("index_policy_score", True, 0.4),
        ("distance_high_12m", True, 0.5),
        ("amount_crowding_percentile_3y", False, 0.8),
        ("negative_day_ratio_3m", False, 0.5),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (rank(feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def weighted_structural_liquidity_flow_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Point-in-time structural score tilted toward ETF liquidity and share flow."""

    current = _current_observations(observations, snapshot)
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
        ("relative_strength_3m", True, 2.2),
        ("relative_strength_6m", True, 1.8),
        ("momentum_3m", True, 1.4),
        ("positive_day_ratio_3m", True, 1.2),
        ("log_amount_1m", True, 1.2),
        ("amount_acceleration_1m_6m", True, 1.4),
        ("etf_share_growth_1q", True, 1.0),
        ("etf_subscription_flow_1q", True, 0.9),
        ("index_etf_positive_turnover_pressure_1m", True, 0.8),
        ("market_correlation_6m", False, 0.8),
        ("amount_crowding_percentile_3y", False, 0.8),
        ("negative_day_ratio_3m", False, 0.7),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (rank(feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def weighted_structural_momentum_breadth_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Point-in-time structural score tilted toward short-cycle mainline breadth."""

    current = _current_observations(observations, snapshot)
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
        ("relative_strength_3m", True, 3.2),
        ("relative_strength_6m", True, 2.6),
        ("momentum_3m", True, 2.2),
        ("momentum_6m", True, 1.8),
        ("positive_day_ratio_3m", True, 1.4),
        ("index_trend_acceleration_geometric_3m_vs_6m", True, 1.2),
        ("market_correlation_6m", False, 1.2),
        ("residual_momentum_6m", True, 1.0),
        ("log_amount_1m", True, 0.7),
        ("amount_acceleration_1m_6m", True, 0.8),
        ("etf_share_growth_1q", True, 0.5),
        ("index_policy_score", True, 0.4),
        ("drawdown_3m", True, 0.6),
        ("amount_crowding_percentile_3y", False, 0.5),
        ("negative_day_ratio_3m", False, 0.7),
        ("historical_cvar_5pct_3m", True, 0.4),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (rank(feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def weighted_structural_resilience_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Point-in-time structural score for post-shock resilient local leaders."""

    current = _current_observations(observations, snapshot)
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
        ("drawdown_6m", True, 2.4),
        ("historical_cvar_5pct_3m", True, 2.0),
        ("maximum_daily_loss_3m", True, 1.8),
        ("relative_strength_3m", True, 1.8),
        ("relative_strength_6m", True, 1.2),
        ("positive_day_ratio_3m", True, 1.3),
        ("market_correlation_6m", False, 1.2),
        ("log_amount_1m", True, 0.8),
        ("amount_acceleration_1m_6m", True, 0.8),
        ("etf_share_growth_1q", True, 0.6),
        ("amount_crowding_percentile_3y", False, 1.0),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (rank(feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def structural_theme_group_for_text(text: str) -> str:
    """Map ETF/index names to coarse domestic theme buckets.

    This is intentionally static text metadata rather than realized-return
    clustering, so it can be used at any historical snapshot without leaking
    future cross-sectional performance.
    """

    normalized = text.lower()
    groups = (
        ("healthcare", ("医药", "医疗", "生物", "创新药", "疫苗", "中药", "养老")),
        ("consumer", ("消费", "食品", "饮料", "酒", "家电", "农业", "养殖", "旅游", "传媒")),
        ("finance", ("银行", "证券", "保险", "金融", "地产", "红利")),
        ("technology", ("科技", "半导体", "芯片", "电子", "通信", "计算机", "人工智能", "软件", "云", "互联网", "5g")),
        ("new_energy", ("新能源", "光伏", "电池", "锂", "储能", "电力设备", "智能汽车", "汽车")),
        ("resources", ("有色", "煤炭", "钢铁", "能源", "石油", "化工", "材料", "稀土", "黄金")),
        ("industrial", ("军工", "机械", "基建", "建材", "工业", "高端制造", "央企", "国企")),
        ("broad_growth", ("创业板", "科创", "成长", "500", "1000", "双创", "中小")),
        ("broad_value", ("300", "50", "180", "价值", "低波", "基本面")),
    )
    for group, keywords in groups:
        if any(keyword in normalized for keyword in keywords):
            return group
    return "other"


def structural_subtheme_group_for_text(text: str) -> str:
    """Map ETF/index names to narrower static buckets for diagnostics."""

    normalized = text.lower()
    groups = (
        ("utilities", ("电力", "绿电", "公用事业", "公共事业")),
        ("communication", ("通信", "5g")),
        ("digital_hot", ("人工智能", "软件", "计算机", "云", "互联网", "信息安全", "信息技术", "tmt", "线上消费", "在线消费", "金融科技")),
        ("semiconductor", ("半导体", "芯片", "电子")),
        ("small_growth", ("创业板", "成长", "中小", "国证2000", "中证500", "500", "1000", "双创")),
        ("healthcare", ("医药", "医疗", "生物", "创新药", "疫苗", "中药", "养老")),
        ("consumer", ("消费", "食品", "饮料", "酒", "家电", "农业", "养殖", "旅游", "传媒")),
        ("finance", ("银行", "证券", "保险", "金融", "地产", "红利")),
        ("new_energy", ("新能源", "光伏", "电池", "锂", "储能", "电力设备", "智能汽车", "汽车")),
        ("resources", ("有色", "煤炭", "钢铁", "能源", "石油", "化工", "材料", "稀土", "黄金", "大宗商品")),
        ("industrial", ("军工", "机械", "基建", "建材", "工业", "高端制造", "央企", "国企")),
    )
    for group, keywords in groups:
        if any(keyword in normalized for keyword in keywords):
            return group
    return "other"


def structural_finance_substyle_for_text(text: str) -> str:
    """Map finance ETF/index names to static substyles.

    The ordering is intentional: mixed names such as ``证券保险红利`` should stay
    with the broker/insurance risk bucket instead of being treated as
    bank/dividend defensiveness.
    """

    normalized = text.lower()
    if any(keyword in normalized for keyword in ("证券", "券商", "保险")):
        return "broker_insurance"
    if any(keyword in normalized for keyword in ("银行", "红利", "股息", "低波红利")):
        return "bank_dividend"
    if any(keyword in normalized for keyword in ("地产", "房地产")):
        return "real_estate"
    if "金融" in normalized:
        return "broad_finance"
    return "other"


def structural_resource_bank_catchup_style_for_text(text: str) -> str:
    """Map names to resources or pure-bank catch-up styles."""

    normalized = text.lower()
    if any(
        keyword in normalized
        for keyword in ("新能源", "光伏", "电池", "证券", "券商", "保险", "非银行")
    ):
        return "other"
    if any(
        keyword in normalized
        for keyword in ("有色", "煤炭", "钢铁", "能源", "石油", "化工", "材料", "稀土")
    ):
        return "resources"
    if "银行" in normalized:
        return "bank"
    return "other"


def _finite_observation_value(row: Mapping[str, Any], feature: str) -> float | None:
    value = row.get(feature)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _rank_current_observations(
    current: Sequence[dict[str, Any]],
    feature: str,
    higher_is_better: bool,
) -> dict[str, float]:
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


def _percentile_scores(
    values: Mapping[str, float],
    *,
    higher_is_better: bool = True,
) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = max(len(ordered) - 1, 1)
    ranks = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
    if higher_is_better:
        return ranks
    return {code: 1.0 - rank for code, rank in ranks.items()}


def _structural_group_metrics(
    current: Sequence[dict[str, Any]],
    groups_by_code: Mapping[str, str],
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        code = str(row["ts_code"])
        grouped[str(groups_by_code.get(code, "other"))].append(row)

    metrics: dict[str, dict[str, float]] = {}
    features = (
        "momentum_1m",
        "momentum_3m",
        "momentum_6m",
        "relative_strength_3m",
        "relative_strength_6m",
        "drawdown_3m",
        "market_correlation_6m",
        "etf_share_growth_1q",
        "amount_acceleration_1m_6m",
        "amount_crowding_percentile_3y",
    )
    for group, rows in grouped.items():
        group_metrics: dict[str, float] = {"n": float(len(rows))}
        for feature in features:
            values = [
                value
                for row in rows
                if (value := _finite_observation_value(row, feature)) is not None
            ]
            group_metrics[feature] = statistics.mean(values) if values else 0.0
        momentum_3m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_3m")) is not None
        ]
        group_metrics["breadth_3m_positive"] = (
            sum(1 for value in momentum_3m if value > 0.0) / len(momentum_3m)
            if momentum_3m
            else 0.0
        )
        metrics[group] = group_metrics
    return metrics


def weighted_structural_cooling_rotation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Diagnostic score for overextended-theme cooling and local rotation.

    The score uses only same-snapshot price, flow, liquidity, risk, and static
    ETF/index-name subtheme tags. It is intentionally kept as a research
    candidate because it repairs some hot-theme reversals while failing others.
    """

    current = _current_observations(observations, snapshot)
    if not current:
        return {}

    raw_components = (
        ("positive_day_ratio_3m", True, 1.5),
        ("etf_share_growth_1q", True, 1.3),
        ("amount_acceleration_1m_6m", True, 1.2),
        ("relative_strength_3m", True, 1.1),
        ("momentum_3m", True, 0.8),
        ("momentum_1m", False, 1.1),
        ("amount_crowding_percentile_3y", False, 1.4),
        ("market_correlation_6m", False, 0.7),
        ("drawdown_3m", True, 0.8),
        ("historical_cvar_5pct_3m", True, 0.8),
        ("maximum_daily_loss_3m", True, 0.8),
        ("log_amount_1m", True, 0.5),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    base = {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        code = str(row["ts_code"])
        grouped[str(subthemes_by_code.get(code, "other"))].append(row)

    group_raw: dict[str, float] = {}
    for group, rows in grouped.items():
        if len(rows) < 2 and group not in {"communication", "semiconductor"}:
            continue
        momentum_1m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_1m")) is not None
        ]
        momentum_3m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_3m")) is not None
        ]
        relative_strength_3m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "relative_strength_3m")) is not None
        ]
        share_growth = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "etf_share_growth_1q")) is not None
        ]
        amount_acceleration = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "amount_acceleration_1m_6m")) is not None
        ]
        crowding = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "amount_crowding_percentile_3y")) is not None
        ]
        cvar = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "historical_cvar_5pct_3m")) is not None
        ]
        if not momentum_3m:
            continue
        avg_momentum_1m = statistics.mean(momentum_1m) if momentum_1m else 0.0
        avg_crowding = statistics.mean(crowding) if crowding else 0.5
        hot_penalty = max(0.0, avg_momentum_1m - 0.08) * 1.5 + max(0.0, avg_crowding - 0.78) * 0.5
        breadth = sum(1 for value in momentum_3m if value > 0.0) / len(momentum_3m)
        score = (
            0.24 * breadth
            + 0.20 * (statistics.mean(share_growth) if share_growth else 0.0)
            + 0.16 * (statistics.mean(amount_acceleration) if amount_acceleration else 0.0)
            + 0.14 * (1.0 - avg_crowding)
            + 0.12 * (statistics.mean(relative_strength_3m) if relative_strength_3m else 0.0)
            + 0.10 * statistics.mean(momentum_3m)
            + 0.04 * (statistics.mean(cvar) if cvar else 0.0)
            - hot_penalty
        )
        if group in {"communication", "utilities", "resources", "finance"}:
            score += 0.08
        if group == "digital_hot" and avg_momentum_1m > 0.10 and avg_crowding > 0.80:
            score -= 0.25
        group_raw[group] = score

    group_rank = _percentile_scores(group_raw, higher_is_better=True)
    return {
        code: 0.48 * score
        + 0.52 * group_rank.get(str(subthemes_by_code.get(code, "other")), 0.45)
        for code, score in base.items()
    }


def structural_hot_theme_cooling_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect crowded digital leadership with investable local rotation breadth."""

    current = _current_observations(observations, snapshot)
    if not current:
        return False
    metrics = _structural_group_metrics(current, subthemes_by_code)
    hot = metrics.get("digital_hot")
    if hot is None:
        return False
    hot_overextended = (
        hot["momentum_3m"] >= 0.18
        and hot["momentum_1m"] >= 0.06
        and hot["amount_crowding_percentile_3y"] >= 0.72
    )
    local_rotation = any(
        (candidate := metrics.get(group)) is not None
        and candidate["breadth_3m_positive"] >= 0.75
        and candidate["momentum_3m"] >= 0.03
        for group in ("communication", "utilities", "semiconductor")
    )
    return hot_overextended and local_rotation


def structural_resources_reflation_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect resources/value reflation while avoiding crowded digital episodes."""

    current = _current_observations(observations, snapshot)
    if not current:
        return False
    metrics = _structural_group_metrics(current, subthemes_by_code)
    reflation_breadth = any(
        (candidate := metrics.get(group)) is not None
        and candidate["breadth_3m_positive"] >= 0.80
        and candidate["momentum_3m"] >= 0.03
        for group in ("resources", "finance", "industrial")
    )
    hot = metrics.get("digital_hot")
    crowded_digital = (
        hot is not None
        and hot["momentum_3m"] >= 0.20
        and hot["amount_crowding_percentile_3y"] >= 0.80
    )
    return reflation_breadth and not crowded_digital


def structural_growth_exhaustion_rotation_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect mature crowded growth leadership vulnerable to local rotation."""

    current = _current_observations(observations, snapshot)
    if not current:
        return False
    metrics = _structural_group_metrics(current, subthemes_by_code)
    growth_groups = [
        metrics[group]
        for group in ("digital_hot", "semiconductor", "communication")
        if group in metrics
    ]
    if not growth_groups:
        return False
    return any(
        (
            group["momentum_3m"] >= 0.25
            and group["amount_crowding_percentile_3y"] >= 0.80
        )
        or (
            group["momentum_6m"] >= 0.25
            and group["etf_share_growth_1q"] >= 1.00
            and group["amount_crowding_percentile_3y"] >= 0.80
        )
        or (
            group["momentum_6m"] >= 0.25
            and group["momentum_1m"] <= 0.00
            and group["drawdown_3m"] <= -0.10
        )
        for group in growth_groups
    )


def structural_policy_catalyst_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect policy/fundamental local-mainline catalysts point-in-time."""

    current = _current_observations(observations, snapshot)
    if not current:
        return False
    catalyst_count = 0
    for row in current:
        code = str(row["ts_code"])
        group = str(subthemes_by_code.get(code, "other"))
        if group not in {
            "digital_hot",
            "semiconductor",
            "communication",
            "new_energy",
            "healthcare",
        }:
            continue
        policy = _finite_observation_value(row, "index_policy_score") or 0.0
        momentum_6m = _finite_observation_value(row, "momentum_6m") or 0.0
        momentum_1m = _finite_observation_value(row, "momentum_1m") or 0.0
        momentum_3m = _finite_observation_value(row, "momentum_3m") or 0.0
        earnings_growth_6m = (
            _finite_observation_value(row, "index_fundamental_earnings_growth_6m")
            or 0.0
        )
        roe_change = _finite_observation_value(row, "index_constituent_roe_change_12m") or 0.0
        drawdown_3m = _finite_observation_value(row, "drawdown_3m") or -1.0
        crowding = _finite_observation_value(row, "amount_crowding_percentile_3y") or 0.5
        if (
            policy >= 6.0
            and (
                momentum_6m >= 0.15
                or earnings_growth_6m >= 0.15
                or roe_change >= 0.05
            )
            and (momentum_1m >= 0.0 or momentum_3m >= 0.03)
            and drawdown_3m > -0.18
            and crowding < 0.95
        ):
            catalyst_count += 1
    return catalyst_count >= 2


def weighted_structural_policy_catalyst_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Score local mainlines with policy tags and fundamental/trend repair."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    raw_components = (
        ("index_policy_score", True, 2.2),
        ("index_fundamental_earnings_growth_6m", True, 1.4),
        ("index_constituent_roe_change_12m", True, 1.0),
        ("momentum_6m", True, 1.3),
        ("momentum_1m", True, 1.0),
        ("relative_strength_6m", True, 0.9),
        ("positive_day_ratio_3m", True, 0.8),
        ("etf_share_growth_1q", True, 0.6),
        ("market_correlation_6m", False, 0.6),
        ("amount_crowding_percentile_3y", False, 0.6),
        ("drawdown_3m", True, 0.5),
        ("historical_cvar_5pct_3m", True, 0.4),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    base = {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }
    group_raw: dict[str, float] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        grouped[str(subthemes_by_code.get(str(row["ts_code"]), "other"))].append(row)
    for group, rows in grouped.items():
        policy = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "index_policy_score")) is not None
        ]
        momentum_6m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_6m")) is not None
        ]
        earnings_growth = [
            value
            for row in rows
            if (
                value := _finite_observation_value(
                    row,
                    "index_fundamental_earnings_growth_6m",
                )
            )
            is not None
        ]
        momentum_1m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_1m")) is not None
        ]
        if not policy and not momentum_6m:
            continue
        catalyst_bonus = 0.0
        if group in {"digital_hot", "semiconductor", "communication", "new_energy", "healthcare"}:
            catalyst_bonus += 0.12
        group_raw[group] = (
            0.32 * ((statistics.mean(policy) / 10.0) if policy else 0.0)
            + 0.24 * (statistics.mean(momentum_6m) if momentum_6m else 0.0)
            + 0.20 * (statistics.mean(earnings_growth) if earnings_growth else 0.0)
            + 0.12 * (statistics.mean(momentum_1m) if momentum_1m else 0.0)
            + catalyst_bonus
        )
    group_rank = _percentile_scores(group_raw, higher_is_better=True)
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        group = str(subthemes_by_code.get(code, "other"))
        score = 0.70 * base[code] + 0.30 * group_rank.get(group, 0.45)
        if group in {"finance", "resources", "industrial"}:
            score *= 0.86
        scores[code] = score
    return scores


def weighted_structural_late_cycle_defensive_rotation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Rotate from exhausted growth leadership into local defensive mainlines."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_growth_exhaustion_rotation_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        return weighted_structural_multistate_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )

    raw_components = (
        ("amount_crowding_percentile_3y", False, 1.8),
        ("market_correlation_6m", False, 1.3),
        ("drawdown_3m", True, 1.2),
        ("historical_cvar_5pct_3m", True, 1.0),
        ("maximum_daily_loss_3m", True, 1.0),
        ("momentum_6m", True, 0.9),
        ("relative_strength_6m", True, 0.8),
        ("positive_day_ratio_3m", True, 0.7),
        ("etf_share_growth_1q", True, 0.6),
        ("index_constituent_earnings_yield_change_12m", True, 0.6),
        ("index_constituent_roe_change_12m", True, 0.5),
        ("index_pb_history_percentile_3y", False, 0.5),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    base = {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }

    metrics = _structural_group_metrics(current, subthemes_by_code)
    group_raw: dict[str, float] = {}
    for group, values in metrics.items():
        defensive_bonus = 0.12 if group in {
            "resources",
            "consumer",
            "finance",
            "healthcare",
            "utilities",
        } else 0.0
        if group in {"digital_hot", "semiconductor", "communication", "new_energy"}:
            defensive_bonus -= 0.20
        group_raw[group] = (
            0.24 * (1.0 - values["amount_crowding_percentile_3y"])
            + 0.20 * max(values["momentum_6m"], 0.0)
            + 0.16 * values["breadth_3m_positive"]
            + 0.14 * (1.0 - values["market_correlation_6m"])
            + 0.12 * values["drawdown_3m"]
            + 0.08 * values["etf_share_growth_1q"]
            + defensive_bonus
        )
    group_rank = _percentile_scores(group_raw, higher_is_better=True)
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        group = str(subthemes_by_code.get(code, "other"))
        score = 0.46 * base[code] + 0.54 * group_rank.get(group, 0.45)
        if group in {"digital_hot", "semiconductor", "communication", "new_energy"}:
            score *= 0.55
        scores[code] = score
    return scores


def structural_finance_defensive_rotation_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect visible bank/financial leadership during risk-off structural tape."""

    current = _current_observations(observations, snapshot)
    finance_candidates = []
    for row in current:
        code = str(row["ts_code"])
        if str(subthemes_by_code.get(code, "other")) != "finance":
            continue
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        market_correlation = _finite_observation_value(row, "market_correlation_6m")
        if None in (momentum_3m, relative_strength_3m, drawdown_3m):
            continue
        if (
            momentum_3m >= 0.04
            and relative_strength_3m >= 0.03
            and drawdown_3m > -0.07
            and (market_correlation is None or market_correlation <= 0.90)
        ):
            finance_candidates.append(row)
    return len(finance_candidates) >= 3


def weighted_structural_finance_defensive_rotation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Prefer visible financial defensives; otherwise keep the tech-pullback stack."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_finance_defensive_rotation_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        return weighted_structural_late_cycle_tech_pullback_continuation_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )

    raw_components = (
        ("relative_strength_3m", True, 0.24),
        ("momentum_3m", True, 0.20),
        ("drawdown_3m", True, 0.14),
        ("market_correlation_6m", False, 0.12),
        ("positive_day_ratio_3m", True, 0.10),
        ("amount_crowding_percentile_3y", False, 0.08),
        ("etf_share_growth_1q", True, 0.06),
        ("index_constituent_earnings_yield_change_12m", True, 0.06),
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if subtheme == "finance":
            score += 0.30
            if (
                (momentum_3m is not None and momentum_3m < 0.04)
                or (relative_strength_3m is not None and relative_strength_3m < 0.02)
                or (drawdown_3m is not None and drawdown_3m < -0.08)
            ):
                score *= 0.65
        elif subtheme in {"resources", "consumer", "utilities"}:
            score *= 0.60
        else:
            score *= 0.30
            if subtheme in {"digital_hot", "semiconductor", "communication", "new_energy"}:
                score *= 0.55
        scores[code] = score
    return scores


def structural_small_growth_recovery_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect lagged small-growth recovery after a mature value-led rebound."""

    current = _current_observations(observations, snapshot)
    if not current:
        return False
    lagged_growth_count = 0
    mature_value_count = 0
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        theme = str(groups_by_code.get(code, "other"))
        momentum_1m = _finite_observation_value(row, "momentum_1m")
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        momentum_6m = _finite_observation_value(row, "momentum_6m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if (
            subtheme in {"small_growth", "digital_hot"}
            and momentum_1m is not None
            and momentum_1m >= 0.08
            and momentum_6m is not None
            and momentum_6m < 0.03
            and drawdown_3m is not None
            and drawdown_3m > -0.08
        ):
            lagged_growth_count += 1
        if (
            (subtheme in {"finance", "industrial", "resources"} or theme == "broad_value")
            and momentum_3m is not None
            and momentum_3m >= 0.12
            and drawdown_3m is not None
            and drawdown_3m > -0.03
        ):
            mature_value_count += 1
    return lagged_growth_count >= 2 and mature_value_count >= 3


def weighted_structural_late_cycle_small_growth_recovery_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Late-cycle fallback for lagged ChiNext/small-growth continuation."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_small_growth_recovery_active(
        observations,
        snapshot,
        groups_by_code,
        subthemes_by_code,
    ):
        return weighted_structural_late_cycle_defensive_rotation_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )

    raw_components = (
        ("momentum_1m", True, 0.26),
        ("momentum_3m", True, 0.10),
        ("momentum_6m", False, 0.20),
        ("drawdown_3m", True, 0.14),
        ("market_correlation_6m", False, 0.12),
        ("amount_acceleration_1m_6m", True, 0.10),
        ("etf_positive_turnover_pressure_1m", True, 0.04),
        ("amount_crowding_percentile_3y", False, 0.04),
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        theme = str(groups_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        if subtheme in {"small_growth", "digital_hot"}:
            score += 0.18
        elif subtheme in {"finance", "industrial", "resources"}:
            score *= 0.65
        elif theme == "broad_value":
            score *= 0.70
        momentum_1m = _finite_observation_value(row, "momentum_1m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if (
            (momentum_1m is not None and momentum_1m < 0.06)
            or (drawdown_3m is not None and drawdown_3m < -0.10)
        ):
            score *= 0.65
        scores[code] = score
    return scores


def structural_tech_pullback_continuation_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect a narrow technology continuation after a visible pullback."""

    current = _current_observations(observations, snapshot)
    if not current:
        return False
    tech_groups = {"semiconductor", "digital_hot", "communication"}
    candidates: list[dict[str, Any]] = []
    semiconductor_candidates: list[dict[str, Any]] = []
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        if subtheme not in tech_groups:
            continue
        momentum_1m = _finite_observation_value(row, "momentum_1m")
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        share_growth = _finite_observation_value(row, "etf_share_growth_1q")
        amount_acceleration = _finite_observation_value(row, "amount_acceleration_1m_6m")
        crowding = _finite_observation_value(row, "amount_crowding_percentile_3y")
        if None in (momentum_1m, momentum_3m, relative_strength_3m, drawdown_3m):
            continue
        has_flow_or_crowding_confirmation = (
            (share_growth is not None and share_growth >= 0.15)
            or (amount_acceleration is not None and amount_acceleration >= 0.20)
            or (crowding is not None and crowding >= 0.75)
        )
        if (
            momentum_3m >= 0.07
            and relative_strength_3m >= 0.05
            and -0.15 < drawdown_3m < -0.035
            and has_flow_or_crowding_confirmation
        ):
            candidates.append(row)
            if subtheme == "semiconductor":
                semiconductor_candidates.append(row)
    if len(candidates) < 3:
        return False
    mean_momentum_1m = statistics.mean(
        _finite_observation_value(row, "momentum_1m") or 0.0
        for row in candidates
    )
    mean_momentum_3m = statistics.mean(
        _finite_observation_value(row, "momentum_3m") or 0.0
        for row in candidates
    )
    if mean_momentum_1m > -0.005 or mean_momentum_3m > 0.22:
        return False
    for row in semiconductor_candidates:
        momentum_1m = _finite_observation_value(row, "momentum_1m")
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        share_growth = _finite_observation_value(row, "etf_share_growth_1q")
        if (
            momentum_1m is not None
            and momentum_1m <= 0.0
            and momentum_3m is not None
            and 0.09 <= momentum_3m <= 0.28
            and (share_growth is None or share_growth < 4.0)
        ):
            return True
    return False


def weighted_structural_late_cycle_tech_pullback_continuation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Narrow late-cycle continuation score for corrected technology leadership."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_tech_pullback_continuation_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        if structural_local_mainline_pullback_reentry_active(
            observations,
            snapshot,
            subthemes_by_code,
        ):
            return weighted_structural_local_mainline_pullback_reentry_scores(
                observations,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            )
        if structural_new_energy_pullback_restart_active(
            observations,
            snapshot,
            subthemes_by_code,
        ):
            return weighted_structural_new_energy_pullback_restart_scores(
                observations,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            )
        if structural_digital_blowoff_rotation_active(
            observations,
            snapshot,
            subthemes_by_code,
        ):
            return weighted_structural_digital_blowoff_rotation_scores(
                observations,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            )
        if structural_digital_reacceleration_active(
            observations,
            snapshot,
            subthemes_by_code,
        ):
            return weighted_structural_digital_reacceleration_scores(
                observations,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            )
        if structural_healthcare_leadership_active(
            observations,
            snapshot,
            groups_by_code,
        ):
            return weighted_structural_healthcare_leadership_scores(
                observations,
                snapshot,
                groups_by_code,
                subthemes_by_code,
            )
        return weighted_structural_late_cycle_small_growth_recovery_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )

    raw_components = (
        ("relative_strength_3m", True, 0.27),
        ("momentum_3m", True, 0.19),
        ("etf_share_growth_1q", True, 0.14),
        ("amount_acceleration_1m_6m", True, 0.11),
        ("positive_day_ratio_3m", True, 0.08),
        ("market_correlation_6m", False, 0.07),
        ("drawdown_3m", True, 0.06),
        ("amount_crowding_percentile_3y", True, 0.05),
        ("momentum_1m", False, 0.03),
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    tech_groups = {"semiconductor", "digital_hot", "communication"}
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        momentum_1m = _finite_observation_value(row, "momentum_1m")
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if subtheme in tech_groups:
            score += 0.23
            if momentum_1m is not None and momentum_1m > 0.04:
                score *= 0.42
            if (
                (momentum_3m is not None and momentum_3m < 0.06)
                or (relative_strength_3m is not None and relative_strength_3m < 0.04)
                or (drawdown_3m is not None and drawdown_3m < -0.18)
            ):
                score *= 0.55
        else:
            score *= 0.78
            if subtheme in {"finance", "resources", "industrial", "utilities"}:
                score *= 0.75
        scores[code] = score
    return scores


def weighted_structural_late_cycle_policy_catalyst_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Late-cycle structural score with an explicit policy catalyst state."""

    if structural_policy_catalyst_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        return weighted_structural_policy_catalyst_scores(
            observations,
            snapshot,
            subthemes_by_code,
        )
    return weighted_structural_late_cycle_defensive_rotation_scores(
        observations,
        snapshot,
        groups_by_code,
        subthemes_by_code,
    )


def weighted_structural_liquidity_group_breadth_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    *,
    group_weight: float = 0.10,
) -> dict[str, float]:
    """Liquidity-flow structural score with point-in-time theme breadth.

    The group component uses only same-snapshot ETF features inside static
    ETF/index-name buckets.  The 10% group weight is deliberately small: in
    structural-quarter diagnostics it improved capture without replacing the
    ETF-level liquidity/flow signal.
    """

    base = weighted_structural_liquidity_flow_scores(observations, snapshot)
    current = _current_observations(observations, snapshot)
    if not base or not current:
        return base
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        code = str(row["ts_code"])
        grouped[str(groups_by_code.get(code, "other"))].append(row)
    group_raw: dict[str, float] = {}
    for group, rows in grouped.items():
        if group == "other" or len(rows) < 2:
            continue
        momentum_3m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_3m")) is not None
        ]
        momentum_6m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_6m")) is not None
        ]
        relative_strength_3m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "relative_strength_3m")) is not None
        ]
        share_growth = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "etf_share_growth_1q")) is not None
        ]
        if not momentum_3m or not momentum_6m:
            continue
        breadth = sum(1 for value in momentum_3m if value > 0.0) / len(momentum_3m)
        group_raw[group] = (
            0.35 * statistics.mean(momentum_3m)
            + 0.20 * statistics.mean(momentum_6m)
            + 0.20 * (statistics.mean(relative_strength_3m) if relative_strength_3m else 0.0)
            + 0.15 * breadth
            + 0.10 * (statistics.mean(share_growth) if share_growth else 0.0)
        )
    group_rank = _percentile_scores(group_raw, higher_is_better=True)
    if not group_rank:
        return base
    clipped_group_weight = max(0.0, min(float(group_weight), 1.0))
    own_weight = 1.0 - clipped_group_weight
    return {
        code: own_weight * score
        + clipped_group_weight * group_rank.get(str(groups_by_code.get(code, "other")), 0.5)
        for code, score in base.items()
    }


def select_weighted_structural_liquidity_group_breadth_top5(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
) -> dict[str, float]:
    scores = weighted_structural_liquidity_group_breadth_scores(
        observations,
        snapshot,
        groups_by_code,
    )
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:5]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def weighted_structural_reflation_rotation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    """Point-in-time score for cooled-off structural/reflation rotation.

    This recipe targets quarters where the prior high-momentum leaders are
    vulnerable but ETFs with still-positive medium-term strength, lower market
    linkage, better drawdown behavior, and lower crowding remain investable.
    """

    current = _current_observations(observations, snapshot)
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
        ("momentum_1m", False, 1.5),
        ("momentum_3m", True, 0.8),
        ("momentum_6m", True, 0.5),
        ("relative_strength_6m", True, 0.5),
        ("market_beta_6m", False, 0.5),
        ("market_correlation_6m", False, 0.75),
        ("etf_share_growth_1q", True, 0.4),
        ("amount_crowding_percentile_3y", False, 0.6),
        ("drawdown_3m", True, 0.5),
        ("historical_cvar_5pct_3m", True, 0.4),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (rank(feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    return {
        str(row["ts_code"]): sum(
            weight * values[str(row["ts_code"])] for values, weight in components
        )
        for row in current
    }


def weighted_structural_value_reflation_mainline_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Score local value/reflation leaders without chasing hot digital themes."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    raw_components = (
        ("momentum_6m", True, 1.4),
        ("relative_strength_6m", True, 1.2),
        ("momentum_3m", True, 1.1),
        ("relative_strength_3m", True, 1.0),
        ("market_correlation_6m", False, 0.8),
        ("drawdown_3m", True, 0.7),
        ("etf_share_growth_1q", True, 0.5),
        ("amount_acceleration_1m_6m", True, 0.4),
        ("amount_crowding_percentile_3y", False, 0.5),
        ("volatility_3m", False, 0.4),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    output: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        group = str(groups_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        if subtheme == "resources":
            score += 0.22
        elif subtheme == "finance":
            score += 0.18
        elif subtheme == "industrial":
            score += 0.02
        elif group == "broad_value":
            score += 0.16
        elif subtheme in {"consumer", "utilities"}:
            score += 0.03
        if subtheme in {"digital_hot", "new_energy", "semiconductor"}:
            score *= 0.15
        elif subtheme == "communication":
            score *= 0.30
        elif subtheme not in {"resources", "finance"} and group != "broad_value":
            score *= 0.45
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        momentum_6m = _finite_observation_value(row, "momentum_6m")
        if (
            momentum_3m is not None
            and momentum_6m is not None
            and momentum_3m < 0.0
            and momentum_6m < 0.0
        ):
            score *= 0.40
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if drawdown_3m is not None and drawdown_3m < -0.12:
            score *= 0.50
        crowding = _finite_observation_value(row, "amount_crowding_percentile_3y")
        if crowding is not None and crowding > 0.98:
            score *= 0.70
        output[code] = score
    return output


def structural_finance_catchup_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect finance catch-up after broad strength without using labels."""

    current = _current_observations(observations, snapshot)
    finance_rows = [
        row
        for row in current
        if str(subthemes_by_code.get(str(row["ts_code"]), "other")) == "finance"
    ]
    if len(finance_rows) < 4:
        return False
    momentum_3m = [
        value
        for row in finance_rows
        if (value := _finite_observation_value(row, "momentum_3m")) is not None
    ]
    momentum_6m = [
        value
        for row in finance_rows
        if (value := _finite_observation_value(row, "momentum_6m")) is not None
    ]
    crowding = [
        value
        for row in finance_rows
        if (value := _finite_observation_value(row, "amount_crowding_percentile_3y"))
        is not None
    ]
    if not momentum_3m or not momentum_6m:
        return False
    return bool(
        statistics.mean(momentum_3m) >= 0.02
        and statistics.mean(momentum_6m) >= 0.10
        and sum(value > 0.0 for value in momentum_3m) / len(momentum_3m) >= 0.70
        and sum(value > 0.0 for value in momentum_6m) / len(momentum_6m) >= 0.70
        and (not crowding or statistics.mean(crowding) <= 0.95)
    )


def weighted_structural_finance_catchup_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Rank finance catch-up candidates from point-in-time price/flow features."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    raw_components = (
        ("momentum_6m", True, 1.4),
        ("momentum_3m", True, 1.1),
        ("relative_strength_6m", True, 0.9),
        ("relative_strength_3m", True, 0.7),
        ("drawdown_3m", True, 0.8),
        ("market_correlation_6m", False, 0.4),
        ("etf_share_growth_1q", True, 0.4),
        ("amount_crowding_percentile_3y", False, 0.5),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        if subtheme != "finance":
            continue
        score = sum(weight * values[code] for values, weight in components)
        score += 0.30
        momentum_6m = _finite_observation_value(row, "momentum_6m")
        if momentum_6m is not None and momentum_6m < 0.0:
            score *= 0.35
        crowding = _finite_observation_value(row, "amount_crowding_percentile_3y")
        if crowding is not None and crowding > 0.98:
            score *= 0.45
        scores[code] = score
    return scores


def weighted_structural_finance_bank_catchup_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
    finance_substyles_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Rank the bank/dividend part of a finance catch-up state."""

    base = weighted_structural_finance_catchup_scores(
        observations,
        snapshot,
        subthemes_by_code,
    )
    output: dict[str, float] = {}
    for code, score in base.items():
        substyle = str(finance_substyles_by_code.get(code, "other"))
        if substyle == "bank_dividend":
            output[code] = score + 0.20
        elif substyle == "broad_finance":
            output[code] = score * 0.75
        else:
            continue
    return output


def weighted_structural_finance_resource_catchup_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
    finance_substyles_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Rank bank/dividend plus resource ETFs in a broad-strength catch-up state."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    raw_components = (
        ("momentum_6m", True, 1.2),
        ("momentum_3m", True, 1.1),
        ("relative_strength_6m", True, 0.8),
        ("relative_strength_3m", True, 0.8),
        ("drawdown_3m", True, 0.7),
        ("market_correlation_6m", False, 0.5),
        ("etf_share_growth_1q", True, 0.4),
        ("amount_crowding_percentile_3y", False, 0.4),
        ("volatility_3m", False, 0.3),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    output: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        finance_substyle = str(finance_substyles_by_code.get(code, "other"))
        if subtheme == "resources":
            style_bonus = 0.24
        elif subtheme == "finance" and finance_substyle == "bank_dividend":
            style_bonus = 0.18
        elif subtheme == "finance" and finance_substyle == "broad_finance":
            style_bonus = 0.02
        else:
            continue
        score = sum(weight * values[code] for values, weight in components)
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        momentum_6m = _finite_observation_value(row, "momentum_6m")
        if (
            momentum_3m is not None
            and momentum_6m is not None
            and momentum_3m < 0.0
            and momentum_6m < 0.0
        ):
            score *= 0.40
        crowding = _finite_observation_value(row, "amount_crowding_percentile_3y")
        if crowding is not None and crowding > 0.985:
            score *= 0.55
        output[code] = score + style_bonus
    return output


def weighted_structural_resource_bank_catchup_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    catchup_styles_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Rank resources plus pure-bank ETFs in a confirmed catch-up tape."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    raw_components = (
        ("momentum_3m", True, 1.3),
        ("momentum_6m", True, 1.1),
        ("relative_strength_3m", True, 0.9),
        ("relative_strength_6m", True, 0.7),
        ("drawdown_3m", True, 0.7),
        ("market_correlation_6m", False, 0.5),
        ("etf_share_growth_1q", True, 0.4),
        ("amount_crowding_percentile_3y", False, 0.4),
        ("volatility_3m", False, 0.3),
    )
    total_weight = sum(weight for _feature, _higher, weight in raw_components)
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight / total_weight)
        for feature, higher, weight in raw_components
    )
    output: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        style = str(catchup_styles_by_code.get(code, "other"))
        if style == "resources":
            style_bonus = 0.26
        elif style == "bank":
            style_bonus = 0.30
        else:
            continue
        score = sum(weight * values[code] for values, weight in components)
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        momentum_6m = _finite_observation_value(row, "momentum_6m")
        if (
            momentum_3m is not None
            and momentum_6m is not None
            and momentum_3m < 0.0
            and momentum_6m < 0.0
        ):
            score *= 0.40
        momentum_1m = _finite_observation_value(row, "momentum_1m")
        days_since_high = _finite_observation_value(row, "days_since_high_6m")
        if (
            style == "resources"
            and momentum_6m is not None
            and days_since_high is not None
            and momentum_6m > 0.35
            and days_since_high <= 5.0
        ):
            score *= 0.62
        if (
            momentum_3m is not None
            and momentum_6m is not None
            and days_since_high is not None
            and 0.05 <= momentum_3m <= 0.20
            and 0.10 <= momentum_6m <= 0.25
            and 5.0 <= days_since_high <= 60.0
        ):
            score += 0.08
        if (
            momentum_1m is not None
            and momentum_3m is not None
            and momentum_1m < -0.08
            and momentum_3m < 0.10
        ):
            score *= 0.85
        crowding = _finite_observation_value(row, "amount_crowding_percentile_3y")
        if crowding is not None and crowding > 0.985:
            score *= 0.55
        output[code] = score + style_bonus
    return output


def structural_healthcare_leadership_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
) -> bool:
    current = _current_observations(observations, snapshot)
    if not current:
        return False
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in current:
        grouped[str(groups_by_code.get(str(row["ts_code"]), "other"))].append(row)
    group_metrics: dict[str, tuple[float, float, float]] = {}
    for group, rows in grouped.items():
        if group == "other" or len(rows) < 2:
            continue
        momentum_3m = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "momentum_3m")) is not None
        ]
        share_growth = [
            value
            for row in rows
            if (value := _finite_observation_value(row, "etf_share_growth_1q")) is not None
        ]
        if not momentum_3m:
            continue
        breadth = sum(1 for value in momentum_3m if value > 0.0) / len(momentum_3m)
        group_metrics[group] = (
            statistics.mean(momentum_3m),
            breadth,
            statistics.mean(share_growth) if share_growth else 0.0,
        )
    healthcare = group_metrics.get("healthcare")
    if healthcare is None:
        return False
    ranked = sorted(
        group_metrics,
        key=lambda group: (
            group_metrics[group][0],
            group_metrics[group][1],
            group_metrics[group][2],
        ),
        reverse=True,
    )
    if (
        ranked
        and ranked[0] == "healthcare"
        and healthcare[0] >= 0.06
        and healthcare[1] >= 0.80
        and healthcare[2] >= 0.25
    ):
        return True
    healthcare_rank = ranked.index("healthcare") if "healthcare" in ranked else 99
    leader_momentum = group_metrics[ranked[0]][0] if ranked else 0.0
    return (
        healthcare_rank <= 5
        and leader_momentum - healthcare[0] <= 0.05
        and healthcare[0] >= 0.045
        and healthcare[1] >= 0.85
        and healthcare[2] >= 0.25
    )


def weighted_structural_healthcare_leadership_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Healthcare-led structural score that avoids broad/value dilution."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_healthcare_leadership_active(
        observations,
        snapshot,
        groups_by_code,
    ):
        return weighted_structural_resilience_scores(observations, snapshot)

    raw_components = (
        ("momentum_3m", True, 0.22),
        ("relative_strength_3m", True, 0.22),
        ("momentum_1m", True, 0.10),
        ("drawdown_3m", True, 0.10),
        ("etf_share_growth_1q", True, 0.12),
        ("amount_acceleration_1m_6m", True, 0.09),
        ("positive_day_ratio_3m", True, 0.08),
        ("amount_crowding_percentile_3y", False, 0.04),
        ("market_correlation_6m", False, 0.03),
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if subtheme == "healthcare":
            score += 0.28
            if (
                (momentum_3m is not None and momentum_3m < 0.04)
                or (relative_strength_3m is not None and relative_strength_3m < -0.02)
                or (drawdown_3m is not None and drawdown_3m < -0.12)
            ):
                score *= 0.65
        elif subtheme == "small_growth":
            valuation_percentile = _finite_observation_value(
                row,
                "index_pb_history_percentile_3y",
            )
            if (
                valuation_percentile is not None
                and valuation_percentile < 0.15
                and drawdown_3m is not None
                and drawdown_3m > -0.13
            ):
                score *= 0.85
            else:
                score *= 0.45
        else:
            score *= 0.52
            if (
                subtheme in {"finance", "consumer", "other", "industrial", "resources"}
                and (relative_strength_3m is None or relative_strength_3m < 0.02)
            ):
                score *= 0.55
        scores[code] = score
    return scores


def structural_digital_reacceleration_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect broad digital/communication reacceleration without blowoff momentum."""

    current = _current_observations(observations, snapshot)
    if not current:
        return False
    candidates: list[dict[str, Any]] = []
    for row in current:
        subtheme = str(subthemes_by_code.get(str(row["ts_code"]), "other"))
        if subtheme not in {"digital_hot", "communication"}:
            continue
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        amount_acceleration = _finite_observation_value(row, "amount_acceleration_1m_6m")
        market_correlation = _finite_observation_value(row, "market_correlation_6m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if None in (momentum_3m, amount_acceleration, market_correlation, drawdown_3m):
            continue
        if momentum_3m > 0.0 and drawdown_3m > -0.08:
            candidates.append(row)
    if len(candidates) < 5:
        return False
    momentum_3m_values = [
        _finite_observation_value(row, "momentum_3m") or 0.0
        for row in candidates
    ]
    momentum_6m_values = [
        value
        for row in candidates
        if (value := _finite_observation_value(row, "momentum_6m")) is not None
    ]
    amount_acceleration_values = [
        _finite_observation_value(row, "amount_acceleration_1m_6m") or 0.0
        for row in candidates
    ]
    correlation_values = [
        _finite_observation_value(row, "market_correlation_6m") or 1.0
        for row in candidates
    ]
    crowding_values = [
        _finite_observation_value(row, "amount_crowding_percentile_3y") or 0.5
        for row in candidates
    ]
    mean_momentum_3m = statistics.mean(momentum_3m_values)
    mean_momentum_6m = statistics.mean(momentum_6m_values) if momentum_6m_values else 0.0
    standard_reacceleration = (
        0.055 <= mean_momentum_3m <= 0.18
        and mean_momentum_6m < 0.20
        and statistics.mean(amount_acceleration_values) >= 0.35
        and statistics.mean(correlation_values) <= 0.72
        and statistics.mean(crowding_values) < 0.90
    )
    early_price_diffusion = (
        len(candidates) >= 8
        and statistics.mean(
            _finite_observation_value(row, "momentum_1m") or 0.0
            for row in candidates
        )
        >= 0.09
        and 0.07 <= mean_momentum_3m <= 0.18
        and mean_momentum_6m < 0.18
        and statistics.mean(amount_acceleration_values) >= 0.12
        and statistics.mean(correlation_values) <= 0.70
        and statistics.mean(
            _finite_observation_value(row, "drawdown_3m") or 0.0
            for row in candidates
        )
        > -0.04
        and statistics.mean(crowding_values) < 0.80
    )
    return standard_reacceleration or early_price_diffusion


def structural_digital_blowoff_rotation_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect overextended digital leadership that should rotate to safer themes."""

    current = _current_observations(observations, snapshot)
    digital_rows = [
        row
        for row in current
        if str(subthemes_by_code.get(str(row["ts_code"]), "other")) == "digital_hot"
    ]
    if len(digital_rows) < 5:
        return False

    def mean_feature(name: str, default: float | None = None) -> float | None:
        values = [
            value
            for row in digital_rows
            if (value := _finite_observation_value(row, name)) is not None
        ]
        if values:
            return statistics.mean(values)
        return default

    momentum_1m = mean_feature("momentum_1m")
    momentum_3m = mean_feature("momentum_3m")
    momentum_6m = mean_feature("momentum_6m")
    drawdown_3m = mean_feature("drawdown_3m")
    crowding = mean_feature("amount_crowding_percentile_3y")
    correlation = mean_feature("market_correlation_6m")
    return (
        momentum_1m is not None
        and momentum_1m >= 0.08
        and momentum_3m is not None
        and momentum_3m >= 0.28
        and (momentum_6m is None or momentum_6m >= 0.25)
        and drawdown_3m is not None
        and drawdown_3m > -0.02
        and crowding is not None
        and crowding >= 0.78
        and (correlation is None or correlation < 0.70)
    )


def structural_digital_blowoff_utilities_rotation_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect green-power/utilities repair after crowded digital blowoff."""

    if not structural_digital_blowoff_rotation_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        return False
    current = [
        row
        for row in _current_observations(observations, snapshot)
        if str(subthemes_by_code.get(str(row["ts_code"]), "other")) == "utilities"
    ]
    if len(current) < 2:
        return False

    def mean_feature(name: str) -> float | None:
        values = [
            value
            for row in current
            if (value := _finite_observation_value(row, name)) is not None
        ]
        return statistics.mean(values) if values else None

    momentum_3m = mean_feature("momentum_3m")
    momentum_6m = mean_feature("momentum_6m")
    drawdown_3m = mean_feature("drawdown_3m")
    crowding = mean_feature("amount_crowding_percentile_3y")
    return (
        momentum_3m is not None
        and momentum_3m >= 0.025
        and (momentum_6m is None or momentum_6m > -0.08)
        and drawdown_3m is not None
        and drawdown_3m > -0.06
        and (crowding is None or crowding < 0.85)
    )


def weighted_structural_digital_blowoff_rotation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Rotate away from overextended digital themes into local defensive leaders."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_digital_blowoff_rotation_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        return weighted_structural_late_cycle_small_growth_recovery_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )

    raw_components = (
        ("relative_strength_3m", True, 0.22),
        ("momentum_3m", True, 0.18),
        ("drawdown_3m", True, 0.14),
        ("market_correlation_6m", False, 0.13),
        ("amount_crowding_percentile_3y", False, 0.12),
        ("etf_share_growth_1q", True, 0.08),
        ("amount_acceleration_1m_6m", True, 0.07),
        ("positive_day_ratio_3m", True, 0.06),
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    utilities_rotation_active = structural_digital_blowoff_utilities_rotation_active(
        observations,
        snapshot,
        subthemes_by_code,
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
        crowding = _finite_observation_value(row, "amount_crowding_percentile_3y")
        if subtheme == "communication":
            score += 0.24
        elif subtheme == "utilities":
            score += 0.20
            if utilities_rotation_active:
                score += 0.35
        elif subtheme == "digital_hot":
            score *= 0.35
        elif subtheme == "finance":
            score *= 0.62
            if relative_strength_3m is None or relative_strength_3m < 0.03:
                score *= 0.55
        else:
            score *= 0.65
        if crowding is not None and crowding > 0.95:
            score *= 0.65
        scores[code] = score
    return scores


def weighted_structural_digital_reacceleration_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Digital/communication reacceleration score for local AI-style mainlines."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_digital_reacceleration_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        return weighted_structural_late_cycle_small_growth_recovery_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )

    raw_components = (
        ("relative_strength_3m", True, 0.24),
        ("momentum_3m", True, 0.20),
        ("amount_acceleration_1m_6m", True, 0.16),
        ("market_correlation_6m", False, 0.12),
        ("positive_day_ratio_3m", True, 0.10),
        ("drawdown_3m", True, 0.08),
        ("etf_share_growth_1q", True, 0.05),
        ("amount_crowding_percentile_3y", False, 0.05),
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
        market_correlation = _finite_observation_value(row, "market_correlation_6m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        if subtheme in {"digital_hot", "communication"}:
            score += 0.24
            if (
                (momentum_3m is not None and momentum_3m < 0.03)
                or (relative_strength_3m is not None and relative_strength_3m < -0.04)
                or (market_correlation is not None and market_correlation > 0.82)
                or (drawdown_3m is not None and drawdown_3m < -0.12)
            ):
                score *= 0.55
        elif subtheme == "semiconductor":
            score *= 0.75
        else:
            score *= 0.45
            if subtheme in {"finance", "resources", "consumer", "small_growth"}:
                score *= 0.75
        scores[code] = score
    return scores


def structural_local_mainline_pullback_reentry_candidate(
    row: Mapping[str, Any],
) -> bool:
    momentum_1m = _finite_observation_value(row, "momentum_1m")
    momentum_3m = _finite_observation_value(row, "momentum_3m")
    momentum_6m = _finite_observation_value(row, "momentum_6m")
    drawdown_3m = _finite_observation_value(row, "drawdown_3m")
    drawdown_6m = _finite_observation_value(row, "drawdown_6m")
    relative_strength_3m = _finite_observation_value(row, "relative_strength_3m")
    relative_strength_6m = _finite_observation_value(row, "relative_strength_6m")
    market_correlation = _finite_observation_value(row, "market_correlation_6m")
    volatility_3m = _finite_observation_value(row, "volatility_3m")
    days_since_high = _finite_observation_value(row, "days_since_high_6m")
    if None in (momentum_1m, momentum_3m, drawdown_3m, drawdown_6m):
        return False
    capitulation_setup = bool(
        momentum_1m <= -0.07
        and -0.25 <= momentum_3m <= -0.05
        and -0.25 <= drawdown_3m <= -0.10
        and -0.35 <= drawdown_6m <= -0.15
        and (relative_strength_3m is None or relative_strength_3m >= -0.03)
        and (market_correlation is None or market_correlation <= 0.75)
        and (volatility_3m is None or volatility_3m <= 0.45)
        and (days_since_high is None or days_since_high >= 50.0)
    )
    leadership_pullback_setup = bool(
        momentum_6m is not None
        and momentum_6m >= 0.25
        and relative_strength_6m is not None
        and relative_strength_6m >= 0.18
        and momentum_1m <= -0.07
        and -0.18 <= momentum_3m <= -0.04
        and -0.25 <= drawdown_3m <= -0.12
        and -0.30 <= drawdown_6m <= -0.15
        and (market_correlation is None or market_correlation <= 0.80)
        and (volatility_3m is None or volatility_3m <= 0.52)
        and (days_since_high is None or days_since_high >= 35.0)
    )
    restart_setup = bool(
        momentum_1m >= 0.05
        and -0.20 <= momentum_3m <= -0.05
        and (
            (momentum_6m is not None and momentum_6m >= 0.25)
            or (relative_strength_6m is not None and relative_strength_6m >= 0.20)
        )
        and -0.20 < drawdown_3m < -0.06
        and (market_correlation is None or market_correlation < 0.85)
    )
    return capitulation_setup or leadership_pullback_setup or restart_setup


def _new_energy_capitulation_reentry_candidate(row: Mapping[str, Any]) -> bool:
    return structural_local_mainline_pullback_reentry_candidate(row)


def structural_local_mainline_pullback_reentry_subthemes(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> set[str]:
    current = _current_observations(observations, snapshot)
    counts: Counter[str] = Counter()
    relative_strength: dict[str, list[float]] = defaultdict(list)
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        if subtheme == "other":
            continue
        if structural_local_mainline_pullback_reentry_candidate(row):
            counts[subtheme] += 1
            value = _finite_observation_value(row, "relative_strength_6m")
            if value is not None:
                relative_strength[subtheme].append(value)
    eligible = {subtheme: count for subtheme, count in counts.items() if count >= 2}
    if not eligible:
        return set()
    max_count = max(eligible.values())
    leaders = [
        subtheme for subtheme, count in eligible.items() if count == max_count
    ]
    if len(leaders) == 1:
        return {leaders[0]}
    best_strength = max(
        statistics.mean(relative_strength.get(subtheme) or [0.0])
        for subtheme in leaders
    )
    return {
        subtheme
        for subtheme in leaders
        if statistics.mean(relative_strength.get(subtheme) or [0.0])
        >= best_strength - 0.02
    }


def structural_local_mainline_pullback_reentry_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    return bool(
        structural_local_mainline_pullback_reentry_subthemes(
            observations,
            snapshot,
            subthemes_by_code,
        )
    )


def structural_new_energy_pullback_restart_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    """Detect new-energy restart or capitulation setups after a pullback."""

    current = _current_observations(observations, snapshot)
    candidates = []
    capitulation_candidates = []
    for row in current:
        if str(subthemes_by_code.get(str(row["ts_code"]), "other")) != "new_energy":
            continue
        if _new_energy_capitulation_reentry_candidate(row):
            capitulation_candidates.append(row)
        momentum_1m = _finite_observation_value(row, "momentum_1m")
        momentum_3m = _finite_observation_value(row, "momentum_3m")
        momentum_6m = _finite_observation_value(row, "momentum_6m")
        drawdown_3m = _finite_observation_value(row, "drawdown_3m")
        market_correlation = _finite_observation_value(row, "market_correlation_6m")
        if None in (momentum_1m, momentum_3m, momentum_6m, drawdown_3m):
            continue
        if (
            momentum_1m >= 0.05
            and -0.20 <= momentum_3m <= -0.05
            and momentum_6m >= 0.25
            and -0.20 < drawdown_3m < -0.06
            and (market_correlation is None or market_correlation < 0.85)
        ):
            candidates.append(row)
    return len(candidates) >= 2 or len(capitulation_candidates) >= 2


def structural_new_energy_capitulation_reentry_active(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    subthemes_by_code: Mapping[str, str],
) -> bool:
    current = _current_observations(observations, snapshot)
    return (
        sum(
            1
            for row in current
            if str(subthemes_by_code.get(str(row["ts_code"]), "other"))
            == "new_energy"
            and _new_energy_capitulation_reentry_candidate(row)
        )
        >= 2
    )


def weighted_structural_new_energy_pullback_restart_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """New-energy restart score after strong six-month leadership and pullback."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    if not structural_new_energy_pullback_restart_active(
        observations,
        snapshot,
        subthemes_by_code,
    ):
        return weighted_structural_late_cycle_small_growth_recovery_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )

    capitulation_active = structural_new_energy_capitulation_reentry_active(
        observations,
        snapshot,
        subthemes_by_code,
    )
    raw_components = (
        (
            ("relative_strength_3m", True, 0.24),
            ("market_correlation_6m", False, 0.16),
            ("drawdown_3m", False, 0.14),
            ("drawdown_6m", False, 0.12),
            ("volatility_3m", False, 0.10),
            ("momentum_3m", False, 0.09),
            ("amount_crowding_percentile_3y", False, 0.06),
            ("positive_day_ratio_3m", True, 0.05),
            ("etf_share_growth_1q", True, 0.04),
        )
        if capitulation_active
        else (
            ("momentum_1m", True, 0.24),
            ("momentum_6m", True, 0.20),
            ("momentum_3m", False, 0.18),
            ("drawdown_3m", True, 0.12),
            ("market_correlation_6m", False, 0.10),
            ("amount_crowding_percentile_3y", False, 0.06),
            ("etf_share_growth_1q", True, 0.05),
            ("positive_day_ratio_3m", True, 0.05),
        )
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        if subtheme == "new_energy":
            score += 0.28
            if capitulation_active and _new_energy_capitulation_reentry_candidate(row):
                score += 0.18
        elif subtheme == "small_growth":
            score *= 0.75
        else:
            score *= 0.42
            if subtheme in {"resources", "finance", "healthcare", "industrial"}:
                score *= 0.65
        scores[code] = score
    return scores


def weighted_structural_local_mainline_pullback_reentry_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Generic local-mainline pullback score without hard-coding a theme."""

    current = _current_observations(observations, snapshot)
    if not current:
        return {}
    active_subthemes = structural_local_mainline_pullback_reentry_subthemes(
        observations,
        snapshot,
        subthemes_by_code,
    )
    if not active_subthemes:
        return weighted_structural_late_cycle_small_growth_recovery_scores(
            observations,
            snapshot,
            groups_by_code,
            subthemes_by_code,
        )
    raw_components = (
        ("relative_strength_3m", True, 0.24),
        ("market_correlation_6m", False, 0.16),
        ("drawdown_3m", False, 0.14),
        ("drawdown_6m", False, 0.12),
        ("volatility_3m", False, 0.10),
        ("momentum_3m", False, 0.09),
        ("amount_crowding_percentile_3y", False, 0.06),
        ("positive_day_ratio_3m", True, 0.05),
        ("etf_share_growth_1q", True, 0.04),
    )
    components = tuple(
        (_rank_current_observations(current, feature, higher), weight)
        for feature, higher, weight in raw_components
    )
    scores: dict[str, float] = {}
    for row in current:
        code = str(row["ts_code"])
        subtheme = str(subthemes_by_code.get(code, "other"))
        score = sum(weight * values[code] for values, weight in components)
        if subtheme in active_subthemes:
            score += 0.28
            if structural_local_mainline_pullback_reentry_candidate(row):
                score += 0.18
        elif subtheme == "small_growth":
            score *= 0.75
        else:
            score *= 0.42
            if subtheme in {"resources", "finance", "healthcare", "industrial"}:
                score *= 0.65
        scores[code] = score
    return scores


def weighted_structural_conditional_rotation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Switch between healthcare-resilience and reflation rotation point-in-time."""

    if structural_healthcare_leadership_active(observations, snapshot, groups_by_code):
        return weighted_structural_resilience_scores(observations, snapshot)
    return weighted_structural_reflation_rotation_scores(observations, snapshot)


def weighted_structural_multistate_rotation_scores(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    """Switch structural repair recipes by point-in-time local-market state."""

    if structural_healthcare_leadership_active(observations, snapshot, groups_by_code):
        return weighted_structural_resilience_scores(observations, snapshot)
    if structural_resources_reflation_active(observations, snapshot, subthemes_by_code):
        return weighted_structural_reflation_rotation_scores(observations, snapshot)
    if structural_hot_theme_cooling_active(observations, snapshot, subthemes_by_code):
        return weighted_structural_cooling_rotation_scores(
            observations,
            snapshot,
            subthemes_by_code,
        )
    return weighted_structural_conditional_rotation_scores(
        observations,
        snapshot,
        groups_by_code,
    )


def select_weighted_structural_conditional_rotation_top3(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
) -> dict[str, float]:
    scores = weighted_structural_conditional_rotation_scores(
        observations,
        snapshot,
        groups_by_code,
    )
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:3]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def select_weighted_structural_late_cycle_defensive_rotation_top3(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    groups_by_code: Mapping[str, str],
    subthemes_by_code: Mapping[str, str],
) -> dict[str, float]:
    scores = weighted_structural_late_cycle_defensive_rotation_scores(
        observations,
        snapshot,
        groups_by_code,
        subthemes_by_code,
    )
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:3]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def select_weighted_structural_reflation_rotation_top3(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_structural_reflation_rotation_scores(observations, snapshot)
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:3]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def select_weighted_structural_mainline_etfs(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
    top_n: int,
    score_power: float = 2.0,
) -> dict[str, float]:
    scores = weighted_structural_mainline_scores(observations, snapshot)
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:top_n]
    powered = {code: max(scores[code], 0.01) ** score_power for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def select_weighted_structural_mainline_top3(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    return select_weighted_structural_mainline_etfs(observations, snapshot, 3, 2.0)


def select_weighted_structural_mainline_top5(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    return select_weighted_structural_mainline_etfs(observations, snapshot, 5, 2.0)


def select_weighted_structural_liquidity_flow_top5(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_structural_liquidity_flow_scores(observations, snapshot)
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:5]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def select_weighted_structural_momentum_breadth_top3(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_structural_momentum_breadth_scores(observations, snapshot)
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:3]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def select_weighted_structural_resilience_top5(
    observations: Sequence[dict[str, Any]],
    snapshot: date,
) -> dict[str, float]:
    scores = weighted_structural_resilience_scores(observations, snapshot)
    if not scores:
        return {}
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:5]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


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
    current = _current_observations(observations, snapshot)
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

    current = _current_observations(observations, snapshot)
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
