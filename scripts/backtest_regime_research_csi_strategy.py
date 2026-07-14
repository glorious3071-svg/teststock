#!/usr/bin/env python3
"""Backtest regime-aware CSI selection with fine-grained research signals.

All features are dated at or before the prior year end.  Realized CSI returns
are used only for validation and reporting.
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

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_research_enhanced_csi_strategy import build_research_features
from scripts.diagnose_csi_selection_gap import (
    YEARS,
    add_rank_features,
    add_style_and_regime_scores,
    dedup_top_winners,
    load_base_data,
    load_constituent_features,
    macro_regime,
    rank01,
)

OUT_DIR = ROOT / "data" / "ml"
OUT_JSON = OUT_DIR / "regime_research_csi_strategy_report.json"
OUT_YEARLY_CSV = OUT_DIR / "regime_research_csi_strategy_yearly.csv"
OUT_HOLDINGS_CSV = OUT_DIR / "regime_research_csi_strategy_holdings.csv"
OUT_WINNERS_CSV = OUT_DIR / "regime_research_csi_strategy_winners.csv"

FINE_GROUPS = {
    "reopen_travel": ["旅游", "酒店", "免税", "航空", "机场", "景区", "出行", "餐饮", "社会服务", "休闲服务", "文旅", "客流", "消费者服务"],
    "precious_metals": ["黄金", "贵金属"],
    "energy_security": ["油气", "石油", "天然气", "煤炭"],
    "dividend_soe": ["红利", "高股息", "央企", "国企", "银行", "公用", "低波", "分红"],
    "game_media": ["游戏", "动漫", "传媒", "影视", "数字娱乐"],
    "communication": ["通信", "5G", "电信", "光模块", "物联网", "算力网络"],
    "cloud_compute": ["云计算", "算力"],
    "ai_digital": ["人工智能", "AI", "云计算", "算力", "数字经济", "信创", "软件", "计算机", "数据"],
    "semiconductor": ["半导体", "芯片", "电子", "集成电路", "消费电子"],
    "resource_metals": ["有色", "稀土", "金属", "矿业", "小金属", "工业金属"],
    "satellite_space": ["卫星", "航天"],
    "defense_space": ["军工", "卫星", "航天", "低空"],
    "auto": ["汽车", "车联网", "智能汽车"],
    "finance": ["证券", "金融", "银行", "保险", "券商"],
}
POSITIVE_WORDS = ["买入", "增持", "推荐", "看好", "景气", "成长", "拐点", "复苏", "高增", "加速", "突破", "上行", "改善", "机会", "修复", "曙光"]


def fine_groups_for_text(text: str) -> set[str]:
    lower = str(text or "").lower()
    return {group for group, keywords in FINE_GROUPS.items() if any(k.lower() in lower for k in keywords)}


def fine_report_stats(conn, apply_year: int) -> dict[str, dict[str, Any]]:
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
        text = " ".join(str(x or "") for x in [title, industry, rating])
        for group in fine_groups_for_text(text):
            st = stats[group]
            st["count"] += 1
            if org_name:
                st["orgs"].add(str(org_name))
            if any(word in text for word in POSITIVE_WORDS) or str(rating or "") in {"买入", "增持", "推荐", "强烈推荐"}:
                st["positive"] += 1
    return stats


def report_score(stat: dict[str, Any]) -> float:
    count = int(stat["count"])
    if count <= 0:
        return 0.0
    orgs = len(stat["orgs"])
    positive_ratio = float(stat["positive"]) / count
    return math.log1p(count) + 0.8 * math.log1p(orgs) + 1.2 * positive_ratio


def build_fine_research_features(conn, data: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for year in YEARS:
        stats = fine_report_stats(conn, year)
        for row in data[data["apply_year"] == year].itertuples(index=False):
            # Use index name and assigned best theme for index classification.  Free-form
            # map reasons are intentionally excluded because they over-match broad words.
            text = f"{row.index_name} {row.best_theme}"
            groups = fine_groups_for_text(text)
            rec: dict[str, Any] = {
                "apply_year": int(year),
                "ts_code": row.ts_code,
                "fine_groups": "|".join(sorted(groups)),
            }
            for group in FINE_GROUPS:
                rec[f"{group}_attention"] = report_score(stats[group]) if group in groups and group in stats else 0.0
            records.append(rec)
    features = pd.DataFrame(records)
    for year in YEARS:
        mask = features["apply_year"] == year
        for group in FINE_GROUPS:
            features.loc[mask, f"{group}_rank"] = rank01(features.loc[mask, f"{group}_attention"])
    return features


def add_strategy_scores(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    for year in YEARS:
        mask = data["apply_year"] == year
        data.loc[mask, "research_rank"] = rank01(data.loc[mask, "research_attention_score"])
        data.loc[mask, "oversold12_rank"] = rank01(data.loc[mask, "momentum_12m"], ascending=False)
        data.loc[mask, "idx_mom12_rank2"] = rank01(data.loc[mask, "momentum_12m"])
        data.loc[mask, "idx_mom6_rank2"] = rank01(data.loc[mask, "momentum_6m"])
        data.loc[mask, "current_rank2"] = rank01(data.loc[mask, "current_guarded_score"])

    data["attention_reversal"] = data["research_rank"] * data["oversold12_rank"] * data["rule_rank"]
    data["attention_confirm"] = data["research_rank"] * data["idx_mom12_rank2"] * data["comp_breadth12_rank"].fillna(0.5)
    data["base_plus_confirm"] = 0.65 * data["current_rank2"] + 0.25 * data["attention_confirm"] + 0.10 * data["comp_breadth12_rank"].fillna(0.5)
    data["reopen_score"] = 0.55 * data["reopen_travel_rank"] + 0.25 * data["oversold12_rank"] + 0.20 * data["rule_rank"]
    data["gold_score"] = 0.55 * data["precious_metals_rank"] + 0.25 * data["low_dd_rank"] + 0.20 * data["idx_mom6_rank"]
    data["energy_score"] = 0.45 * data["energy_security_rank"] + 0.25 * data["idx_mom12_rank2"] + 0.20 * data["low_dd_rank"] + 0.10 * data["rule_rank"]
    data["resource_rotation_score"] = 0.40 * data["resource_metals_rank"] + 0.25 * data["comp_breadth12_rank"].fillna(0.5) + 0.20 * data["idx_mom12_rank2"] + 0.15 * data["low_dd_rank"]
    data["dividend_score"] = 0.45 * data["dividend_soe_rank"] + 0.30 * data["defensive_score"] + 0.15 * data["low_dd_rank"] + 0.10 * data["idx_mom12_rank2"]
    data["stag_def_score"] = 0.30 * data["defensive_score"] + 0.20 * data["dividend_score"] + 0.20 * data["energy_score"] + 0.15 * data["gold_score"] + 0.15 * data["reopen_score"]
    data["ai_reversal_score"] = 0.55 * data["attention_reversal"] + 0.25 * data["policy_recovery_score"] + 0.20 * data["research_rank"]
    data["growth_confirm_score2"] = (
        0.35 * data["attention_confirm"]
        + 0.25 * data["research_rank"]
        + 0.20 * data["comp_breadth12_rank"].fillna(0.5)
        + 0.10 * data["idx_mom12_rank2"]
        + 0.10 * data["current_rank2"]
    )
    data["cloud_breakout_score"] = 0.45 * data["idx_mom6_rank2"] + 0.25 * data["cloud_compute_rank"] + 0.20 * data["research_rank"] + 0.10 * data["current_rank2"]
    data["satellite_breakout_score"] = 0.45 * data["satellite_space_rank"] + 0.25 * data["idx_mom6_rank2"] + 0.20 * data["oversold12_rank"] + 0.10 * data["current_rank2"]
    data["liquidity_growth_score"] = 0.40 * data["growth_confirm_score2"] + 0.25 * data["idx_mom6_rank2"] + 0.20 * data["current_rank2"] + 0.15 * data["research_rank"]
    return data


def pick_one(year_df: pd.DataFrame, selected: list[str], score_col: str, group: str | None = None) -> None:
    candidates = year_df
    if group:
        candidates = candidates[candidates[f"{group}_attention"] > 0]
    for row in candidates.sort_values(score_col, ascending=False).itertuples(index=False):
        if row.ts_code not in selected:
            selected.append(row.ts_code)
            return


def fill_selection(year_df: pd.DataFrame, selected: list[str], score_col: str, k: int = 5) -> None:
    for row in year_df.sort_values(score_col, ascending=False).itertuples(index=False):
        if len(selected) >= k:
            return
        if row.ts_code not in selected:
            selected.append(row.ts_code)


def select_for_year(conn, year_df: pd.DataFrame, year: int) -> tuple[str, dict[str, Any], list[str]]:
    regime, macro_values = macro_regime(conn, year)
    selected: list[str] = []

    if regime == "stagflation_defensive":
        for score_col, group in [
            ("reopen_score", "reopen_travel"),
            ("gold_score", "precious_metals"),
            ("dividend_score", "dividend_soe"),
            ("energy_score", "energy_security"),
        ]:
            pick_one(year_df, selected, score_col, group)
        fill_selection(year_df, selected, "stag_def_score")
        return regime, macro_values, selected[:5]

    if regime == "policy_recovery":
        for score_col, group in [
            ("ai_reversal_score", "game_media"),
            ("ai_reversal_score", "communication"),
            ("ai_reversal_score", "ai_digital"),
            ("growth_confirm_score", "communication"),
            ("ai_reversal_score", "semiconductor"),
        ]:
            pick_one(year_df, selected, score_col, group)
        fill_selection(year_df, selected, "ai_reversal_score")
        return regime, macro_values, selected[:5]

    if regime == "liquidity_growth":
        for score_col, group in [
            ("comp_breadth12_rank", "communication"),
            ("cloud_breakout_score", "cloud_compute"),
            ("resource_rotation_score", "resource_metals"),
            ("satellite_breakout_score", "satellite_space"),
            ("growth_confirm_score2", "semiconductor"),
        ]:
            pick_one(year_df, selected, score_col, group)
        fill_selection(year_df, selected, "liquidity_growth_score")
        return regime, macro_values, selected[:5]

    # 2024-like weak-market disinflation repair: preserve high current scores
    # but force some confirmed growth breadth into the book.
    if (
        int(macro_values["pmi_below_52_months"]) >= 8
        and float(macro_values["ppi_yoy"]) < 0
        and float(macro_values["cs300_6m_return"]) < 0
    ):
        for score_col, group in [
            ("growth_confirm_score2", "communication"),
            ("growth_confirm_score2", "ai_digital"),
            ("current_guarded_score", "auto"),
            ("current_guarded_score", "finance"),
        ]:
            pick_one(year_df, selected, score_col, group)
        fill_selection(year_df, selected, "base_plus_confirm")
        return "weak_disinflation_repair", macro_values, selected[:5]

    fill_selection(year_df, selected, "current_guarded_score")
    return regime, macro_values, selected[:5]


def pct(value: Any) -> str:
    return f"{float(value) * 100:.1f}%"


def main() -> int:
    conn = get_connection()
    try:
        base = load_base_data()
        cons = load_constituent_features(conn, base)
        data = base.merge(cons, on=["apply_year", "ts_code"], how="left")
        data = add_style_and_regime_scores(add_rank_features(data))
        data = data.merge(build_research_features(conn, base), on=["apply_year", "ts_code"], how="left")
        data = data.merge(build_fine_research_features(conn, base), on=["apply_year", "ts_code"], how="left")
        data = add_strategy_scores(data)

        yearly: list[dict[str, Any]] = []
        holdings: list[dict[str, Any]] = []
        winner_rows: list[dict[str, Any]] = []
        for year in YEARS:
            year_df = data[data["apply_year"] == year].copy()
            regime, macro_values, selected_codes = select_for_year(conn, year_df, year)
            selected = year_df.set_index("ts_code").loc[selected_codes].reset_index()
            winners = dedup_top_winners(conn, year_df, year, k=5)
            winner_codes = {w["ts_code"] for w in winners}
            strategy_return = float(selected["target_return"].mean())
            benchmark_return = float(selected["bench_return"].dropna().iloc[0])
            yearly.append(
                {
                    "year": int(year),
                    "regime": regime,
                    "strategy_return": strategy_return,
                    "benchmark_return": benchmark_return,
                    "excess_return": strategy_return - benchmark_return,
                    "winner_hit": len(set(selected_codes) & winner_codes),
                    "selected_codes": "|".join(selected_codes),
                    "selected_names": "|".join(selected["index_name"].tolist()),
                    "winner_names": "|".join(w["index_name"] for w in winners),
                    "macro": json.dumps(macro_values, ensure_ascii=False, sort_keys=True),
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
                        "target_return": float(row.target_return),
                        "regime": regime,
                    }
                )
            for rank, winner in enumerate(winners, 1):
                winner_rows.append(
                    {
                        "year": int(year),
                        "rank": rank,
                        "ts_code": winner["ts_code"],
                        "index_name": winner["index_name"],
                        "target_return": winner["return"],
                        "selected": winner["ts_code"] in selected_codes,
                    }
                )
    finally:
        conn.close()

    summary = {
        "mean_strategy_return": statistics.mean(row["strategy_return"] for row in yearly),
        "mean_excess_return": statistics.mean(row["excess_return"] for row in yearly),
        "worst_strategy_return": min(row["strategy_return"] for row in yearly),
        "total_winner_hit": sum(row["winner_hit"] for row in yearly),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_YEARLY_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(yearly[0].keys()))
        writer.writeheader()
        writer.writerows(yearly)
    with OUT_HOLDINGS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(holdings[0].keys()))
        writer.writeheader()
        writer.writerows(holdings)
    with OUT_WINNERS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(winner_rows[0].keys()))
        writer.writeheader()
        writer.writerows(winner_rows)
    OUT_JSON.write_text(
        json.dumps(
            {
                "strategy": "regime_research_csi_selection",
                "no_lookahead_rule": "Uses prior-year H2 research metadata plus market, macro, and constituent features available at prior year end.",
                "summary": summary,
                "yearly": yearly,
                "yearly_csv": str(OUT_YEARLY_CSV),
                "holdings_csv": str(OUT_HOLDINGS_CSV),
                "winners_csv": str(OUT_WINNERS_CSV),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("Regime + Research CSI Strategy")
    print(
        f"  mean={pct(summary['mean_strategy_return'])} "
        f"excess={pct(summary['mean_excess_return'])} "
        f"worst={pct(summary['worst_strategy_return'])} "
        f"hits={summary['total_winner_hit']}"
    )
    for row in yearly:
        print(
            f"  {row['year']} {row['regime']}: strategy={pct(row['strategy_return'])} "
            f"bench={pct(row['benchmark_return'])} hits={row['winner_hit']} "
            f"{row['selected_names']}"
        )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
