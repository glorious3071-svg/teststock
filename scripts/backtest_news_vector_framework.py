#!/usr/bin/env python3
"""Backtest a news-vector enhancement layer for CSI annual ranking.

This script intentionally avoids external embedding or LLM APIs.  It uses a
local hashing-vector index over the already stored news extraction text to
measure whether semantic retrieval features improve the existing
rule-first/ML-enhanced CSI framework.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

warnings.filterwarnings(
    "ignore",
    message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
    category=FutureWarning,
)

from csi.ranking import news_window
from db.connection import get_connection
from news.processing.schema import ensure_processing_schema
from scripts.ml_industry_scorecard import (
    FEATURE_COLUMNS as BASE_FEATURE_COLUMNS,
    build_dataset,
    clean_matrix,
    fmt_pct,
    rank01,
    top_metrics,
)
from scripts.rank_annual_csi import load_theme_map
from scripts.validate_csi_rank import forward_return

OUT_DIR = ROOT / "data" / "ml"
VECTOR_FEATURES_CSV = OUT_DIR / "news_vector_theme_features.csv"
FRAMEWORK_FEATURES_CSV = OUT_DIR / "industry_scorecard_vector_features.csv"
PRED_CSV = OUT_DIR / "news_vector_framework_predictions.csv"
REPORT_JSON = OUT_DIR / "news_vector_framework_report.json"

VECTOR_FEATURE_COLUMNS = [
    "vector_event_count",
    "vector_sentiment_score",
    "vector_novelty_score",
    "vector_duplicate_density",
    "vector_semantic_breadth",
    "vector_source_diversity",
    "vector_similar_excess",
    "vector_theme_strength",
]


@dataclass
class NewsDoc:
    article_id: int
    event_id: int
    pub_date: date
    source: str
    themes: list[str]
    text: str
    sentiment: str
    magnitude: float
    confidence: float
    mention_count: int
    unique_sources: int
    vector: np.ndarray


def parse_json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x]


def sentiment_sign(sentiment: str | None) -> float:
    if sentiment == "bullish":
        return 1.0
    if sentiment == "bearish":
        return -1.0
    return 0.0


def text_tokens(text: str) -> list[str]:
    clean = "".join(ch.lower() if not ch.isspace() else " " for ch in text)
    tokens: list[str] = []
    ascii_buf: list[str] = []
    chinese_chars: list[str] = []

    def flush_ascii() -> None:
        if len(ascii_buf) >= 2:
            tokens.append("".join(ascii_buf))
        ascii_buf.clear()

    for ch in clean:
        if "\u4e00" <= ch <= "\u9fff":
            flush_ascii()
            chinese_chars.append(ch)
        elif ch.isascii() and ch.isalnum():
            ascii_buf.append(ch)
        else:
            flush_ascii()
    flush_ascii()

    for n in (2, 3):
        for i in range(0, max(0, len(chinese_chars) - n + 1)):
            tokens.append("".join(chinese_chars[i : i + n]))
    return tokens


def hash_vector(text: str, dims: int) -> np.ndarray:
    vec = np.zeros(dims, dtype=np.float64)
    for tok in text_tokens(text[:6000]):
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "little", signed=False)
        idx = h % dims
        sign = 1.0 if (h >> 63) == 0 else -1.0
        vec[idx] += sign
    norm = float(np.linalg.norm(vec))
    if norm > 1e-9:
        vec /= norm
    vec[~np.isfinite(vec)] = 0.0
    return vec


def load_news_docs(conn, year_from: int, year_to: int, dims: int) -> list[NewsDoc]:
    start = date(year_from - 4, 1, 1)
    end = date(year_to - 1, 12, 31)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, COALESCE(e.event_id, 0), DATE(COALESCE(a.pub_time, a.created_at)),
                   a.source, a.title, COALESCE(e.summary, ''), COALESCE(e.reasoning, ''),
                   e.themes, COALESCE(e.event_type, ''), COALESCE(e.sentiment, 'neutral'),
                   COALESCE(e.magnitude, 1), COALESCE(e.confidence, 0.8),
                   COALESCE(ev.mention_count, 1), COALESCE(ev.unique_sources, 1)
            FROM news_extraction e
            JOIN news_article a ON a.id = e.article_id
            LEFT JOIN news_event ev ON ev.id = e.event_id
            WHERE DATE(COALESCE(a.pub_time, a.created_at)) BETWEEN %s AND %s
              AND e.themes IS NOT NULL
            ORDER BY COALESCE(a.pub_time, a.created_at), a.id
            """,
            (start, end),
        )
        rows = cur.fetchall()

    docs: list[NewsDoc] = []
    for (
        article_id,
        event_id,
        pub_date,
        source,
        title,
        summary,
        reasoning,
        themes_raw,
        event_type,
        sentiment,
        magnitude,
        confidence,
        mention_count,
        unique_sources,
    ) in rows:
        themes = parse_json_list(themes_raw)
        if not themes:
            continue
        text = " ".join([str(title or ""), str(summary or ""), str(event_type or ""), " ".join(themes), str(reasoning or "")])
        docs.append(
            NewsDoc(
                article_id=int(article_id),
                event_id=int(event_id or article_id),
                pub_date=pub_date,
                source=str(source or ""),
                themes=themes,
                text=text,
                sentiment=str(sentiment or "neutral"),
                magnitude=float(magnitude or 1),
                confidence=float(confidence or 0.8),
                mention_count=int(mention_count or 1),
                unique_sources=int(unique_sources or 1),
                vector=hash_vector(text, dims),
            )
        )
    return docs


