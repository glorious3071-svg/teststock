#!/usr/bin/env python3
"""Backtest CSI selection enhanced with broker research attention signals.

The signal uses East Money industry research reports whose report_date is in
H2 of the previous calendar year.  It is designed as an ex-ante feature:
realized CSI returns are used only for validation.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from db.connection import get_connection
from scripts.diagnose_csi_selection_gap import (
    YEARS,
    dedup_top_winners,
    load_base_data,
    rank01,
)

OUT_DIR = ROOT / "data" / "ml"
OUT_JSON = OUT_DIR / "research_enhanced_csi_strategy_report.json"
OUT_YEARLY_CSV = OUT_DIR / "research_enhanced_csi_strategy_yearly.csv"
OUT_HOLDINGS_CSV = OUT_DIR / "research_enhanced_csi_strategy_holdings.csv"
OUT_FEATURES_CSV = OUT_DIR / "research_attention_features_2021_2025.csv"

GROUPS = {
    "ai_compute": ["人工智能", "AI", "算力", "云计算", "数据", "数字经济", "信创", "软件", "计算机", "IT服务", "计算机设备", "软件开发", "互联网服务", "通信设备", "半导体", "消费电子", "电子", "机器人", "液冷"],
    "communication": ["通信", "5G", "电信", "物联网", "通信设备", "通信服务", "光模块", "算力网络"],
    "semiconductor": ["半导体", "芯片", "电子", "集成电路", "消费电子", "元件"],
    "resource": ["有色", "稀土", "金属", "矿业", "工业金属", "小金属", "贵金属", "能源金属", "黄金", "煤炭", "钢铁", "油气"],
    "new_energy": ["新能源", "光伏", "储能", "电池", "锂电", "电网", "电力设备", "风电"],
    "media_game": ["游戏", "动漫", "传媒", "影视", "文化传媒", "数字娱乐"],
    "finance": ["证券", "金融", "银行", "保险", "非银", "金融科技"],
    "auto": ["汽车", "智能汽车", "车联网", "汽车零部件"],
    "defense": ["军工", "卫星", "航天", "低空", "航空"],
    "consumer": ["消费", "旅游", "食品饮料", "商贸", "零售", "酒店", "农业"],
}
POSITIVE_WORDS = ["买入", "增持", "推荐", "看好", "景气", "成长", "拐点", "复苏", "高增", "加速", "突破", "上行", "改善", "机会"]


def groups_for_text(text: str) -> set[str]:
    lower = str(text or "").lower()
    return {
        group
        for group, keywords in GROUPS.items()
        if any(keyword.lower() in lower for keyword in keywords)
    }


def load_index_descriptors(conn) -> dict[str, str]:
    out: dict[str, list[str]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, index_name, theme, relevance, reason
            FROM theme_index_map WHERE ts_code LIKE '%.CSI'
            """
        )
        for ts_code, index_name, theme, relevance, reason in cur.fetchall():
            out[str(ts_code)].extend([str(index_name or ""), str(theme or ""), str(relevance or ""), str(reason or "")])
    return {ts: " ".join(parts) for ts, parts in out.items()}


