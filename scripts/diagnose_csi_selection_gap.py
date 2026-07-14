#!/usr/bin/env python3
"""Diagnose CSI winner gaps and test ex-ante selection overlays.

The script compares the guarded CSI selection with each year's realized CSI
winners, then tests a small set of ranking overlays that only use information
available at the prior year end.  Realized returns are used only for validation
and walk-forward weight selection.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from backtest.scorecard_adapter import load_scorecard_inputs
from db.connection import get_connection

OUT_DIR = ROOT / "data" / "ml"
FEATURES_CSV = OUT_DIR / "industry_scorecard_vector_features.csv"
PRED_CSV = OUT_DIR / "news_vector_framework_predictions.csv"
HOLDINGS_CSV = OUT_DIR / "guarded_strategy_weighted_allocation_holdings.csv"
REPORT_JSON = OUT_DIR / "csi_selection_gap_report.json"
DIFF_CSV = OUT_DIR / "csi_selection_gap_2021_2025.csv"
OPT_YEARLY_CSV = OUT_DIR / "csi_selection_gap_optimized_yearly.csv"
OPT_HOLDINGS_CSV = OUT_DIR / "csi_selection_gap_optimized_holdings.csv"
CONSTITUENT_FEATURES_CSV = OUT_DIR / "csi_selection_constituent_features_2021_2025.csv"
REGIME_YEARLY_CSV = OUT_DIR / "csi_regime_aware_selection_yearly.csv"
REGIME_HOLDINGS_CSV = OUT_DIR / "csi_regime_aware_selection_holdings.csv"

YEARS = list(range(2021, 2026))
TOP_K = 5
CURRENT_GUARDED_WEIGHTS = {
    "vector_ml_score_rank": 0.20,
    "base_ml_score_rank": 0.10,
    "momentum_12m_rank_rank": 0.20,
    "momentum_6m_rank_rank": 0.50,
}


def rank01(s: pd.Series, *, ascending: bool = True) -> pd.Series:
    vals = pd.to_numeric(s, errors="coerce")
    out = vals.rank(pct=True, ascending=ascending)
    return out.fillna(0.5)


def current_guarded_score(data: pd.DataFrame) -> pd.Series:
    out = pd.Series(0.0, index=data.index)
    data = data.copy()
    for col in ["vector_ml_score", "base_ml_score", "momentum_12m_rank", "momentum_6m_rank"]:
        data[f"{col}_rank"] = data.groupby("apply_year")[col].transform(rank01)
    for col, weight in CURRENT_GUARDED_WEIGHTS.items():
        out = out + weight * pd.to_numeric(data[col], errors="coerce").fillna(0.5)
    return out


def load_base_data() -> pd.DataFrame:
    pred = pd.read_csv(PRED_CSV)
    features = pd.read_csv(FEATURES_CSV)
    keep = [
        "apply_year",
        "ts_code",
        "momentum_6m",
        "momentum_6m_rank",
        "momentum_12m",
        "momentum_12m_rank",
        "reversal_1m",
        "vol_6m",
        "max_drawdown_12m",
        "pb_pct",
        "pb_value",
        "pe_ttm",
        "turnover_rate",
        "theme_duration",
        "heat_penalty",
        "index_scorecard",
        "vector_event_count",
        "vector_novelty_score",
        "vector_duplicate_density",
        "vector_semantic_breadth",
        "vector_source_diversity",
        "vector_similar_excess",
        "vector_theme_strength",
        "bench_return",
    ]
    data = pred.merge(features[keep], on=["apply_year", "ts_code"], how="left")
    data = data[data["apply_year"].isin(YEARS)].copy()
    data["current_guarded_score"] = current_guarded_score(data)
    return data.reset_index(drop=True)


def chunked(items: list[str], n: int = 800):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def load_constituent_features(conn, data: pd.DataFrame) -> pd.DataFrame:
    if CONSTITUENT_FEATURES_CSV.exists():
        cached = pd.read_csv(CONSTITUENT_FEATURES_CSV)
        years = set(int(y) for y in cached.get("apply_year", []))
        codes = set(cached.get("ts_code", []))
        need_years = set(YEARS)
        need_codes = set(data["ts_code"].dropna().unique().tolist())
        if need_years.issubset(years) and need_codes.issubset(codes):
            return cached[cached["apply_year"].isin(YEARS)].copy()

    records: list[dict[str, Any]] = []
    codes = sorted(data["ts_code"].dropna().unique().tolist())
    for year in YEARS:
        cutoff = date(year - 1, 12, 31)
        start = cutoff - timedelta(days=430)
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(codes))
            cur.execute(
                f"""
                SELECT index_code, MAX(trade_date)
                FROM index_constituent
                WHERE index_code IN ({placeholders}) AND trade_date <= %s
                GROUP BY index_code
                """,
                codes + [cutoff],
            )
            latest_dates = {idx: td for idx, td in cur.fetchall()}

        constituents: dict[str, list[tuple[str, float]]] = {}
        for idx, td in latest_dates.items():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT con_code, COALESCE(weight, 0)
                    FROM index_constituent
                    WHERE index_code=%s AND trade_date=%s
                    """,
                    (idx, td),
                )
                rows = [(str(c), float(w or 0.0)) for c, w in cur.fetchall()]
                total = sum(max(w, 0.0) for _c, w in rows)
                if total > 0:
                    constituents[idx] = [(c, max(w, 0.0) / total) for c, w in rows]
                elif rows:
                    constituents[idx] = [(c, 1.0 / len(rows)) for c, _w in rows]

        stock_codes = sorted({c for rows in constituents.values() for c, _w in rows})
        stock_rows: dict[str, list[tuple[date, float, float | None, float | None, float | None, float | None]]] = defaultdict(list)
        for batch in chunked(stock_codes):
            placeholders = ",".join(["%s"] * len(batch))
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT ts_code, trade_date, close, total_mv, circ_mv, pb, pe_ttm
                    FROM stock_daily_basic
                    WHERE ts_code IN ({placeholders})
                      AND trade_date BETWEEN %s AND %s
                    ORDER BY ts_code, trade_date
                    """,
                    batch + [start, cutoff],
                )
                for ts, td, close, total_mv, circ_mv, pb, pe_ttm in cur.fetchall():
                    stock_rows[str(ts)].append(
                        (
                            td,
                            float(close) if close is not None else math.nan,
                            float(total_mv) if total_mv is not None else None,
                            float(circ_mv) if circ_mv is not None else None,
                            float(pb) if pb is not None else None,
                            float(pe_ttm) if pe_ttm is not None else None,
                        )
                    )

        def ret_at(rows: list[tuple[date, float, Any, Any, Any, Any]], days: int) -> float | None:
            pts = [(d, c) for d, c, *_rest in rows if d <= cutoff and math.isfinite(c) and c > 0]
            if len(pts) < 2:
                return None
            end = pts[-1][1]
            target = cutoff - timedelta(days=days)
            prev = [p for p in pts if p[0] <= target]
            if not prev:
                return None
            start_px = prev[-1][1]
            if start_px <= 0:
                return None
            return end / start_px - 1.0

        def latest_fund(rows: list[tuple[date, float, Any, Any, Any, Any]], idx: int) -> float | None:
            vals = [r[idx] for r in rows if r[0] <= cutoff and r[idx] is not None]
            return vals[-1] if vals else None

        for idx in codes:
            cons = constituents.get(idx, [])
            if not cons:
                records.append({"apply_year": year, "ts_code": idx})
                continue
            weights = [w for _c, w in cons]
            hhi = sum(w * w for w in weights)
            top_weights = sorted(weights, reverse=True)
            mom6_vals = []
            mom12_vals = []
            pos6 = 0.0
            pos12 = 0.0
            total_mv = []
            circ_mv = []
            pb_vals = []
            pe_vals = []
            for con, w in cons:
                rows = stock_rows.get(con, [])
                r6 = ret_at(rows, 183)
                r12 = ret_at(rows, 365)
                if r6 is not None:
                    mom6_vals.append((w, r6))
                    if r6 > 0:
                        pos6 += w
                if r12 is not None:
                    mom12_vals.append((w, r12))
                    if r12 > 0:
                        pos12 += w
                for holder, value in [
                    (total_mv, latest_fund(rows, 2)),
                    (circ_mv, latest_fund(rows, 3)),
                    (pb_vals, latest_fund(rows, 4)),
                    (pe_vals, latest_fund(rows, 5)),
                ]:
                    if value is not None and math.isfinite(float(value)) and float(value) > 0:
                        holder.append((w, float(value)))

            def wav(vals: list[tuple[float, float]]) -> float | None:
                den = sum(w for w, _v in vals)
                return None if den <= 0 else sum(w * v for w, v in vals) / den

            records.append(
                {
                    "apply_year": year,
                    "ts_code": idx,
                    "constituent_count": len(cons),
                    "constituent_top1_weight": top_weights[0] if top_weights else None,
                    "constituent_top5_weight": sum(top_weights[:5]),
                    "constituent_hhi": hhi,
                    "constituent_effective_n": 1.0 / hhi if hhi > 0 else None,
                    "constituent_mom6": wav(mom6_vals),
                    "constituent_mom12": wav(mom12_vals),
                    "constituent_breadth6": pos6,
                    "constituent_breadth12": pos12,
                    "constituent_total_mv": wav(total_mv),
                    "constituent_circ_mv": wav(circ_mv),
                    "constituent_pb": wav(pb_vals),
                    "constituent_pe_ttm": wav(pe_vals),
                    "constituent_coverage": len(mom12_vals) / len(cons) if cons else 0.0,
                }
            )
    df = pd.DataFrame(records)
    df.to_csv(CONSTITUENT_FEATURES_CSV, index=False)
    return df


def add_rank_features(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    rank_specs = {
        "current_rank": ("current_guarded_score", True),
        "rule_rank": ("rule_score", True),
        "idx_mom6_rank": ("momentum_6m", True),
        "idx_mom12_rank": ("momentum_12m", True),
        "idx_reversal_rank": ("reversal_1m", False),
        "low_vol_rank": ("vol_6m", False),
        "low_dd_rank": ("max_drawdown_12m", True),
        "cheap_pb_rank": ("pb_value", False),
        "index_scorecard_rank": ("index_scorecard", True),
        "theme_strength_rank": ("vector_theme_strength", True),
        "theme_novelty_rank": ("vector_novelty_score", True),
        "comp_mom6_rank": ("constituent_mom6", True),
        "comp_mom12_rank": ("constituent_mom12", True),
        "comp_breadth6_rank": ("constituent_breadth6", True),
        "comp_breadth12_rank": ("constituent_breadth12", True),
        "comp_effective_n_rank": ("constituent_effective_n", True),
        "comp_mv_rank": ("constituent_total_mv", True),
        "comp_low_pb_rank": ("constituent_pb", False),
    }
    for out_col, (src, asc) in rank_specs.items():
        data[out_col] = data.groupby("apply_year")[src].transform(lambda s, asc=asc: rank01(s, ascending=asc))
    return data


def add_style_and_regime_scores(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    names = data["index_name"].fillna("")
    data["defensive_style"] = names.str.contains("红利|低波|黄金|油气|银行|高股息|价值|公用|运输|央企", regex=True).astype(float)
    data["growth_style"] = names.str.contains("通信|云计算|人工智能|数字|电信|5G|卫星|半导|电子|信创|软件|游戏|动漫|VR", regex=True).astype(float)
    data["defensive_style_rank"] = data.groupby("apply_year")["defensive_style"].transform(lambda s: rank01(s, ascending=True))
    data["growth_style_rank"] = data.groupby("apply_year")["growth_style"].transform(lambda s: rank01(s, ascending=True))
    data["oversold12_rank"] = data.groupby("apply_year")["momentum_12m"].transform(lambda s: rank01(s, ascending=False))
    data["defensive_score"] = (
        0.25 * data["low_vol_rank"]
        + 0.25 * data["low_dd_rank"]
        + 0.20 * data["idx_mom12_rank"]
        + 0.20 * data["defensive_style_rank"]
        + 0.10 * data["cheap_pb_rank"]
    )
    data["policy_recovery_score"] = (
        0.40 * data["rule_rank"]
        + 0.30 * data["oversold12_rank"]
        + 0.20 * data["growth_style_rank"]
        + 0.10 * data["theme_novelty_rank"]
    )
    data["growth_confirm_score"] = (
        0.35 * data["current_rank"]
        + 0.25 * data["rule_rank"]
        + 0.20 * data["idx_mom12_rank"]
        + 0.10 * data["comp_mom12_rank"]
        + 0.10 * data["growth_style_rank"]
    )
    return data


def macro_regime(conn, year: int) -> tuple[str, dict[str, Any]]:
    inp = load_scorecard_inputs(date(year - 1, 12, 31), conn=conn)
    pmi_weak_months = int(getattr(inp, "pmi_below_52_months", 0) or 0)
    ppi = float(getattr(inp, "ppi_yoy", 0.0) or 0.0)
    us10y = float(getattr(inp, "us10y_chg_12m_bp", 0.0) or 0.0)
    cs300_6m = float(getattr(inp, "cs300_6m_return", 0.0) or 0.0)
    rate_bp = float(getattr(inp, "rate_cum_bp_12m", 0.0) or 0.0)
    pboc_tone = getattr(inp, "pboc_tone", None)
    values = {
        "pmi_below_52_months": pmi_weak_months,
        "ppi_yoy": ppi,
        "us10y_chg_12m_bp": us10y,
        "cs300_6m_return": cs300_6m,
        "rate_cum_bp_12m": rate_bp,
        "pboc_tone": pboc_tone,
    }
    if pmi_weak_months >= 6 and ppi >= 5.0 and us10y >= 50.0 and cs300_6m < 0:
        return "stagflation_defensive", values
    if pmi_weak_months >= 10 and ppi <= 0.0 and us10y >= 100.0 and cs300_6m < 0:
        return "policy_recovery", values
    if (pboc_tone == "loose" or rate_bp <= -50.0) and cs300_6m > 0:
        return "liquidity_growth", values
    return "base_current", values


def select_regime_aware(year_df: pd.DataFrame, regime: str) -> pd.DataFrame:
    if regime == "stagflation_defensive":
        return year_df.sort_values("defensive_score", ascending=False).head(TOP_K)
    if regime == "policy_recovery":
        return year_df.sort_values("policy_recovery_score", ascending=False).head(TOP_K)
    if regime == "liquidity_growth":
        selected = []
        used: set[str] = set()
        for _idx, row in year_df.sort_values("current_guarded_score", ascending=False).head(3).iterrows():
            selected.append(row)
            used.add(row["ts_code"])
        for _idx, row in year_df.sort_values("growth_confirm_score", ascending=False).iterrows():
            if row["ts_code"] in used:
                continue
            selected.append(row)
            used.add(row["ts_code"])
            if len(selected) >= TOP_K:
                break
        return pd.DataFrame(selected)
    return year_df.sort_values("current_guarded_score", ascending=False).head(TOP_K)


def evaluate_regime_aware(
    conn,
    data: pd.DataFrame,
    winners_by_year: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    yearly: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    for year, g in data.groupby("apply_year"):
        regime, macro_values = macro_regime(conn, int(year))
        selected = select_regime_aware(g.copy(), regime)
        winners = {w["ts_code"] for w in winners_by_year[int(year)]}
        ret = float(selected["target_return"].mean())
        bench = float(selected["bench_return"].dropna().iloc[0])
        yearly.append(
            {
                "year": int(year),
                "regime": regime,
                "strategy_return": ret,
                "benchmark_return": bench,
                "excess_return": ret - bench,
                "winner_hit": len(set(selected["ts_code"]) & winners),
                "macro": json.dumps(macro_values, ensure_ascii=False, sort_keys=True),
                "selected_codes": "|".join(selected["ts_code"].tolist()),
                "selected_names": "|".join(selected["index_name"].tolist()),
            }
        )
        for rank, row in enumerate(selected.itertuples(index=False), 1):
            score = (
                row.defensive_score
                if regime == "stagflation_defensive"
                else row.policy_recovery_score
                if regime == "policy_recovery"
                else row.growth_confirm_score
                if regime == "liquidity_growth"
                else row.current_guarded_score
            )
            holdings.append(
                {
                    "year": int(year),
                    "regime": regime,
                    "rank": rank,
                    "ts_code": row.ts_code,
                    "index_name": row.index_name,
                    "best_theme": row.best_theme,
                    "selection_score": float(score),
                    "target_return": float(row.target_return),
                }
            )
    summary = {
        "mean_strategy_return": statistics.mean(r["strategy_return"] for r in yearly),
        "mean_excess_return": statistics.mean(r["excess_return"] for r in yearly),
        "worst_strategy_return": min(r["strategy_return"] for r in yearly),
        "total_winner_hit": sum(r["winner_hit"] for r in yearly),
    }
    return {"yearly": yearly, "summary": summary, "holdings": holdings}


def component_vectors(conn, codes: list[str], year: int) -> dict[str, dict[str, float]]:
    cutoff = date(year, 12, 31)
    out: dict[str, dict[str, float]] = {}
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(codes))
        cur.execute(
            f"""
            SELECT index_code, MAX(trade_date)
            FROM index_constituent
            WHERE index_code IN ({placeholders}) AND trade_date <= %s
            GROUP BY index_code
            """,
            codes + [cutoff],
        )
        dates = {idx: td for idx, td in cur.fetchall()}
    for idx, td in dates.items():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT con_code, COALESCE(weight, 0)
                FROM index_constituent
                WHERE index_code=%s AND trade_date=%s
                """,
                (idx, td),
            )
            rows = [(str(c), float(w or 0.0)) for c, w in cur.fetchall()]
        total = sum(max(w, 0.0) for _c, w in rows)
        if total <= 0 and rows:
            total = float(len(rows))
            out[idx] = {c: 1.0 / total for c, _w in rows}
        elif total > 0:
            out[idx] = {c: max(w, 0.0) / total for c, w in rows}
    return out