def theme_forward_excess(conn, year_from: int, year_to: int) -> dict[tuple[int, str], float]:
    theme_map = load_theme_map(conn, "CSI")
    by_theme: dict[str, list[str]] = {}
    for row in theme_map:
        by_theme.setdefault(row["theme"], []).append(row["ts_code"])

    out: dict[tuple[int, str], float] = {}
    for apply_year in range(year_from, year_to + 1):
        bench = forward_return(conn, "000300.SH", date(apply_year, 1, 5), date(apply_year, 12, 31))
        if bench is None:
            continue
        for theme, codes in by_theme.items():
            vals = []
            for ts in codes:
                ret = forward_return(conn, ts, date(apply_year, 1, 5), date(apply_year, 12, 31))
                if ret is not None and math.isfinite(float(ret)):
                    vals.append(float(ret) - float(bench))
            if vals:
                out[(apply_year, theme)] = statistics.mean(vals)
    return out


def max_similarity(vec: np.ndarray, others: list[np.ndarray]) -> float | None:
    if not others:
        return None
    mat = np.vstack(others)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        sims = mat @ vec
    sims = np.nan_to_num(sims, nan=-1.0, posinf=-1.0, neginf=-1.0)
    return float(np.max(sims)) if len(sims) else None


def mean_pairwise_max(vectors: list[np.ndarray]) -> float | None:
    if len(vectors) < 2:
        return None
    mat = np.vstack(vectors)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        sims = mat @ mat.T
    sims = np.nan_to_num(sims, nan=-1.0, posinf=-1.0, neginf=-1.0)
    np.fill_diagonal(sims, -1.0)
    return float(np.mean(np.max(sims, axis=1)))


def similar_excess_for_doc(
    doc: NewsDoc,
    history: list[NewsDoc],
    forward: dict[tuple[int, str], float],
    *,
    min_sim: float,
    top_k: int,
) -> float | None:
    scored: list[tuple[float, float]] = []
    for hist in history:
        h_year = hist.pub_date.year + 1
        vals = [forward[(h_year, t)] for t in hist.themes if (h_year, t) in forward]
        if not vals:
            continue
        sim = float(hist.vector @ doc.vector)
        if sim >= min_sim:
            scored.append((sim, statistics.mean(vals)))
    if not scored:
        return None
    scored.sort(reverse=True, key=lambda x: x[0])
    top = scored[:top_k]
    den = sum(abs(s) for s, _v in top)
    if den < 1e-9:
        return None
    return sum(s * v for s, v in top) / den


