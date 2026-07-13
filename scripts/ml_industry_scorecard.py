#!/usr/bin/env python3
"""ML industry scorecard for annual CSI index ranking.

This is an experimental pipeline, separate from the production rule scorecard.
It builds an annual cross-sectional dataset (apply_year x CSI index), trains a
simple ridge ranker with walk-forward validation, and compares it with the
existing rule-based CSI ranking score.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from csi.enhanced import duration_multiplier, get_theme_duration, heat_penalty
from csi.index_scorecard import compute_index_scorecard
from csi.ranking import RELEVANCE_SCORE, STRENGTH_SCORE, rank_indices, year_as_of
from db.connection import get_connection
from scripts.rank_annual_csi import (
    load_news,
    load_signals,
    load_theme_map,
    load_valuations,
)
from scripts.validate_csi_rank import forward_return, spearman

OUT_DIR = ROOT / "data" / "ml"
FEATURES_CSV = OUT_DIR / "industry_scorecard_features.csv"
REPORT_JSON = OUT_DIR / "industry_scorecard_report.json"
PRED_CSV = OUT_DIR / "industry_scorecard_predictions.csv"


FEATURE_COLUMNS = [
    "rule_score",
    "policy_norm",
    "news_score",
    "news_abs",
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
    "pe_pct",
    "turnover_rate",
    "theme_duration",
    "duration_mult",
    "heat_penalty",
    "index_scorecard",
    "signal_strength_num",
    "relevance_num",
]


@dataclass
class RidgeModel:
    columns: list[str]
    mean: np.ndarray
    std: np.ndarray
    coef: np.ndarray
    alpha: float
    bounds: dict[str, tuple[float, float]]

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        x, _ = clean_matrix(df, self.columns, self.bounds)
        z = (x - self.mean) / self.std
        z = np.column_stack([np.ones(len(z)), z])
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            pred = z @ self.coef
        return np.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)


def pct_rank(values: dict[str, float | None], neutral: float = 0.5) -> dict[str, float]:
    valid = [(k, v) for k, v in values.items() if v is not None and math.isfinite(float(v))]
    if not valid:
        return {k: neutral for k in values}
    ordered = sorted(valid, key=lambda kv: float(kv[1]))
    denom = max(len(ordered) - 1, 1)
    ranks = {k: i / denom for i, (k, _v) in enumerate(ordered)}
    return {k: ranks.get(k, neutral) for k in values}


def latest_basic(conn, ts_codes: list[str], as_of: date) -> dict[str, dict[str, float | None]]:
    if not ts_codes:
        return {}
    placeholders = ",".join(["%s"] * len(ts_codes))
    params: list[Any] = list(ts_codes) + [as_of]
    out: dict[str, dict[str, float | None]] = {ts: {} for ts in ts_codes}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT b.ts_code, b.pe_ttm, b.pb, b.turnover_rate
            FROM index_dailybasic b
            JOIN (
                SELECT ts_code, MAX(trade_date) AS trade_date
                FROM index_dailybasic
                WHERE ts_code IN ({placeholders}) AND trade_date <= %s
                GROUP BY ts_code
            ) x ON x.ts_code=b.ts_code AND x.trade_date=b.trade_date
            """,
            params,
        )
        for ts, pe_ttm, pb, turnover in cur.fetchall():
            out[ts] = {
                "pe_ttm": float(pe_ttm) if pe_ttm is not None else None,
                "pb_value": float(pb) if pb is not None else None,
                "turnover_rate": float(turnover) if turnover is not None else None,
            }
    return out