def similarity(a: dict[str, float], b: dict[str, float]) -> tuple[float, float]:
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    cos = dot / (na * nb) if na > 0 and nb > 0 else 0.0
    smaller = min(sum(a.values()), sum(b.values()))
    overlap = sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys) / smaller if smaller > 0 else 0.0
    return cos, overlap


def dedup_top_winners(conn, year_df: pd.DataFrame, year: int, k: int = TOP_K) -> list[dict[str, Any]]:
    ordered = year_df.dropna(subset=["target_return"]).sort_values("target_return", ascending=False)
    vectors = component_vectors(conn, ordered["ts_code"].head(40).tolist(), year)
    winners: list[dict[str, Any]] = []
    for row in ordered.itertuples(index=False):
        vec = vectors.get(row.ts_code, {})
        too_similar = False
        for win in winners:
            cos, overlap = similarity(vec, vectors.get(win["ts_code"], {}))
            if cos >= 0.75 or overlap >= 0.70:
                too_similar = True
                break
        if too_similar:
            continue
        winners.append(
            {
                "year": int(year),
                "ts_code": row.ts_code,
                "index_name": row.index_name,
                "return": float(row.target_return),
            }
        )
        if len(winners) >= k:
            break
    return winners


def make_score(data: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    score = pd.Series(0.0, index=data.index)
    for col, weight in weights.items():
        score = score + float(weight) * pd.to_numeric(data[col], errors="coerce").fillna(0.5)
    return score


def evaluate_selection(data: pd.DataFrame, score_col: str, winners_by_year: dict[int, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    yearly: list[dict[str, Any]] = []
    for year, g in data.groupby("apply_year"):
        selected = g.sort_values(score_col, ascending=False).head(TOP_K)
        winners = winners_by_year[int(year)]
        winner_codes = {w["ts_code"] for w in winners}
        hit = len(set(selected["ts_code"]) & winner_codes)
        ret = float(selected["target_return"].mean())
        bench = float(selected["bench_return"].dropna().iloc[0])
        yearly.append(
            {
                "year": int(year),
                "strategy_return": ret,
                "benchmark_return": bench,
                "excess_return": ret - bench,
                "winner_hit": hit,
                "selected_codes": "|".join(selected["ts_code"].tolist()),
                "selected_names": "|".join(selected["index_name"].tolist()),
            }
        )
    summary = {
        "mean_strategy_return": statistics.mean(r["strategy_return"] for r in yearly),
        "mean_excess_return": statistics.mean(r["excess_return"] for r in yearly),
        "worst_strategy_return": min(r["strategy_return"] for r in yearly),
        "total_winner_hit": sum(r["winner_hit"] for r in yearly),
    }
    return yearly, summary


def objective(summary: dict[str, Any]) -> float:
    return (
        float(summary["mean_excess_return"])
        + 0.35 * float(summary["worst_strategy_return"])
        + 0.01 * float(summary["total_winner_hit"])
    )


def weight_grid() -> list[dict[str, float]]:
    components = [
        "current_rank",
        "rule_rank",
        "idx_mom6_rank",
        "idx_mom12_rank",
        "comp_mom6_rank",
        "comp_mom12_rank",
        "comp_breadth12_rank",
        "comp_effective_n_rank",
        "theme_novelty_rank",
    ]
    combos: list[dict[str, float]] = []
    # Coarse 20% grid keeps this diagnostic script fast.  Fine grids are too
    # easy to overfit on five annual observations and should be run separately.
    total_units = 5

    def rec(prefix: list[int], remaining: int, slots: int):
        if slots == 1:
            yield prefix + [remaining]
            return
        for v in range(remaining + 1):
            yield from rec(prefix + [v], remaining - v, slots - 1)

    for ints in rec([], total_units, len(components)):
        vals = [v / total_units for v in ints]
        w = dict(zip(components, vals))
        if w["current_rank"] < 0.20:
            continue
        if w["current_rank"] > 0.80:
            continue
        if w.get("rule_rank", 0.0) > 0.40:
            continue
        combos.append({k: v for k, v in w.items() if v > 0})
    return combos


def optimize_full_sample(data: pd.DataFrame, winners_by_year: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    for weights in weight_grid():
        data["_candidate_score"] = make_score(data, weights)
        yearly, summary = evaluate_selection(data, "_candidate_score", winners_by_year)
        score = objective(summary)
        item = {"weights": weights, "yearly": yearly, "summary": summary, "objective": score}
        if best is None or score > float(best["objective"]):
            best = item
    assert best is not None
    return best


def optimize_walk_forward(data: pd.DataFrame, winners_by_year: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    default_weights = {"current_rank": 0.50, "rule_rank": 0.25, "idx_mom6_rank": 0.15, "comp_mom12_rank": 0.10}
    all_holdings: list[dict[str, Any]] = []
    yearly: list[dict[str, Any]] = []
    chosen_weights: dict[int, dict[str, float]] = {}
    for year in YEARS:
        train_years = [y for y in YEARS if y < year]
        if len(train_years) < 2:
            weights = default_weights
        else:
            train = data[data["apply_year"].isin(train_years)].copy()
            best: dict[str, Any] | None = None
            for candidate in weight_grid():
                train["_candidate_score"] = make_score(train, candidate)
                _yr, summary = evaluate_selection(train, "_candidate_score", winners_by_year)
                score = objective(summary)
                if best is None or score > float(best["objective"]):
                    best = {"weights": candidate, "objective": score}
            weights = best["weights"] if best else default_weights
        chosen_weights[year] = weights
        test = data[data["apply_year"] == year].copy()
        test["optimized_score"] = make_score(test, weights)
        selected = test.sort_values("optimized_score", ascending=False).head(TOP_K)
        winners = {w["ts_code"] for w in winners_by_year[year]}
        ret = float(selected["target_return"].mean())
        bench = float(selected["bench_return"].dropna().iloc[0])
        yearly.append(
            {
                "year": year,
                "strategy_return": ret,
                "benchmark_return": bench,
                "excess_return": ret - bench,
                "winner_hit": len(set(selected["ts_code"]) & winners),
                "weights": json.dumps(weights, ensure_ascii=False, sort_keys=True),
                "selected_codes": "|".join(selected["ts_code"].tolist()),
                "selected_names": "|".join(selected["index_name"].tolist()),
            }
        )
        for rank, row in enumerate(selected.itertuples(index=False), 1):
            all_holdings.append(
                {
                    "year": year,
                    "rank": rank,
                    "ts_code": row.ts_code,
                    "index_name": row.index_name,
                    "best_theme": row.best_theme,
                    "optimized_score": float(row.optimized_score),
                    "target_return": float(row.target_return),
                }
            )
    summary = {
        "mean_strategy_return": statistics.mean(r["strategy_return"] for r in yearly),
        "mean_excess_return": statistics.mean(r["excess_return"] for r in yearly),
        "worst_strategy_return": min(r["strategy_return"] for r in yearly),
        "total_winner_hit": sum(r["winner_hit"] for r in yearly),
    }
    return {"yearly": yearly, "summary": summary, "holdings": all_holdings, "chosen_weights": chosen_weights}


def build_gap_rows(
    data: pd.DataFrame,
    winners_by_year: dict[int, list[dict[str, Any]]],
    current_holdings: pd.DataFrame,
    optimized_holdings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    optimized_by_year = defaultdict(list)
    for row in optimized_holdings:
        optimized_by_year[int(row["year"])].append(row)
    rows: list[dict[str, Any]] = []
    for year in YEARS:
        cur = current_holdings[current_holdings["year"] == year].sort_values("rank")
        opt = optimized_by_year[year]
        winners = winners_by_year[year]
        year_df = data[data["apply_year"] == year].copy()
        rank_by_current = {
            row.ts_code: i
            for i, row in enumerate(year_df.sort_values("current_guarded_score", ascending=False).itertuples(index=False), 1)
        }
        for bucket, items in [
            ("realized_winner", winners),
            ("current_selected", cur.to_dict("records")),
            ("optimized_selected", opt),
        ]:
            for i, item in enumerate(items, 1):
                ts = item["ts_code"]
                feature = year_df[year_df["ts_code"] == ts]
                f = feature.iloc[0].to_dict() if not feature.empty else {}
                rows.append(
                    {
                        "year": year,
                        "bucket": bucket,
                        "rank": i,
                        "ts_code": ts,
                        "index_name": item.get("index_name", ""),
                        "return": item.get("return", item.get("target_return", item.get("realized_return"))),
                        "current_rank_position": rank_by_current.get(ts),
                        "current_score": f.get("current_guarded_score"),
                        "idx_mom6": f.get("momentum_6m"),
                        "idx_mom12": f.get("momentum_12m"),
                        "comp_mom12": f.get("constituent_mom12"),
                        "comp_breadth12": f.get("constituent_breadth12"),
                        "constituent_hhi": f.get("constituent_hhi"),
                        "theme_novelty": f.get("vector_novelty_score"),
                        "best_theme": f.get("best_theme", item.get("best_theme", "")),
                    }
                )
    return rows


def main() -> int:
    data = load_base_data()
    conn = get_connection()
    cons = load_constituent_features(conn, data)
    data = add_style_and_regime_scores(add_rank_features(data.merge(cons, on=["apply_year", "ts_code"], how="left")))
    winners_by_year = {year: dedup_top_winners(conn, data[data["apply_year"] == year], year) for year in YEARS}

    current_holdings = pd.read_csv(HOLDINGS_CSV)
    current_holdings = current_holdings[current_holdings["year"].isin(YEARS)].copy()
    current_yearly, current_summary = evaluate_selection(data, "current_guarded_score", winners_by_year)
    full_best = optimize_full_sample(data, winners_by_year)
    walk = optimize_walk_forward(data, winners_by_year)
    regime = evaluate_regime_aware(conn, data, winners_by_year)
    conn.close()

    gap_rows = build_gap_rows(data, winners_by_year, current_holdings, walk["holdings"])
    with DIFF_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(gap_rows[0].keys()))
        writer.writeheader()
        writer.writerows(gap_rows)
    with OPT_YEARLY_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(walk["yearly"][0].keys()))
        writer.writeheader()
        writer.writerows(walk["yearly"])
    with OPT_HOLDINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(walk["holdings"][0].keys()))
        writer.writeheader()
        writer.writerows(walk["holdings"])
    with REGIME_YEARLY_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(regime["yearly"][0].keys()))
        writer.writeheader()
        writer.writerows(regime["yearly"])
    with REGIME_HOLDINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(regime["holdings"][0].keys()))
        writer.writeheader()
        writer.writerows(regime["holdings"])

    report = {
        "years": YEARS,
        "top_k": TOP_K,
        "no_lookahead_rule": "features use data available by prior year end; realized returns only validate or select weights from earlier validation years",
        "current_summary": current_summary,
        "full_sample_diagnostic_best": {
            "weights": full_best["weights"],
            "summary": full_best["summary"],
            "yearly": full_best["yearly"],
        },
        "walk_forward_optimized": {
            "summary": walk["summary"],
            "yearly": walk["yearly"],
            "chosen_weights": {str(k): v for k, v in walk["chosen_weights"].items()},
        },
        "regime_aware_selection": {
            "summary": regime["summary"],
            "yearly": regime["yearly"],
            "rules": {
                "stagflation_defensive": "PMI weak months >=6, PPI >=5, US10Y 12M change >=50bp, CSI300 6M return <0; rank by low vol, low drawdown, positive 12M, defensive style and cheap PB",
                "policy_recovery": "PMI weak months >=10, PPI <=0, US10Y 12M change >=100bp, CSI300 6M return <0; rank by rule strength plus 12M oversold recovery and growth style",
                "liquidity_growth": "PBOC loose or domestic rate cuts <=-50bp, and CSI300 6M return >0; keep top3 current plus top2 growth-confirmation candidates",
                "base_current": "use current guarded score",
            },
        },
        "winners_by_year": winners_by_year,
        "gap_csv": str(DIFF_CSV),
        "optimized_yearly_csv": str(OPT_YEARLY_CSV),
        "optimized_holdings_csv": str(OPT_HOLDINGS_CSV),
        "regime_yearly_csv": str(REGIME_YEARLY_CSV),
        "regime_holdings_csv": str(REGIME_HOLDINGS_CSV),
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def pct(v: Any) -> str:
        return "N/A" if v is None else f"{float(v) * 100:.1f}%"

    print("CSI Selection Gap Diagnosis")
    print("Current:")
    print(
        f"  mean={pct(current_summary['mean_strategy_return'])} "
        f"excess={pct(current_summary['mean_excess_return'])} "
        f"worst={pct(current_summary['worst_strategy_return'])} "
        f"winner_hits={current_summary['total_winner_hit']}"
    )
    print("Walk-forward optimized:")
    ws = walk["summary"]
    print(
        f"  mean={pct(ws['mean_strategy_return'])} "
        f"excess={pct(ws['mean_excess_return'])} "
        f"worst={pct(ws['worst_strategy_return'])} "
        f"winner_hits={ws['total_winner_hit']}"
    )
    print("Regime-aware selection:")
    rs = regime["summary"]
    print(
        f"  mean={pct(rs['mean_strategy_return'])} "
        f"excess={pct(rs['mean_excess_return'])} "
        f"worst={pct(rs['worst_strategy_return'])} "
        f"winner_hits={rs['total_winner_hit']}"
    )
    print("Full-sample diagnostic best:")
    fs = full_best["summary"]
    print(f"  weights={full_best['weights']}")
    print(
        f"  mean={pct(fs['mean_strategy_return'])} "
        f"excess={pct(fs['mean_excess_return'])} "
        f"worst={pct(fs['worst_strategy_return'])} "
        f"winner_hits={fs['total_winner_hit']}"
    )
    print("\nYearly winners vs current vs optimized:")
    for year in YEARS:
        winners = ", ".join(f"{w['index_name']} {pct(w['return'])}" for w in winners_by_year[year])
        cur = next(r for r in current_yearly if r["year"] == year)
        opt = next(r for r in walk["yearly"] if r["year"] == year)
        print(f"  {year}: winners=[{winners}]")
        print(f"    current={pct(cur['strategy_return'])} hits={cur['winner_hit']} {cur['selected_names']}")
        print(f"    optimized={pct(opt['strategy_return'])} hits={opt['winner_hit']} {opt['selected_names']}")
        reg = next(r for r in regime["yearly"] if r["year"] == year)
        print(f"    regime={reg['regime']} {pct(reg['strategy_return'])} hits={reg['winner_hit']} {reg['selected_names']}")
    print(f"\nWrote {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