def history_outcome(doc: NewsDoc, forward: dict[tuple[int, str], float]) -> float | None:
    h_year = doc.pub_date.year + 1
    vals = [forward[(h_year, t)] for t in doc.themes if (h_year, t) in forward]
    return statistics.mean(vals) if vals else None


def similar_excess_map(
    current_docs: list[NewsDoc],
    history: list[NewsDoc],
    forward: dict[tuple[int, str], float],
    *,
    min_sim: float,
    top_k: int,
) -> dict[int, float]:
    hist_docs: list[NewsDoc] = []
    hist_outcomes: list[float] = []
    for doc in history:
        outcome = history_outcome(doc, forward)
        if outcome is not None and math.isfinite(float(outcome)):
            hist_docs.append(doc)
            hist_outcomes.append(float(outcome))
    if not current_docs or not hist_docs:
        return {}

    hist_mat = np.vstack([d.vector for d in hist_docs])
    cur_mat = np.vstack([d.vector for d in current_docs])
    hist_vals = np.array(hist_outcomes, dtype=float)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        sims = cur_mat @ hist_mat.T
    sims = np.nan_to_num(sims, nan=-1.0, posinf=-1.0, neginf=-1.0)

    out: dict[int, float] = {}
    for i, doc in enumerate(current_docs):
        row = sims[i]
        idx = np.where(row >= min_sim)[0]
        if idx.size == 0:
            continue
        if idx.size > top_k:
            idx = idx[np.argpartition(row[idx], -top_k)[-top_k:]]
        weights = row[idx]
        den = float(np.sum(np.abs(weights)))
        if den < 1e-9:
            continue
        out[doc.article_id] = float(np.sum(weights * hist_vals[idx]) / den)
    return out


def build_vector_theme_features(
    conn,
    year_from: int,
    year_to: int,
    *,
    dims: int,
    lookback_days: int,
    min_sim: float,
    top_k: int,
) -> pd.DataFrame:
    ensure_processing_schema(conn)
    docs = load_news_docs(conn, year_from, year_to, dims)
    forward = theme_forward_excess(conn, year_from - 3, year_to - 1)
    records: list[dict[str, Any]] = []

    for apply_year in range(year_from, year_to + 1):
        w_start, w_end = news_window(apply_year)
        history_start = w_start - timedelta(days=lookback_days)
        history = [d for d in docs if history_start <= d.pub_date < w_start]
        window_docs = [d for d in docs if w_start <= d.pub_date <= w_end]
        similar_by_article = similar_excess_map(
            window_docs,
            history,
            forward,
            min_sim=min_sim,
            top_k=top_k,
        )
        themes = sorted({t for d in window_docs for t in d.themes})
        history_by_theme = {
            t: [d for d in history if t in d.themes]
            for t in themes
        }

        for theme in themes:
            cur_docs = [d for d in window_docs if theme in d.themes]
            if not cur_docs:
                continue
            hist_same = history_by_theme.get(theme, [])
            hist_vectors = [d.vector for d in hist_same]
            cur_vectors = [d.vector for d in cur_docs]
            novelty_vals = []
            similar_excess_vals = []
            sentiment_vals = []
            source_vals = []
            for doc in cur_docs:
                ms = max_similarity(doc.vector, hist_vectors)
                novelty_vals.append(0.5 if ms is None else max(0.0, min(1.0, 1.0 - ms)))
                sx = similar_by_article.get(doc.article_id)
                if sx is not None and math.isfinite(float(sx)):
                    similar_excess_vals.append(float(sx))
                sentiment_vals.append(
                    sentiment_sign(doc.sentiment) * doc.magnitude * doc.confidence * math.log1p(doc.mention_count)
                )
                source_vals.append(math.log1p(max(doc.unique_sources, 1)))
            duplicate = mean_pairwise_max(cur_vectors)
            duplicate_density = 0.0 if duplicate is None else max(0.0, min(1.0, duplicate))
            novelty = statistics.mean(novelty_vals)
            sentiment_score = statistics.mean(sentiment_vals)
            source_div = statistics.mean(source_vals)
            similar_excess = statistics.mean(similar_excess_vals) if similar_excess_vals else 0.0
            breadth = max(0.0, min(1.0, 1.0 - duplicate_density))
            strength = sentiment_score * novelty * (1.0 - 0.5 * duplicate_density) * (1.0 + 0.1 * source_div)
            records.append(
                {
                    "apply_year": apply_year,
                    "theme": theme,
                    "vector_event_count": len(cur_docs),
                    "vector_sentiment_score": sentiment_score,
                    "vector_novelty_score": novelty,
                    "vector_duplicate_density": duplicate_density,
                    "vector_semantic_breadth": breadth,
                    "vector_source_diversity": source_div,
                    "vector_similar_excess": similar_excess,
                    "vector_theme_strength": strength,
                }
            )

    df = pd.DataFrame(records)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(VECTOR_FEATURES_CSV, index=False)
    return df


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