def research_group_stats(conn, apply_year: int) -> dict[str, dict[str, Any]]:
    start = date(apply_year - 1, 7, 1)
    end = date(apply_year - 1, 12, 31)
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "orgs": set(), "positive": 0})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT title, org_name, industry, rating
            FROM broker_research_report
            WHERE source='eastmoney_api' AND report_type='industry'
              AND report_date BETWEEN %s AND %s
            """,
            (start, end),
        )
        rows = cur.fetchall()
    for title, org_name, industry, rating in rows:
        text = " ".join([str(title or ""), str(industry or ""), str(rating or "")])
        for group in groups_for_text(text):
            st = stats[group]
            st["count"] += 1
            if org_name:
                st["orgs"].add(str(org_name))
            if any(word in text for word in POSITIVE_WORDS) or str(rating or "") in {"买入", "增持", "推荐", "强烈推荐"}:
                st["positive"] += 1
    return stats


def build_research_features(conn, data: pd.DataFrame) -> pd.DataFrame:
    descriptors = load_index_descriptors(conn)
    rows: list[dict[str, Any]] = []
    for year in YEARS:
        stats = research_group_stats(conn, year)
        for row in data[data["apply_year"] == year].itertuples(index=False):
            text = descriptors.get(row.ts_code, f"{row.index_name} {row.best_theme}")
            index_groups = sorted(groups_for_text(text))
            score = 0.0
            matched = []
            for group in index_groups:
                st = stats.get(group)
                if not st:
                    continue
                count = int(st["count"])
                orgs = len(st["orgs"])
                positive_ratio = float(st["positive"]) / count if count else 0.0
                value = math.log1p(count) + 0.8 * math.log1p(orgs) + 1.2 * positive_ratio
                score += value
                matched.append({"group": group, "count": count, "orgs": orgs, "positive_ratio": positive_ratio})
            rows.append(
                {
                    "apply_year": int(year),
                    "ts_code": row.ts_code,
                    "research_attention_score": score,
                    "research_groups": json.dumps(matched, ensure_ascii=False),
                }
            )
    features = pd.DataFrame(rows)
    for year in YEARS:
        mask = features["apply_year"] == year
        features.loc[mask, "research_attention_rank"] = rank01(features.loc[mask, "research_attention_score"])
    return features


def add_interaction_scores(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    for year in YEARS:
        mask = data["apply_year"] == year
        data.loc[mask, "current_rank"] = rank01(data.loc[mask, "current_guarded_score"])
        data.loc[mask, "rule_rank"] = rank01(data.loc[mask, "rule_score"])
        data.loc[mask, "mom12_rank"] = rank01(data.loc[mask, "momentum_12m"])
        data.loc[mask, "oversold12_rank"] = rank01(data.loc[mask, "momentum_12m"], ascending=False)
    data["research_momentum"] = data["research_attention_rank"] * data["mom12_rank"]
    data["research_reversal"] = data["research_attention_rank"] * data["rule_rank"] * data["oversold12_rank"]
    data["research_enhanced_score"] = (
        0.80 * data["current_rank"]
        + 0.06 * data["research_momentum"]
        + 0.14 * data["research_reversal"]
    )
    return data


def evaluate(data: pd.DataFrame, score_col: str, winners_by_year: dict[int, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    yearly: list[dict[str, Any]] = []
    holdings: list[dict[str, Any]] = []
    for year, g in data.groupby("apply_year"):
        selected = g.sort_values(score_col, ascending=False).head(5)
        bench = float(selected["bench_return"].dropna().iloc[0])
        strategy_return = float(selected["target_return"].mean())
        winner_hit = len(set(selected["ts_code"]) & {w["ts_code"] for w in winners_by_year[int(year)]})
        yearly.append(
            {
                "year": int(year),
                "strategy_return": strategy_return,
                "benchmark_return": bench,
                "excess_return": strategy_return - bench,
                "winner_hit": winner_hit,
                "selected_codes": "|".join(selected["ts_code"].tolist()),
                "selected_names": "|".join(selected["index_name"].tolist()),
            }
        )
        for rank, row in enumerate(selected.itertuples(index=False), 1):
            holdings.append(
                {
                    "year": int(year),
                    "rank": rank,
                    "ts_code": row.ts_code,
                    "index_name": row.index_name,
                    "best_theme": row.best_theme,
                    "research_enhanced_score": float(row.research_enhanced_score),
                    "current_guarded_score": float(row.current_guarded_score),
                    "research_attention_score": float(row.research_attention_score),
                    "target_return": float(row.target_return),
                }
            )
    summary = {
        "mean_strategy_return": statistics.mean(r["strategy_return"] for r in yearly),
        "mean_excess_return": statistics.mean(r["excess_return"] for r in yearly),
        "worst_strategy_return": min(r["strategy_return"] for r in yearly),
        "total_winner_hit": sum(r["winner_hit"] for r in yearly),
    }
    return yearly, summary, holdings


def main() -> int:
    conn = get_connection()
    try:
        data = load_base_data()
        features = build_research_features(conn, data)
        data = data.merge(features, on=["apply_year", "ts_code"], how="left")
        data = add_interaction_scores(data)
        winners_by_year = {year: dedup_top_winners(conn, data[data["apply_year"] == year], year) for year in YEARS}
    finally:
        conn.close()

    current_yearly, current_summary, _current_holdings = evaluate(data, "current_guarded_score", winners_by_year)
    enhanced_yearly, enhanced_summary, enhanced_holdings = evaluate(data, "research_enhanced_score", winners_by_year)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    features.to_csv(OUT_FEATURES_CSV, index=False)
    with OUT_YEARLY_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(enhanced_yearly[0].keys()))
        writer.writeheader()
        writer.writerows(enhanced_yearly)
    with OUT_HOLDINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(enhanced_holdings[0].keys()))
        writer.writeheader()
        writer.writerows(enhanced_holdings)
    report = {
        "strategy": "research_enhanced_csi_selection",
        "no_lookahead_rule": "Uses only industry research report metadata dated in H2 of the prior year plus prior-year market/rule features.",
        "score_formula": "0.80*current_rank + 0.06*(research_attention_rank*mom12_rank) + 0.14*(research_attention_rank*rule_rank*oversold12_rank)",
        "current_summary": current_summary,
        "enhanced_summary": enhanced_summary,
        "current_yearly": current_yearly,
        "enhanced_yearly": enhanced_yearly,
        "yearly_csv": str(OUT_YEARLY_CSV),
        "holdings_csv": str(OUT_HOLDINGS_CSV),
        "features_csv": str(OUT_FEATURES_CSV),
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def pct(v: Any) -> str:
        return f"{float(v) * 100:.1f}%"

    print("Research Enhanced CSI Strategy")
    print(
        f"  current mean={pct(current_summary['mean_strategy_return'])} "
        f"excess={pct(current_summary['mean_excess_return'])} "
        f"worst={pct(current_summary['worst_strategy_return'])} "
        f"hits={current_summary['total_winner_hit']}"
    )
    print(
        f"  enhanced mean={pct(enhanced_summary['mean_strategy_return'])} "
        f"excess={pct(enhanced_summary['mean_excess_return'])} "
        f"worst={pct(enhanced_summary['worst_strategy_return'])} "
        f"hits={enhanced_summary['total_winner_hit']}"
    )
    for row in enhanced_yearly:
        print(
            f"  {row['year']}: strategy={pct(row['strategy_return'])} "
            f"bench={pct(row['benchmark_return'])} hits={row['winner_hit']} "
            f"{row['selected_names']}"
        )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
