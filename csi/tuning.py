"""CSI ranking parameter tuning and evaluation."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, asdict
from datetime import date
from typing import Any

import pymysql

from csi.enhanced import (
    MOMENTUM_HEAT_WEIGHT,
    apply_enhancements,
    dedupe_by_correlation,
    heat_penalty,
)
from csi.index_scorecard import compute_index_scorecard
from csi.ranking import rank_indices, year_as_of


@dataclass
class RankConfig:
    news_weight: float = 0.15
    policy_weight: float = 0.35
    momentum_weight: float = 0.30
    valuation_weight: float = 0.20
    heat_weight: float = 0.20
    scorecard_weight: float = 0.10
    use_duration: bool = True
    use_heat: bool = True
    use_scorecard: bool = False
    use_corr_dedup: bool = False
    corr_top_n: int = 30

    def weights(self, has_news: bool) -> dict[str, float]:
        if not has_news:
            tw = self.news_weight
            pw = self.policy_weight + tw
            return {
                "policy": pw,
                "news": 0.0,
                "momentum": self.momentum_weight,
                "valuation": self.valuation_weight,
            }
        total = self.policy_weight + self.news_weight + self.momentum_weight + self.valuation_weight
        if total <= 0:
            total = 1.0
        return {
            "policy": self.policy_weight / total,
            "news": self.news_weight / total,
            "momentum": self.momentum_weight / total,
            "valuation": self.valuation_weight / total,
        }


def rank_with_config(
    conn: pymysql.connections.Connection,
    apply_year: int,
    cfg: RankConfig,
    *,
    load_signals,
    load_news,
    load_theme_map,
    load_prices,
    load_valuations,
) -> list[dict]:
    from csi.enhanced import duration_multiplier, get_theme_duration

    as_of = year_as_of(apply_year)
    cutoff = date(apply_year - 1, 12, 31)
    signals = load_signals(conn, apply_year, as_of)
    news = load_news(conn, apply_year)
    has_news = len(news) > 0
    theme_map = load_theme_map(conn, "CSI")
    prices = load_prices(conn, "CSI", apply_year)
    vals = load_valuations(conn, "CSI", apply_year)
    rows = rank_indices(
        signals=signals,
        news=news if has_news else {},
        theme_map=theme_map,
        price_data=prices,
        val_data=vals,
        as_of=cutoff,
        suffix="CSI",
        has_news=has_news,
        weights=cfg.weights(has_news),
    )
    price_closes = {ts: [c for _, c in ser] for ts, ser in prices.items()}
    theme_dur: dict[str, int] = {}

    for row in rows:
        if cfg.use_duration and row.get("best_theme"):
            t = row["best_theme"]
            if t not in theme_dur:
                theme_dur[t] = get_theme_duration(conn, t, apply_year)
            dm = duration_multiplier(theme_dur[t])
            row["final_score"] = row.get("final_score", 0) * (0.7 + 0.3 * dm / 2.0)
        if cfg.use_scorecard:
            sc = compute_index_scorecard(conn, row["ts_code"], prices.get(row["ts_code"], []), cutoff)
            row["final_score"] = row.get("final_score", 0) + cfg.scorecard_weight * sc
        if cfg.use_heat:
            h = heat_penalty(price_closes.get(row["ts_code"], []))
            row["final_score"] = row.get("final_score", 0) + cfg.heat_weight * h

    rows.sort(key=lambda r: -r["final_score"])
    if cfg.use_corr_dedup:
        rows = dedupe_by_correlation(rows, price_closes, cfg.corr_top_n)
    return rows


def eval_config_on_years(
    conn: pymysql.connections.Connection,
    cfg: RankConfig,
    years: list[int],
    *,
    load_signals,
    load_news,
    load_theme_map,
    load_prices,
    load_valuations,
    forward_return,
    spearman,
) -> dict[str, Any]:
    from datetime import date as dt

    rhos, spreads, excesses = [], [], []
    ytd_excesses = []
    for year in years:
        news = load_news(conn, year)
        if not news and cfg.news_weight > 0:
            continue
        rows = rank_with_config(
            conn, year, cfg,
            load_signals=load_signals,
            load_news=load_news,
            load_theme_map=load_theme_map,
            load_prices=load_prices,
            load_valuations=load_valuations,
        )
        if not rows:
            continue
        start = dt(year, 1, 5)
        end = dt(year, 12, 31)
        pairs = []
        for row in rows:
            ret = forward_return(conn, row["ts_code"], start, end)
            if ret is not None:
                pairs.append((row["final_score"], ret))
        if len(pairs) < 10:
            continue
        scores, rets = zip(*pairs)
        rho = spearman(list(scores), list(rets))
        sorted_p = sorted(pairs, key=lambda p: -p[0])
        k = min(10, len(sorted_p) // 4)
        top_avg = statistics.mean([r for _, r in sorted_p[:k]])
        bot_avg = statistics.mean([r for _, r in sorted_p[-k:]])
        bench = forward_return(conn, "000300.SH", start, end)
        if rho is not None:
            rhos.append(rho)
        spreads.append(top_avg - bot_avg)
        if bench is not None:
            excesses.append(top_avg - bench)

        # YTD partial for current year
        if year >= dt.today().year:
            ytd_end = dt.today()
            top5 = rows[:5]
            t5_rets = [forward_return(conn, r["ts_code"], start, ytd_end) for r in top5]
            t5_rets = [x for x in t5_rets if x is not None]
            ytd_bench = forward_return(conn, "000300.SH", start, ytd_end)
            if t5_rets and ytd_bench is not None:
                ytd_excesses.append(statistics.mean(t5_rets) - ytd_bench)

    score = 0.0
    if rhos:
        score += statistics.mean(rhos) * 2.0
    if spreads:
        score += statistics.mean(spreads) * 3.0
    if excesses:
        score += statistics.mean(excesses) * 4.0
    if ytd_excesses:
        score += statistics.mean(ytd_excesses) * 5.0

    return {
        "config": asdict(cfg),
        "score": score,
        "mean_rho": statistics.mean(rhos) if rhos else None,
        "mean_spread": statistics.mean(spreads) if spreads else None,
        "mean_excess": statistics.mean(excesses) if excesses else None,
        "mean_ytd_excess": statistics.mean(ytd_excesses) if ytd_excesses else None,
        "n_years": len(rhos),
    }
