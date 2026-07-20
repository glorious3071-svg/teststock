"""Point-in-time domestic defensive ETF selection for monthly backtests."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class DefensiveEtfMeta:
    code: str
    name: str
    index_name: str
    first_trade_date: date
    category: str


@dataclass(frozen=True)
class DefensivePolicy:
    name: str
    lookback_days: int
    refresh_days: int
    gold_max_weight: float
    gold_min_return: float = 0.0
    bond_min_return: float = -0.02
    volatility_penalty: float = 0.5
    portfolio_drawdown_threshold: float | None = None
    stressed_gold_max_weight: float | None = None
    gold_short_lookback_days: int | None = None
    gold_short_min_return: float = 0.0
    bond_top_n: int = 1
    commodity_max_weight: float = 0.0
    commodity_min_return: float = 0.0
    gold_candidate_lookback_days: int | None = None


NO_DEFENSIVE_ETF = DefensivePolicy("cash_only", 126, 21, 0.0)
DEFENSIVE_POLICIES = (
    NO_DEFENSIVE_ETF,
    DefensivePolicy("bond_63d", 63, 21, 0.0),
    DefensivePolicy("bond_126d", 126, 21, 0.0),
    *(
        DefensivePolicy(
            f"bond_{lookback}d_vp{int(round(penalty * 100))}",
            lookback,
            21,
            0.0,
            volatility_penalty=penalty,
        )
        for lookback in (21, 42, 63, 126, 252)
        for penalty in (0.0, 0.25, 1.0)
    ),
    *(
        DefensivePolicy(
            f"bond_126d_vp{int(round(penalty * 100))}",
            126,
            21,
            0.0,
            volatility_penalty=penalty,
        )
        for penalty in (0.10, 0.15, 0.20, 0.30, 0.35, 0.40)
    ),
    *(
        DefensivePolicy(
            f"bond_126d_vp{int(round(penalty * 100))}",
            126,
            21,
            0.0,
            volatility_penalty=penalty,
        )
        for penalty in (0.36, 0.37, 0.38, 0.39, 0.41, 0.42, 0.43, 0.44, 0.45)
    ),
    *(
        DefensivePolicy(
            f"bond_126d_vp41_top{top_n}",
            126,
            21,
            0.0,
            volatility_penalty=0.41,
            bond_top_n=top_n,
        )
        for top_n in (2, 3, 5)
    ),
    *(
        DefensivePolicy(
            f"bond_126d_vp41_min{int(round(minimum_return * 1000)):+d}",
            126,
            21,
            0.0,
            bond_min_return=minimum_return,
            volatility_penalty=0.41,
        )
        for minimum_return in (-1.0, -0.20, -0.10, -0.05, -0.01, 0.0, 0.01, 0.02, 0.03)
    ),
    *(
        DefensivePolicy(
            f"bond_{lookback}d_vp41_min-50",
            lookback,
            21,
            0.0,
            bond_min_return=-0.05,
            volatility_penalty=0.41,
        )
        for lookback in (84, 105, 147, 168, 189)
    ),
    *(
        DefensivePolicy(
            (
                f"bondfine_{lookback}d_vp{int(round(penalty * 100))}"
                f"_top{top_n}_min-50"
            ),
            lookback,
            21,
            0.0,
            bond_min_return=-0.05,
            volatility_penalty=penalty,
            bond_top_n=top_n,
        )
        for lookback in (91, 98, 105, 112, 119)
        for penalty in (0.25, 0.41, 0.60)
        for top_n in (1, 2, 3)
    ),
    *(
        DefensivePolicy(
            (
                f"bond_commodity{int(round(commodity_weight * 100))}"
                f"_{lookback}d_vp41"
            ),
            lookback,
            21,
            0.0,
            bond_min_return=-0.05,
            volatility_penalty=0.41,
            commodity_max_weight=commodity_weight,
        )
        for lookback in (63, 126, 252)
        for commodity_weight in (0.10, 0.20, 0.30, 0.40)
    ),
    DefensivePolicy("bond_gold20_63d", 63, 21, 0.20),
    DefensivePolicy("bond_gold20_126d", 126, 21, 0.20),
    DefensivePolicy("bond_gold35_126d", 126, 21, 0.35),
    DefensivePolicy("bond_gold35_252d", 252, 21, 0.35),
    DefensivePolicy(
        "bond_gold35_252d_gold63",
        252,
        21,
        0.35,
        gold_short_lookback_days=63,
    ),
    DefensivePolicy(
        "bond_gold35_252d_gold42",
        252,
        21,
        0.35,
        gold_short_lookback_days=42,
    ),
    DefensivePolicy(
        "bond_gold40_252d_gold42",
        252,
        21,
        0.40,
        gold_short_lookback_days=42,
    ),
    DefensivePolicy(
        "bond_gold45_252d_gold42",
        252,
        21,
        0.45,
        gold_short_lookback_days=42,
    ),
    DefensivePolicy("bond_gold452_252d_gold42", 252, 21, 0.452, gold_short_lookback_days=42),
    DefensivePolicy("bond_gold454_252d_gold42", 252, 21, 0.454, gold_short_lookback_days=42),
    DefensivePolicy("bond_gold456_252d_gold42", 252, 21, 0.456, gold_short_lookback_days=42),
    DefensivePolicy("bond_gold458_252d_gold42", 252, 21, 0.458, gold_short_lookback_days=42),
    DefensivePolicy(
        "bond_gold46_252d_gold42",
        252,
        21,
        0.46,
        gold_short_lookback_days=42,
    ),
    DefensivePolicy(
        "bond_gold47_252d_gold42",
        252,
        21,
        0.47,
        gold_short_lookback_days=42,
    ),
    DefensivePolicy(
        "bond_gold35_252d_gold84",
        252,
        21,
        0.35,
        gold_short_lookback_days=84,
    ),
    DefensivePolicy(
        "bond_gold35_252d_gold126",
        252,
        21,
        0.35,
        gold_short_lookback_days=126,
    ),
    DefensivePolicy(
        "bond_gold35_252d_dd5cap20",
        252,
        21,
        0.35,
        portfolio_drawdown_threshold=-0.05,
        stressed_gold_max_weight=0.20,
    ),
    DefensivePolicy("bond_gold50_252d", 252, 21, 0.50),
    DefensivePolicy("bond_gold65_252d", 252, 21, 0.65),
    DefensivePolicy("bond_gold80_252d", 252, 21, 0.80),
    DefensivePolicy("bond_gold100_252d", 252, 21, 1.00),
    DefensivePolicy(
        "bond_gold65_252d_earlygold63",
        252,
        21,
        0.65,
        gold_candidate_lookback_days=63,
    ),
    DefensivePolicy(
        "bond_gold65_252d_earlygold63_min-120",
        252,
        21,
        0.65,
        gold_min_return=-0.12,
        gold_candidate_lookback_days=63,
    ),
)


def classify_defensive_etf(code: str, name: str, index_name: str) -> str | None:
    text = f"{code} {name} {index_name}"
    if any(keyword in text for keyword in ("可转债", "可交换债")):
        return None
    if code in {"159980.SZ", "159981.SZ", "159985.SZ"} or any(
        keyword in index_name for keyword in ("期货价格指数", "能源化工指数A")
    ):
        return "commodity"
    if any(keyword in text for keyword in ("黄金", "上海金", "金ETF", "黄金9999")):
        return "gold"
    if code.startswith("511") or any(
        keyword in text
        for keyword in ("国债", "政金债", "信用债", "公司债", "城投债", "地方债")
    ):
        return "bond"
    return None


def load_defensive_etf_universe(conn) -> tuple[dict[str, DefensiveEtfMeta], dict[str, list[tuple[date, float]]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.ts_code, e.extname, e.index_name, MIN(f.trade_date)
            FROM passive_etf e
            JOIN fund_daily f ON f.ts_code=e.ts_code
            WHERE (e.etf_type IS NULL OR e.etf_type!='QDII')
              AND (e.is_enhanced IS NULL OR e.is_enhanced=0)
              AND e.ts_code NOT LIKE '%%.OF'
              AND e.ts_code NOT LIKE '513%%'
              AND e.ts_code NOT LIKE '520%%'
              AND COALESCE(e.extname, '') NOT REGEXP '港股|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|东南亚|沙特'
              AND f.close IS NOT NULL
            GROUP BY e.ts_code, e.extname, e.index_name
            ORDER BY MIN(f.trade_date), e.ts_code
            """
        )
        metas: dict[str, DefensiveEtfMeta] = {}
        for code, name, index_name, first_trade_date in cur.fetchall():
            code = str(code)
            name = str(name or code)
            index_name = str(index_name or "")
            category = classify_defensive_etf(code, name, index_name)
            if category is None:
                continue
            metas[code] = DefensiveEtfMeta(
                code=code,
                name=name,
                index_name=index_name,
                first_trade_date=first_trade_date,
                category=category,
            )
        series = {code: [] for code in metas}
        codes = sorted(metas)
        for start in range(0, len(codes), 300):
            chunk = codes[start : start + 300]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, close, pct_chg
                FROM fund_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            cumulative: dict[str, float] = {code: 100.0 for code in chunk}
            for code, trade_date, close, pct_chg in cur.fetchall():
                code = str(code)
                if series[code] and pct_chg is not None:
                    cumulative[code] *= 1.0 + float(pct_chg) / 100.0
                series[code].append((trade_date, cumulative[code]))
    return metas, series