def fit_ridge(train: pd.DataFrame, columns: list[str], alpha: float) -> RidgeModel:
    x, bounds = clean_matrix(train, columns)
    y = train["target_excess"].to_numpy(dtype=float)
    y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    std[std < 1e-9] = 1.0
    z = (x - mean) / std
    z = np.nan_to_num(z, nan=0.0, posinf=0.0, neginf=0.0)
    z = np.column_stack([np.ones(len(z)), z])
    reg = np.eye(z.shape[1]) * alpha
    reg[0, 0] = 0.0
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        lhs = z.T @ z + reg
        rhs = z.T @ y
    lhs = np.nan_to_num(lhs, nan=0.0, posinf=0.0, neginf=0.0)
    rhs = np.nan_to_num(rhs, nan=0.0, posinf=0.0, neginf=0.0)
    try:
        coef = np.linalg.solve(lhs, rhs)
    except np.linalg.LinAlgError:
        coef = np.linalg.pinv(lhs) @ rhs
    return RidgeModel(columns, mean, std, coef, alpha, bounds)


def summarize_years(yearly: list[dict[str, Any]], key: str) -> dict[str, Any]:
    vals = [item[key] for item in yearly if key in item]

    def mean_metric(metric: str) -> float | None:
        xs = [float(v[metric]) for v in vals if v.get(metric) is not None]
        return statistics.mean(xs) if xs else None

    return {
        "n_years": len(vals),
        "mean_rho": mean_metric("rho"),
        "mean_spread": mean_metric("spread"),
        "mean_excess": mean_metric("excess"),
    }


def objective(summary: dict[str, Any]) -> float:
    return (
        float(summary.get("mean_excess") or 0.0)
        + 0.5 * float(summary.get("mean_spread") or 0.0)
        + 0.05 * float(summary.get("mean_rho") or 0.0)
    )


def framework_weight_combos(points: list[float]) -> list[tuple[float, float, float]]:
    vals = sorted({round(float(x), 2) for x in points} | {0.0, 1.0})
    combos: set[tuple[float, float, float]] = set()
    for rule_w in vals:
        for base_w in vals:
            vector_w = round(1.0 - rule_w - base_w, 2)
            if vector_w < -1e-9:
                continue
            if any(abs(vector_w - v) < 1e-9 for v in vals):
                combos.add((round(rule_w, 2), round(base_w, 2), round(vector_w, 2)))
    return sorted(combos, key=lambda x: (x[0], x[1], x[2]))


def framework_key(weights: tuple[float, float, float]) -> str:
    r, b, v = weights
    return f"framework_r{r:.2f}_b{b:.2f}_v{v:.2f}"