def load_ml_prices(conn, suffix: str, apply_year: int) -> dict[str, list[tuple[date, float]]]:
    start = date(apply_year - 3, 1, 1)
    end = date(apply_year - 1, 12, 31)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, trade_date, close FROM index_daily
            WHERE ts_code LIKE %s AND trade_date BETWEEN %s AND %s
            ORDER BY ts_code, trade_date
            """,
            (f"%.{suffix}", start, end),
        )
        result: dict[str, list[tuple[date, float]]] = {}
        for ts, td, cl in cur.fetchall():
            if cl is None:
                continue
            result.setdefault(ts, []).append((td, float(cl)))
    return result


def compute_return_features(series: list[tuple[date, float]], as_of: date) -> dict[str, float | None]:
    pts = [(d, c) for d, c in series if d <= as_of and c and c > 0]
    if len(pts) < 20:
        return {
            "momentum_6m": None,
            "momentum_12m": None,
            "reversal_1m": None,
            "vol_6m": None,
            "max_drawdown_12m": None,
        }
    closes = np.array([c for _d, c in pts], dtype=float)

    def ret(days: int) -> float | None:
        if len(closes) <= days:
            return None
        return float(closes[-1] / closes[-days - 1] - 1.0)

    returns = np.diff(np.log(closes))
    vol_6m = float(np.std(returns[-125:]) * math.sqrt(252)) if len(returns) >= 60 else None
    lookback = closes[-252:] if len(closes) >= 60 else closes
    running_max = np.maximum.accumulate(lookback)
    drawdowns = lookback / running_max - 1.0
    max_dd = float(np.min(drawdowns)) if len(drawdowns) else None
    return {
        "momentum_6m": ret(125),
        "momentum_12m": ret(252),
        "reversal_1m": ret(21),
        "vol_6m": vol_6m,
        "max_drawdown_12m": max_dd,
    }


def build_year_rows(conn, apply_year: int) -> pd.DataFrame:
    cutoff = date(apply_year - 1, 12, 31)
    as_of = year_as_of(apply_year)
    signals = load_signals(conn, apply_year, as_of)
    news = load_news(conn, apply_year)
    theme_map = load_theme_map(conn, "CSI")
    prices = load_ml_prices(conn, "CSI", apply_year)
    vals = load_valuations(conn, "CSI", apply_year)
    rows = rank_indices(
        signals=signals,
        news=news,
        theme_map=theme_map,
        price_data=prices,
        val_data=vals,
        as_of=cutoff,
        suffix="CSI",
        min_signal="弱",
        has_news=bool(news),
    )
    if not rows:
        return pd.DataFrame()

    ts_codes = [r["ts_code"] for r in rows]
    basics = latest_basic(conn, ts_codes, cutoff)
    ret_features = {ts: compute_return_features(prices.get(ts, []), cutoff) for ts in ts_codes}
    mom6_rank = pct_rank({ts: ret_features[ts]["momentum_6m"] for ts in ts_codes})
    mom12_rank = pct_rank({ts: ret_features[ts]["momentum_12m"] for ts in ts_codes})
    pe_rank = pct_rank({ts: basics.get(ts, {}).get("pe_ttm") for ts in ts_codes})

    records: list[dict[str, Any]] = []
    bench = forward_return(conn, "000300.SH", date(apply_year, 1, 5), date(apply_year, 12, 31))
    for row in rows:
        ts = row["ts_code"]
        rf = ret_features[ts]
        basic = basics.get(ts, {})
        best_theme = row.get("best_theme") or ""
        strength = STRENGTH_SCORE.get(row.get("signal_strength") or "弱", 0)
        relevance = RELEVANCE_SCORE.get(row.get("relevance") or "弱", 1)
        dur = get_theme_duration(conn, best_theme, apply_year) if best_theme else 1
        target_return = forward_return(conn, ts, date(apply_year, 1, 5), date(apply_year, 12, 31))
        rec = {
            "apply_year": apply_year,
            "ts_code": ts,
            "index_name": row.get("index_name"),
            "best_theme": best_theme,
            "rule_score": float(row.get("final_score", 0.0)),
            "policy_norm": float(row.get("raw_policy", 0.0)) / 9.0,
            "news_score": float(row.get("raw_news", 0.0)),
            "news_abs": abs(float(row.get("raw_news", 0.0))),
            "momentum_6m": rf["momentum_6m"],
            "momentum_6m_rank": mom6_rank[ts],
            "momentum_12m": rf["momentum_12m"],
            "momentum_12m_rank": mom12_rank[ts],
            "reversal_1m": rf["reversal_1m"],
            "vol_6m": rf["vol_6m"],
            "max_drawdown_12m": rf["max_drawdown_12m"],
            "pb_pct": row.get("pb_pct"),
            "pb_value": basic.get("pb_value"),
            "pe_ttm": basic.get("pe_ttm"),
            "pe_pct": pe_rank[ts],
            "turnover_rate": basic.get("turnover_rate"),
            "theme_duration": float(dur),
            "duration_mult": duration_multiplier(dur),
            "heat_penalty": heat_penalty([c for _d, c in prices.get(ts, [])]),
            "index_scorecard": compute_index_scorecard(conn, ts, prices.get(ts, []), cutoff),
            "signal_strength_num": float(strength),
            "relevance_num": float(relevance),
            "target_return": target_return,
            "target_excess": (target_return - bench) if target_return is not None and bench is not None else None,
            "bench_return": bench,
        }
        records.append(rec)
    return pd.DataFrame(records)


def build_dataset(year_from: int, year_to: int) -> pd.DataFrame:
    conn = get_connection()
    frames = []
    for year in range(year_from, year_to + 1):
        df = build_year_rows(conn, year)
        if not df.empty:
            frames.append(df)
            print(f"{year}: rows={len(df)}")
        else:
            print(f"{year}: no rows")
    conn.close()
    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data.to_csv(FEATURES_CSV, index=False)
    return data


def clean_matrix(df: pd.DataFrame, columns: list[str], bounds: dict[str, tuple[float, float]] | None = None):
    xdf = df[columns].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if bounds is None:
        bounds = {}
        for c in columns:
            s = xdf[c].dropna()
            if s.empty:
                bounds[c] = (-1.0, 1.0)
                continue
            lo = float(s.quantile(0.01))
            hi = float(s.quantile(0.99))
            if not math.isfinite(lo) or not math.isfinite(hi) or lo == hi:
                lo, hi = float(s.min()), float(s.max())
            if not math.isfinite(lo) or not math.isfinite(hi) or lo == hi:
                lo, hi = -1.0, 1.0
            bounds[c] = (lo, hi)
    for c, (lo, hi) in bounds.items():
        xdf[c] = xdf[c].clip(lo, hi)
        fill = float(xdf[c].median()) if xdf[c].notna().any() else 0.0
        xdf[c] = xdf[c].fillna(fill)
    return xdf.to_numpy(dtype=float), bounds


def fit_ridge(train: pd.DataFrame, target: str, alpha: float) -> RidgeModel:
    cols = FEATURE_COLUMNS
    x, bounds = clean_matrix(train, cols)
    y = train[target].to_numpy(dtype=float)
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    std[std < 1e-9] = 1.0
    z = (x - mean) / std
    z = np.column_stack([np.ones(len(z)), z])
    reg = np.eye(z.shape[1]) * alpha
    reg[0, 0] = 0.0
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        lhs = z.T @ z + reg
        rhs = z.T @ y
    try:
        coef = np.linalg.solve(lhs, rhs)
    except np.linalg.LinAlgError:
        coef = np.linalg.pinv(lhs) @ rhs
    return RidgeModel(cols, mean, std, coef, alpha, bounds)


def top_metrics(df: pd.DataFrame, score_col: str, return_col: str = "target_return") -> dict[str, Any]:
    sub = df.dropna(subset=[score_col, return_col]).copy()
    if len(sub) < 10:
        return {"n": len(sub), "rho": None, "top_avg": None, "bot_avg": None, "spread": None, "excess": None}
    rho = spearman(sub[score_col].tolist(), sub[return_col].tolist())
    sub = sub.sort_values(score_col, ascending=False)
    k = min(10, max(1, len(sub) // 4))
    top = sub.head(k)
    bot = sub.tail(k)
    top_avg = float(top[return_col].mean())
    bot_avg = float(bot[return_col].mean())
    bench = float(sub["bench_return"].dropna().iloc[0]) if sub["bench_return"].notna().any() else None
    return {
        "n": int(len(sub)),
        "top_k": int(k),
        "rho": rho,
        "top_avg": top_avg,
        "bot_avg": bot_avg,
        "spread": top_avg - bot_avg,
        "excess": top_avg - bench if bench is not None else None,
        "top_codes": top["ts_code"].head(10).tolist(),
    }


def rank01(s: pd.Series) -> pd.Series:
    if s.notna().sum() <= 1:
        return pd.Series(0.5, index=s.index)
    return s.rank(pct=True)


def walk_forward(
    data: pd.DataFrame,
    min_train_years: int,
    alpha: float,
    *,
    validate_to: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    usable = data.dropna(subset=["target_return", "target_excess"]).copy()
    years = sorted(int(y) for y in usable["apply_year"].unique())
    if validate_to is not None:
        years = [y for y in years if y <= validate_to]
    pred_frames = []
    yearly: list[dict[str, Any]] = []
    blend_lambdas = [0.0, 0.25, 0.50, 0.75, 1.0]
    blend_yearly: dict[float, list[dict[str, Any]]] = {w: [] for w in blend_lambdas}
    for test_year in years:
        train_years = [y for y in years if y < test_year]
        if len(train_years) < min_train_years:
            continue
        train = usable[usable["apply_year"].isin(train_years)]
        test = usable[usable["apply_year"] == test_year].copy()
        model = fit_ridge(train, "target_excess", alpha)
        test["ml_score"] = model.predict(test)
        test["rule_rank_score"] = rank01(test["rule_score"])
        test["ml_rank_score"] = rank01(test["ml_score"])
        for w in blend_lambdas:
            col = f"blend_{w:.2f}"
            test[col] = (1.0 - w) * test["rule_rank_score"] + w * test["ml_rank_score"]
        pred_frames.append(test)
        rule_m = top_metrics(test, "rule_score")
        ml_m = top_metrics(test, "ml_score")
        blend_m = {}
        for w in blend_lambdas:
            m = top_metrics(test, f"blend_{w:.2f}")
            blend_m[f"{w:.2f}"] = m
            blend_yearly[w].append({"year": test_year, "blend": m})
        yearly.append({"year": test_year, "rule": rule_m, "ml": ml_m, "blend": blend_m})
        print(
            f"{test_year}: rule excess={fmt_pct(rule_m['excess'])} "
            f"ml excess={fmt_pct(ml_m['excess'])} rule spread={fmt_pct(rule_m['spread'])} "
            f"ml spread={fmt_pct(ml_m['spread'])}"
        )

    preds = pd.concat(pred_frames, ignore_index=True) if pred_frames else pd.DataFrame()
    summary = summarize_yearly(yearly)
    blend_summary = {}
    best_w = 0.0
    best_score = float("-inf")
    for w, items in blend_yearly.items():
        s = summarize_blend_yearly(items)
        blend_summary[f"{w:.2f}"] = s
        score = (s.get("mean_excess") or 0.0) + 0.5 * (s.get("mean_spread") or 0.0) + 0.25 * (s.get("mean_rho") or 0.0)
        if score > best_score:
            best_score = score
            best_w = w
    return preds, {
        "yearly": yearly,
        "summary": summary,
        "blend_summary": blend_summary,
        "best_blend_weight": best_w,
        "best_blend_objective": best_score,
    }


def fmt_pct(v: Any) -> str:
    return "N/A" if v is None else f"{float(v) * 100:.1f}%"


def mean_present(items: list[dict[str, Any]], path: tuple[str, str]) -> float | None:
    vals = []
    for item in items:
        v = item[path[0]].get(path[1])
        if v is not None:
            vals.append(float(v))
    return statistics.mean(vals) if vals else None


def summarize_yearly(yearly: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n_years": len(yearly),
        "rule_mean_rho": mean_present(yearly, ("rule", "rho")),
        "ml_mean_rho": mean_present(yearly, ("ml", "rho")),
        "rule_mean_spread": mean_present(yearly, ("rule", "spread")),
        "ml_mean_spread": mean_present(yearly, ("ml", "spread")),
        "rule_mean_excess": mean_present(yearly, ("rule", "excess")),
        "ml_mean_excess": mean_present(yearly, ("ml", "excess")),
    }


def summarize_blend_yearly(yearly: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [item["blend"] for item in yearly]
    def mean_key(key: str) -> float | None:
        xs = [float(v[key]) for v in vals if v.get(key) is not None]
        return statistics.mean(xs) if xs else None
    return {
        "n_years": len(vals),
        "mean_rho": mean_key("rho"),
        "mean_spread": mean_key("spread"),
        "mean_excess": mean_key("excess"),
    }


def train_current(data: pd.DataFrame, target_year: int, alpha: float) -> pd.DataFrame:
    train = data[(data["apply_year"] < target_year) & data["target_excess"].notna()].copy()
    current = data[data["apply_year"] == target_year].copy()
    if train.empty or current.empty:
        return pd.DataFrame()
    model = fit_ridge(train, "target_excess", alpha)
    current["ml_score"] = model.predict(current)
    return current


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="year_from", type=int, default=2019)
    parser.add_argument("--to", dest="year_to", type=int, default=2026)
    parser.add_argument("--min-train-years", type=int, default=2)
    parser.add_argument("--alpha", type=float, default=10.0)
    parser.add_argument("--target-year", type=int, default=2026)
    parser.add_argument("--validate-to", type=int, default=None)
    args = parser.parse_args()

    data = build_dataset(args.year_from, args.year_to)
    if data.empty:
        raise SystemExit("No dataset rows")
    validate_to = args.validate_to if args.validate_to is not None else min(args.year_to, args.target_year - 1)
    preds, report = walk_forward(data, args.min_train_years, args.alpha, validate_to=validate_to)
    current = train_current(data, args.target_year, args.alpha)
    best_blend = float(report.get("best_blend_weight", 0.0))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not preds.empty:
        pred_cols = [
            "apply_year", "ts_code", "index_name", "best_theme",
            "rule_score", "ml_score", f"blend_{best_blend:.2f}", "target_return", "target_excess",
        ]
        pred_cols = [c for c in pred_cols if c in preds.columns]
        preds[pred_cols].to_csv(PRED_CSV, index=False)
    current_path = OUT_DIR / f"industry_scorecard_{args.target_year}_ml_rank.csv"
    if not current.empty:
        current["rule_rank_score"] = rank01(current["rule_score"])
        current["ml_rank_score"] = rank01(current["ml_score"])
        current["blend_score"] = (1.0 - best_blend) * current["rule_rank_score"] + best_blend * current["ml_rank_score"]
        current = current.sort_values("blend_score", ascending=False)
        current_cols = [
            "apply_year", "ts_code", "index_name", "best_theme",
            "rule_score", "ml_score", "blend_score", "policy_norm", "news_score",
            "momentum_6m", "momentum_12m", "pb_pct", "heat_penalty",
        ]
        current[current_cols].to_csv(current_path, index=False)

    report.update({
        "generated_at": pd.Timestamp.now().isoformat(),
        "feature_columns": FEATURE_COLUMNS,
        "features_csv": str(FEATURES_CSV),
        "predictions_csv": str(PRED_CSV),
        "current_rank_csv": str(current_path) if not current.empty else None,
        "alpha": args.alpha,
        "min_train_years": args.min_train_years,
        "validate_to": validate_to,
        "dataset_rows": int(len(data)),
    })
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nSummary")
    for k, v in report["summary"].items():
        print(f"  {k}: {fmt_pct(v) if isinstance(v, float) else v}")
    print(f"  best_blend_weight: {best_blend:.2f}")
    if report.get("blend_summary"):
        bs = report["blend_summary"].get(f"{best_blend:.2f}", {})
        print(
            f"  best_blend_mean_excess: {fmt_pct(bs.get('mean_excess'))} "
            f"spread: {fmt_pct(bs.get('mean_spread'))} rho: {fmt_pct(bs.get('mean_rho'))}"
        )
    if not current.empty:
        print(f"\n{args.target_year} ML Top 10")
        for i, row in enumerate(current.head(10).itertuples(index=False), 1):
            print(
                f"  {i:2d}. {row.ts_code:14s} {row.index_name:18s} "
                f"blend={row.blend_score:+.4f} ml={row.ml_score:+.4f} theme={row.best_theme}"
            )
    print(f"\nWrote {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