def _history(
    rows: list[tuple[date, float]],
    snapshot: date,
    points: int,
) -> list[float] | None:
    index = bisect_right(rows, (snapshot, math.inf)) - 1
    if index < points:
        return None
    values = [value for _day, value in rows[index - points : index + 1]]
    if any(value <= 0 for value in values):
        return None
    return values


def _score(values: list[float], volatility_penalty: float) -> tuple[float, float]:
    trailing_return = values[-1] / values[0] - 1.0
    returns = [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))]
    mean_return = sum(returns) / len(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / len(returns)
    annualized_volatility = math.sqrt(variance * 252.0)
    return trailing_return - volatility_penalty * annualized_volatility, trailing_return


def select_defensive_weights(
    metas: dict[str, DefensiveEtfMeta],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    policy: DefensivePolicy,
) -> dict[str, float]:
    if policy.name == NO_DEFENSIVE_ETF.name:
        return {}
    candidates: dict[str, list[tuple[float, float, str]]] = {
        "bond": [],
        "gold": [],
        "commodity": [],
    }
    for code, meta in metas.items():
        if meta.first_trade_date > snapshot:
            continue
        lookback_days = (
            policy.gold_candidate_lookback_days
            if meta.category == "gold"
            and policy.gold_candidate_lookback_days is not None
            else policy.lookback_days
        )
        values = _history(series[code], snapshot, lookback_days)
        if values is None:
            continue
        score, trailing_return = _score(values, policy.volatility_penalty)
        candidates[meta.category].append((score, trailing_return, code))

    weights: dict[str, float] = {}
    bonds = sorted(candidates["bond"], reverse=True)[: policy.bond_top_n]
    gold = max(candidates["gold"], default=None)
    commodity = max(candidates["commodity"], default=None)
    gold_weight = 0.0
    gold_short_trend_ok = True
    if gold is not None and policy.gold_short_lookback_days is not None:
        short_values = _history(
            series[gold[2]],
            snapshot,
            policy.gold_short_lookback_days,
        )
        gold_short_trend_ok = bool(
            short_values
            and short_values[-1] / short_values[0] - 1.0
            >= policy.gold_short_min_return
        )
    if gold is not None and gold[1] >= policy.gold_min_return and gold_short_trend_ok:
        gold_weight = policy.gold_max_weight
        weights[gold[2]] = gold_weight
    commodity_weight = 0.0
    if commodity is not None and commodity[1] >= policy.commodity_min_return:
        commodity_weight = min(policy.commodity_max_weight, 1.0 - gold_weight)
        if commodity_weight > 0:
            weights[commodity[2]] = commodity_weight
    eligible_bonds = [bond for bond in bonds if bond[1] >= policy.bond_min_return]
    if eligible_bonds:
        bond_weight = (1.0 - gold_weight - commodity_weight) / len(eligible_bonds)
        for _score_value, _trailing_return, code in eligible_bonds:
            weights[code] = bond_weight
    return weights


def apply_portfolio_drawdown_guard(
    weights: dict[str, float],
    metas: dict[str, DefensiveEtfMeta],
    policy: DefensivePolicy,
    portfolio_drawdown: float,
) -> tuple[dict[str, float], bool]:
    threshold = policy.portfolio_drawdown_threshold
    gold_cap = policy.stressed_gold_max_weight
    if threshold is None or gold_cap is None or portfolio_drawdown > threshold:
        return dict(weights), False
    adjusted = dict(weights)
    gold_codes = [
        code for code in adjusted if metas.get(code) and metas[code].category == "gold"
    ]
    gold_weight = sum(adjusted[code] for code in gold_codes)
    if gold_weight <= gold_cap:
        return adjusted, True
    scale = gold_cap / gold_weight if gold_weight > 0.0 else 0.0
    excess = gold_weight - gold_cap
    for code in gold_codes:
        adjusted[code] *= scale
    bond_codes = [
        code for code in adjusted if metas.get(code) and metas[code].category == "bond"
    ]
    bond_weight = sum(adjusted[code] for code in bond_codes)
    if bond_weight > 0.0:
        for code in bond_codes:
            adjusted[code] += excess * adjusted[code] / bond_weight
    return adjusted, True


class DefensiveWeightSchedule:
    """Cache selections on a phase-neutral fixed trading-day cadence."""

    def __init__(
        self,
        metas: dict[str, DefensiveEtfMeta],
        series: dict[str, list[tuple[date, float]]],
        policy: DefensivePolicy,
        selection_cache: dict[tuple[str, date], dict[str, float]] | None = None,
    ) -> None:
        self.metas = metas
        self.series = series
        self.policy = policy
        self.selection_cache = selection_cache if selection_cache is not None else {}
        self._weights: dict[str, float] = {}
        self._days_since_refresh = policy.refresh_days

    def weights_for(self, snapshot: date) -> dict[str, float]:
        if self._days_since_refresh >= self.policy.refresh_days:
            key = (self.policy.name, snapshot)
            if key not in self.selection_cache:
                self.selection_cache[key] = select_defensive_weights(
                    self.metas,
                    self.series,
                    snapshot,
                    self.policy,
                )
            self._weights = self.selection_cache[key]
            self._days_since_refresh = 0
        self._days_since_refresh += 1
        return self._weights


def describe_universe(metas: dict[str, DefensiveEtfMeta]) -> dict[str, Any]:
    first_dates = [meta.first_trade_date for meta in metas.values()]
    return {
        "count": len(metas),
        "bond_count": sum(meta.category == "bond" for meta in metas.values()),
        "gold_count": sum(meta.category == "gold" for meta in metas.values()),
        "commodity_count": sum(meta.category == "commodity" for meta in metas.values()),
        "first_trade_date": min(first_dates).isoformat() if first_dates else None,
        "codes": sorted(metas),
    }