def select_framework_candidate(
    summaries: dict[str, dict[str, Any]],
    *,
    min_rule: float = 0.0,
    min_vector: float = 0.0,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for key, summary in summaries.items():
        if not key.startswith("framework_r"):
            continue
        parts = key.removeprefix("framework_").split("_")
        weights = {p[0]: float(p[1:]) for p in parts}
        rule_w = weights.get("r", 0.0)
        base_w = weights.get("b", 0.0)
        vector_w = weights.get("v", 0.0)
        if rule_w < min_rule or vector_w < min_vector:
            continue
        score = objective(summary)
        item = {
            "key": key,
            "weights": {"rule": rule_w, "base_ml": base_w, "vector_ml": vector_w},
            "summary": summary,
            "objective": score,
        }
        if best is None or score > float(best["objective"]):
            best = item
    return best


def walk_forward_eval(
    data: pd.DataFrame,
    columns: list[str],
    *,
    alpha: float,
    min_train_years: int,
    validate_to: int,
    weight_points: list[float],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    usable = data.dropna(subset=["target_return", "target_excess"]).copy()
    years = [int(y) for y in sorted(usable["apply_year"].unique()) if int(y) <= validate_to]
    combos = framework_weight_combos(weight_points)
    pred_frames = []
    yearly: list[dict[str, Any]] = []
    for test_year in years:
        train_years = [y for y in years if y < test_year]
        if len(train_years) < min_train_years:
            continue
        train = usable[usable["apply_year"].isin(train_years)].copy()
        test = usable[usable["apply_year"] == test_year].copy()
        base_model = fit_ridge(train, BASE_FEATURE_COLUMNS, alpha)
        vector_model = fit_ridge(train, columns, alpha)
        test["base_ml_score"] = base_model.predict(test)
        test["vector_ml_score"] = vector_model.predict(test)
        test["rule_rank_score"] = rank01(test["rule_score"])
        test["base_ml_rank_score"] = rank01(test["base_ml_score"])
        test["vector_ml_rank_score"] = rank01(test["vector_ml_score"])
        year_item = {
            "year": test_year,
            "rule": top_metrics(test, "rule_score"),
            "base_ml": top_metrics(test, "base_ml_score"),
            "vector_ml": top_metrics(test, "vector_ml_score"),
        }
        for weights in combos:
            rule_w, base_w, vector_w = weights
            col = framework_key(weights)
            test[col] = (
                rule_w * test["rule_rank_score"]
                + base_w * test["base_ml_rank_score"]
                + vector_w * test["vector_ml_rank_score"]
            )
            year_item[col] = top_metrics(test, col)
        yearly.append(year_item)
        pred_frames.append(test)
    preds = pd.concat(pred_frames, ignore_index=True) if pred_frames else pd.DataFrame()
    summaries = {
        "rule": summarize_years(yearly, "rule"),
        "base_ml": summarize_years(yearly, "base_ml"),
        "vector_ml": summarize_years(yearly, "vector_ml"),
    }
    best_weights = (1.0, 0.0, 0.0)
    best_key = framework_key(best_weights)
    best_summary = summaries["rule"]
    best_score = objective(best_summary)
    for weights in combos:
        key = framework_key(weights)
        summaries[key] = summarize_years(yearly, key)
        score = objective(summaries[key])
        if score > best_score:
            best_weights = weights
            best_key = key
            best_summary = summaries[key]
            best_score = score
    rule_first = select_framework_candidate(summaries, min_rule=0.5)
    rule_first_with_vector = select_framework_candidate(summaries, min_rule=0.5, min_vector=0.01)
    return preds, {
        "yearly": yearly,
        "summaries": summaries,
        "best_framework_key": best_key,
        "best_framework_weights": {
            "rule": best_weights[0],
            "base_ml": best_weights[1],
            "vector_ml": best_weights[2],
        },
        "best_vector_weight": best_weights[2],
        "best_summary": best_summary,
        "best_objective": best_score,
        "rule_first_candidate": rule_first,
        "rule_first_with_vector_candidate": rule_first_with_vector,
        "alpha": alpha,
        "columns": columns,
    }


def merge_vector_features(base: pd.DataFrame, vector_df: pd.DataFrame) -> pd.DataFrame:
    data = base.merge(
        vector_df,
        how="left",
        left_on=["apply_year", "best_theme"],
        right_on=["apply_year", "theme"],
    )
    for col in VECTOR_FEATURE_COLUMNS:
        if col not in data.columns:
            data[col] = 0.0
        data[col] = pd.to_numeric(data[col], errors="coerce").fillna(0.0)
    data = data.drop(columns=["theme"], errors="ignore")
    return data


def tune_framework(
    data: pd.DataFrame,
    *,
    validate_to: int,
    min_train_years: int,
    alphas: list[float],
    weight_points: list[float],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    columns = BASE_FEATURE_COLUMNS + VECTOR_FEATURE_COLUMNS
    best_preds = pd.DataFrame()
    best_report: dict[str, Any] | None = None
    best_rule_first: dict[str, Any] | None = None
    best_rule_first_with_vector: dict[str, Any] | None = None
    for alpha in alphas:
        preds, report = walk_forward_eval(
            data,
            columns,
            alpha=alpha,
            min_train_years=min_train_years,
            validate_to=validate_to,
            weight_points=weight_points,
        )
        if best_report is None or float(report["best_objective"]) > float(best_report["best_objective"]):
            best_report = report
            best_preds = preds
        for name, current_best in (
            ("rule_first_candidate", best_rule_first),
            ("rule_first_with_vector_candidate", best_rule_first_with_vector),
        ):
            cand = report.get(name)
            if cand is None:
                continue
            cand = {**cand, "alpha": alpha}
            if current_best is None or float(cand["objective"]) > float(current_best["objective"]):
                if name == "rule_first_candidate":
                    best_rule_first = cand
                else:
                    best_rule_first_with_vector = cand
    assert best_report is not None
    best_report["tuned_rule_first_candidate"] = best_rule_first
    best_report["tuned_rule_first_with_vector_candidate"] = best_rule_first_with_vector
    return best_preds, best_report


def train_current(data: pd.DataFrame, report: dict[str, Any], target_year: int) -> pd.DataFrame:
    train = data[(data["apply_year"] < target_year) & data["target_excess"].notna()].copy()
    current = data[data["apply_year"] == target_year].copy()
    if train.empty or current.empty:
        return pd.DataFrame()
    model = fit_ridge(train, report["columns"], float(report["alpha"]))
    base_model = fit_ridge(train, BASE_FEATURE_COLUMNS, float(report["alpha"]))
    current["base_ml_score"] = base_model.predict(current)
    current["vector_ml_score"] = model.predict(current)
    current["rule_rank_score"] = rank01(current["rule_score"])
    current["base_ml_rank_score"] = rank01(current["base_ml_score"])
    current["vector_ml_rank_score"] = rank01(current["vector_ml_score"])
    weights = report["best_framework_weights"]
    current["framework_score"] = (
        float(weights["rule"]) * current["rule_rank_score"]
        + float(weights["base_ml"]) * current["base_ml_rank_score"]
        + float(weights["vector_ml"]) * current["vector_ml_rank_score"]
    )
    return current.sort_values("framework_score", ascending=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="year_from", type=int, default=2019)
    parser.add_argument("--to", dest="year_to", type=int, default=2026)
    parser.add_argument("--target-year", type=int, default=2026)
    parser.add_argument("--validate-to", type=int, default=None)
    parser.add_argument("--min-train-years", type=int, default=2)
    parser.add_argument("--dims", type=int, default=2048)
    parser.add_argument("--lookback-days", type=int, default=1095)
    parser.add_argument("--min-sim", type=float, default=0.18)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--alphas", default="1,3,10,30,100")
    parser.add_argument("--weight-points", default="0,0.10,0.25,0.50,0.75,1.00")
    args = parser.parse_args()

    alphas = [float(x) for x in args.alphas.split(",") if x.strip()]
    weight_points = [float(x) for x in args.weight_points.split(",") if x.strip()]
    validate_to = args.validate_to if args.validate_to is not None else min(args.year_to, args.target_year - 1)

    conn = get_connection()
    try:
        base = build_dataset(args.year_from, args.year_to)
        vector_df = build_vector_theme_features(
            conn,
            args.year_from,
            args.year_to,
            dims=args.dims,
            lookback_days=args.lookback_days,
            min_sim=args.min_sim,
            top_k=args.top_k,
        )
    finally:
        conn.close()

    data = merge_vector_features(base, vector_df)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data.to_csv(FRAMEWORK_FEATURES_CSV, index=False)
    preds, report = tune_framework(
        data,
        validate_to=validate_to,
        min_train_years=args.min_train_years,
        alphas=alphas,
        weight_points=weight_points,
    )
    current = train_current(data, report, args.target_year)

    if not preds.empty:
        pred_cols = [
            "apply_year",
            "ts_code",
            "index_name",
            "best_theme",
            "rule_score",
            "base_ml_score",
            "vector_ml_score",
            report["best_framework_key"],
            "target_return",
            "target_excess",
        ]
        pred_cols = [c for c in pred_cols if c in preds.columns]
        preds[pred_cols].to_csv(PRED_CSV, index=False)

    current_path = OUT_DIR / f"news_vector_framework_{args.target_year}_rank.csv"
    if not current.empty:
        current_cols = [
            "apply_year",
            "ts_code",
            "index_name",
            "best_theme",
            "rule_score",
            "base_ml_score",
            "vector_ml_score",
            "framework_score",
            "vector_event_count",
            "vector_novelty_score",
            "vector_duplicate_density",
            "vector_similar_excess",
            "vector_theme_strength",
        ]
        current[current_cols].to_csv(current_path, index=False)

    final_report = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "year_from": args.year_from,
        "year_to": args.year_to,
        "target_year": args.target_year,
        "validate_to": validate_to,
        "dataset_rows": int(len(data)),
        "vector_theme_rows": int(len(vector_df)),
        "vector_features_csv": str(VECTOR_FEATURES_CSV),
        "framework_features_csv": str(FRAMEWORK_FEATURES_CSV),
        "predictions_csv": str(PRED_CSV),
        "current_rank_csv": str(current_path) if not current.empty else None,
        "params": {
            "dims": args.dims,
            "lookback_days": args.lookback_days,
            "min_sim": args.min_sim,
            "top_k": args.top_k,
            "alphas": alphas,
            "weight_points": weight_points,
            "min_train_years": args.min_train_years,
        },
        **report,
    }
    REPORT_JSON.write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nNews Vector Framework Summary")
    print(f"  rows: dataset={len(data)} vector_theme={len(vector_df)} validate_to={validate_to}")
    print(f"  best_alpha: {float(report['alpha']):.2f}")
    bw = report["best_framework_weights"]
    print(
        "  best_framework_weights: "
        f"rule={float(bw['rule']):.2f} base_ml={float(bw['base_ml']):.2f} vector_ml={float(bw['vector_ml']):.2f}"
    )
    for key, val in report["summaries"].items():
        if key.startswith("framework_") and key != report["best_framework_key"]:
            continue
        print(
            f"  {key}: excess={fmt_pct(val.get('mean_excess'))} "
            f"spread={fmt_pct(val.get('mean_spread'))} rho={fmt_pct(val.get('mean_rho'))}"
        )
    if not current.empty:
        print(f"\n{args.target_year} framework Top 10")
        for i, row in enumerate(current.head(10).itertuples(index=False), 1):
            print(
                f"  {i:2d}. {row.ts_code:14s} {row.index_name:18s} "
                f"framework={row.framework_score:+.4f} vector_ml={row.vector_ml_score:+.4f} "
                f"theme={row.best_theme}"
            )
    print(f"\nWrote {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
