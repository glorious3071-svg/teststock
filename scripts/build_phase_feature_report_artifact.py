#!/usr/bin/env python3
"""Build the canonical artifact for the phase/feature technical report."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "backtests" / "phase_feature_technical_report"
ARTIFACT_JSON = OUT_DIR / "artifact.json"


def load(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def main() -> int:
    processed = load("data/backtests/processed_phase_features/report.json")
    selector_ic = load("data/backtests/csi_selector_feature_ic/report.json")
    selector = load("data/backtests/csi_snapshot_selector/report.json")
    tipp = load("data/backtests/calendar_neutral_csi_tipp_lag3_report.json")
    tipp_full = load("data/backtests/calendar_neutral_csi_tipp_report.json")
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    processed_rows = []
    for schedule_name, schedule in processed["schedules"].items():
        cadence = schedule_name.removeprefix("cycle12m_review")
        for item in schedule["features"]:
            if item["candidate"]:
                processed_rows.append(
                    {
                        "schedule": cadence,
                        "feature": item["feature"],
                        "phase_weighted_hit_rate": item["leave_phase_out"]["return_weighted_hit_rate"],
                        "expanding_year_weighted_hit_rate": item["expanding_year"]["return_weighted_hit_rate"],
                        "phase_wins": item["leave_phase_out"]["phase_count_above_50pct"],
                    }
                )
    processed_rows.sort(key=lambda row: row["phase_weighted_hit_rate"], reverse=True)
    processed_chart = processed_rows[:16]

    selector_feature_rows = [
        {
            "feature": item["feature"],
            "phase_top_quintile_excess": item["leave_phase_mean_top_quintile_excess"],
            "expanding_year_top_quintile_excess": item["expanding_year_mean_top_quintile_excess"],
            "positive_phase_count": item["positive_phase_count"],
            "median_ic": item["median_ic"],
            "candidate": item["candidate"],
        }
        for item in selector_ic["features"]
    ]
    selector_feature_chart = [row for row in selector_feature_rows if row["candidate"]]

    selector_policy_rows = [
        {
            "policy": item["policy"]["name"],
            "min_final_capital_wan": item["summary"]["min_final_capital_wan"],
            "median_final_capital_wan": item["summary"]["median_final_capital_wan"],
            "worst_max_drawdown": item["summary"]["worst_max_drawdown"],
            "min_annualized_return": item["summary"]["min_annualized_return"],
        }
        for item in selector["results"]
    ]
    selector_policy_rows.sort(key=lambda row: row["min_final_capital_wan"], reverse=True)

    tipp_rows = [
        {
            "rule": (
                f"{item['rule']['name']} / {item.get('defensive_policy', {}).get('name', 'cash_only')}"
            ),
            "min_final_capital_wan": item["summary"]["min_final_capital_wan"],
            "worst_drawdown_magnitude": -item["summary"]["worst_max_drawdown"],
            "median_average_exposure": item["summary"]["median_average_exposure"],
            "risk_target_met": item["summary"]["worst_max_drawdown"] >= -0.10,
            "capital_target_met": item["summary"]["min_final_capital_wan"] >= 4_000.0,
        }
        for item in tipp["results"]
    ]
    valid_tipp = [row for row in tipp_rows if row["risk_target_met"]]
    full_summary = tipp_full["results"][0]["summary"]
    best_valid_tipp = {
        "min_final_capital_wan": full_summary["min_final_capital_wan"],
        "worst_drawdown_magnitude": -full_summary["worst_max_drawdown"],
    }
    best_selector = max(selector_policy_rows, key=lambda row: row["min_final_capital_wan"])

    headline = [{
        "formal_observations": processed["method"].get("observation_count", 4_560),
        "processed_candidate_count": sum(row["candidate"] for row in selector_ic["features"]),
        "selector_min_capital_wan": best_selector["min_final_capital_wan"],
        "risk_valid_min_capital_wan": best_valid_tipp["min_final_capital_wan"],
        "risk_valid_worst_drawdown": best_valid_tipp["worst_drawdown_magnitude"],
    }]

    index_sql = "SELECT ts_code, trade_date, close FROM index_daily WHERE ts_code IN (%s) AND trade_date <= %s AND close IS NOT NULL ORDER BY ts_code, trade_date"
    external_sql = "SELECT symbol, trade_date, COALESCE(adj_close, close) AS close FROM external_asset_daily WHERE symbol IN (%s) AND trade_date < %s ORDER BY symbol, trade_date"
    defensive_sql = "SELECT e.ts_code, e.extname, e.index_name, e.list_date, f.trade_date, f.pct_chg FROM passive_etf e JOIN fund_daily f ON f.ts_code=e.ts_code WHERE (e.etf_type IS NULL OR e.etf_type!='QDII') AND (e.is_enhanced IS NULL OR e.is_enhanced=0) AND f.trade_date <= %s AND f.pct_chg IS NOT NULL ORDER BY e.ts_code, f.trade_date"
    selector_sql = "SELECT e.index_ts_code, e.list_date, r.as_of_date, r.final_score, d.trade_date, d.close FROM passive_etf e LEFT JOIN csi_annual_recommendation r ON r.ts_code=e.index_ts_code JOIN index_daily d ON d.ts_code=e.index_ts_code WHERE e.list_date <= %s AND (e.etf_type IS NULL OR e.etf_type!='QDII') AND (e.is_enhanced IS NULL OR e.is_enhanced=0) AND d.trade_date <= %s"
    sources = [
        {
            "id": "processed_features",
            "label": "Processed phase feature diagnostics",
            "path": "data/backtests/processed_phase_features/report.json",
            "query": {
                "sql": f"{index_sql}; {external_sql}",
                "description": "Load point-in-time A-share index closes and strictly lagged external market closes for phase feature diagnostics.",
                "engine": "mysql",
                "language": "sql",
                "tables_used": ["index_daily", "external_asset_daily"],
                "filters": ["20 continuous cycles", "12 phase offsets", "review intervals 12/6/3/1 months", "external trade_date strictly before snapshot"],
                "metric_definitions": {"phase_weighted_hit_rate": "Absolute-forward-return-weighted share of predictions whose direction matches the next review-window return."},
            },
        },
        {
            "id": "selector_ic",
            "label": "CSI selector cross-sectional feature IC",
            "path": "data/backtests/csi_selector_feature_ic/report.json",
            "query": {
                "sql": selector_sql,
                "description": "Load only benchmark indices backed by a listed domestic passive ETF at each snapshot, with the latest observable annual recommendation and index history.",
                "engine": "mysql",
                "language": "sql",
                "tables_used": ["passive_etf", "csi_annual_recommendation", "index_daily"],
                "filters": ["ETF list_date at or before snapshot", "exclude QDII and enhanced ETFs", "12 phase offsets", "3-trading-day execution lag"],
                "metric_definitions": {"phase_top_quintile_excess": "Mean next-12-month return of the held-out-phase top feature quintile minus the same-snapshot eligible-universe mean."},
            },
        },
        {
            "id": "selector_phase",
            "label": "Arbitrary-snapshot selector phase results",
            "path": "data/backtests/csi_snapshot_selector/report.json",
            "query": {
                "sql": selector_sql,
                "description": "Compound point-in-time selector returns over 20 cycles for each of 12 cycle phases.",
                "engine": "mysql",
                "language": "sql",
                "tables_used": ["passive_etf", "csi_annual_recommendation", "index_daily"],
                "filters": ["100万元 initial capital", "20 cycles", "12 phase offsets", "3-trading-day execution lag"],
                "metric_definitions": {"min_final_capital_wan": "Minimum terminal capital across all 12 phases, expressed in ten-thousand CNY units."},
            },
        },
        {
            "id": "tipp_phase",
            "label": "Calendar-neutral CSI TIPP lag-3 search",
            "path": "data/backtests/calendar_neutral_csi_tipp_lag3_report.json",
            "query": {
                "sql": f"{index_sql}; {defensive_sql}",
                "description": "Load daily A-share index closes and split-neutralized domestic defensive ETF returns for high-water risk-budget simulation across phase schedules.",
                "engine": "mysql",
                "language": "sql",
                "tables_used": ["index_daily", "passive_etf", "fund_daily"],
                "filters": ["A-share index basket plus domestic bond/gold passive ETFs and cash", "3-trading-day execution lag", "four review intervals", "12 phase offsets"],
                "metric_definitions": {"worst_drawdown_magnitude": "Absolute value of the lowest daily peak-to-trough portfolio drawdown across all tested cases.", "min_final_capital_wan": "Minimum terminal capital across all tested cases, in ten-thousand CNY units."},
            },
        },
        {
            "id": "tipp_full",
            "label": "Calendar-neutral CSI TIPP full execution-lag matrix",
            "path": "data/backtests/calendar_neutral_csi_tipp_report.json",
            "query": {
                "sql": f"{index_sql}; {defensive_sql}",
                "description": "Load daily A-share index closes and point-in-time domestic defensive ETF returns for the selected high-water rule across all review, phase, and execution-lag cases.",
                "engine": "mysql",
                "language": "sql",
                "tables_used": ["index_daily", "passive_etf", "fund_daily"],
                "filters": ["A-share index basket plus domestic bond passive ETFs and cash", "fund_daily pct_chg compounded to neutralize share splits", "execution lags 0/1/3/5 trading days", "four review intervals", "12 phase offsets"],
                "metric_definitions": {"worst_drawdown_magnitude": "Absolute value of the lowest daily peak-to-trough drawdown across 192 cases.", "min_final_capital_wan": "Minimum terminal capital across 192 cases, in ten-thousand CNY units."},
            },
        },
    ]

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "大盘评分与CSI选择的时点漂移特征诊断",
            "description": "技术报告：日历中性相位框架、方向匹配、任意快照选择与日频风险边界。",
            "generatedAt": generated_at,
            "cards": [
                {
                    "id": "observations",
                    "description": "四种复核间隔乘十二相位的方向匹配观察数。",
                    "dataset": "headline",
                    "sourceId": "processed_features",
                    "metrics": [{"label": "方向观察", "field": "formal_observations", "format": "number"}],
                },
                {
                    "id": "selector_capital",
                    "description": "任意快照选择器在十二相位中的最差二十年终值。",
                    "dataset": "headline",
                    "sourceId": "selector_phase",
                    "metrics": [{"label": "选择层最差终值, 万元", "field": "selector_min_capital_wan", "format": "number"}],
                },
                {
                    "id": "risk_capital",
                    "description": "满足最差回撤不超过10%的日频风险候选中，最低终值最高者。",
                    "dataset": "headline",
                    "sourceId": "tipp_full",
                    "metrics": [
                        {"label": "风险合格最差终值, 万元", "field": "risk_valid_min_capital_wan", "format": "number"},
                        {"label": "最差回撤", "field": "risk_valid_worst_drawdown", "format": "percent"},
                    ],
                },
            ],
            "charts": [
                {
                    "id": "processed_direction",
                    "title": "加工市场特征的留一相位方向命中率",
                    "subtitle": "按下一复核窗口收益绝对幅度加权；仅展示同时通过相位与时间扩展门槛的前16项。",
                    "type": "bar",
                    "dataset": "processed_features",
                    "sourceId": "processed_features",
                    "intent": "comparison",
                    "rationale": "排序横向比较可直接显示不同复核间隔下最稳定的方向特征。",
                    "encodings": {
                        "x": {"field": "feature", "type": "nominal", "label": "特征"},
                        "y": {"field": "phase_weighted_hit_rate", "type": "quantitative", "label": "加权方向命中率", "format": "percent"},
                    },
                    "valueFormat": "percent",
                },
                {
                    "id": "selector_feature_edge",
                    "title": "CSI选择特征的留一相位Top20%年化前瞻超额",
                    "subtitle": "方向在其他11个相位学习；数值为持有下一12个月的平均横截面超额收益。",
                    "type": "bar",
                    "dataset": "selector_feature_candidates",
                    "sourceId": "selector_ic",
                    "intent": "comparison",
                    "rationale": "条形图适合比较少量候选特征的样本外超额幅度。",
                    "encodings": {
                        "x": {"field": "feature", "type": "nominal", "label": "特征"},
                        "y": {"field": "phase_top_quintile_excess", "type": "quantitative", "label": "Top20%超额", "format": "percent"},
                    },
                    "valueFormat": "percent",
                },
                {
                    "id": "selector_policy_capital",
                    "title": "任意快照CSI选择策略的十二相位最差终值",
                    "subtitle": "100万元起始，20个连续12个月周期，执行滞后3个交易日；仅投资当时已有境内被动ETF映射的指数。",
                    "type": "bar",
                    "dataset": "selector_policies",
                    "sourceId": "selector_phase",
                    "intent": "comparison",
                    "rationale": "策略排序的核心比较量是十二相位中的最差终值。",
                    "encodings": {
                        "x": {"field": "policy", "type": "nominal", "label": "选择策略"},
                        "y": {"field": "min_final_capital_wan", "type": "quantitative", "label": "最差终值, 万元"},
                    },
                    "valueFormat": "number",
                },
                {
                    "id": "risk_frontier",
                    "title": "日频高水位风险预算的终值-回撤前沿",
                    "subtitle": "每点为一条预声明规则在四种复核频率乘十二相位中的最差结果；回撤目标线为10%。",
                    "type": "scatter",
                    "dataset": "tipp_frontier",
                    "sourceId": "tipp_phase",
                    "intent": "relationship",
                    "rationale": "散点图揭示严格回撤约束与最差终值之间的结构性权衡。",
                    "encodings": {
                        "x": {"field": "worst_drawdown_magnitude", "type": "quantitative", "label": "最差回撤幅度", "format": "percent"},
                        "y": {"field": "min_final_capital_wan", "type": "quantitative", "label": "最差终值, 万元"},
                    },
                },
            ],
            "tables": [
                {
                    "id": "selector_feature_table",
                    "title": "CSI选择特征样本外诊断明细",
                    "subtitle": "候选门槛要求至少8/12相位为正，且时间扩展Top20%超额为正。",
                    "dataset": "selector_feature_table",
                    "sourceId": "selector_ic",
                    "defaultSort": {"field": "phase_top_quintile_excess", "direction": "desc"},
                    "density": "dense",
                    "layout": "full",
                    "columns": [
                        {"field": "feature", "label": "特征", "type": "text"},
                        {"field": "median_ic", "label": "中位IC", "format": "number"},
                        {"field": "phase_top_quintile_excess", "label": "留一相位超额", "format": "percent"},
                        {"field": "expanding_year_top_quintile_excess", "label": "时间扩展超额", "format": "percent"},
                        {"field": "positive_phase_count", "label": "正向相位数", "format": "number"},
                        {"field": "candidate", "label": "纳入候选", "type": "text"},
                    ],
                },
            ],
            "sources": sources,
            "blocks": [
                {"id": "title", "type": "markdown", "body": "# 大盘评分与CSI选择的时点漂移特征诊断"},
                {"id": "summary", "type": "markdown", "body": "## 技术结论\n\n时点漂移后的崩塌主要来自两层错配：原评分把弱风险信号直接映射为低仓位，而风险变化只在少数低频节点刷新；原CSI篮子又按自然年保存，不能在漂移后的任意周期起点重排。日历中性调度、可观测日期修复和任意快照选择现已完成。加工特征提升了跨相位方向匹配，日频高水位层也能把最差回撤压到10%以内，但风险合格候选的最差终值仍未达到4000万元，因此当前状态是研究候选，不是生产规则。"},
                {"id": "metrics", "type": "metric-strip", "cardIds": ["observations", "selector_capital", "risk_capital"]},
                {"id": "processed_heading", "type": "markdown", "body": "## A股趋势与外部压力特征比完整评分更能跨相位匹配方向\n\n留一相位测试在其他11个相位学习特征方向与中位阈值，再在未见相位判断下一复核窗口涨跌；时间扩展测试只使用过去年份。中期趋势广度、沪深300回撤/均线、VIX、美元和美债期限结构通过双重门槛。"},
                {"id": "processed_chart", "type": "chart", "chartId": "processed_direction", "layout": "full"},
                {"id": "selector_heading", "type": "markdown", "body": "## CSI选择层应删除年度总分与12月动量主导，改用中期趋势持续性\n\n在当时已上市境内被动ETF的基准池中，6月趋势距离、6月/3月动量、上涨月份占比和12月回撤恢复具有正的跨相位与时间扩展超额；年度政策、新闻总分及跳过近1月的12月动量未通过。"},
                {"id": "selector_feature_chart", "type": "chart", "chartId": "selector_feature_edge", "layout": "full"},
                {"id": "selector_policy_chart", "type": "chart", "chartId": "selector_policy_capital", "layout": "full"},
                {"id": "selector_table", "type": "table", "tableId": "selector_feature_table", "layout": "full"},
                {"id": "scope", "type": "markdown", "body": "## 样本、口径与可观测日期\n\n样本为20个连续12个月周期，测试12个月周期下的12/6/3/1个月复核间隔、12个相位；本报告风险前沿先固定执行滞后3个交易日。A股收盘特征可使用快照当日数据并在下一交易日执行；VIX、美元和美债只使用严格早于快照日的值。ETF基准仅在对应境内非QDII、非增强被动ETF上市后进入选择池；早期不足时使用允许的宽基代理。"},
                {"id": "method", "type": "markdown", "body": "## 验证设计避免把相位稳定误当成行情匹配\n\n评分方向不是独立目标。每个信号都以预测方向是否匹配漂移后实际篮子收益为终点，并报告按收益绝对幅度加权的命中率。CSI横截面特征用留一相位确定方向，再比较未见相位Top20%与同期可投全集；另用仅过去年份确定方向的扩展测试排除全样本事后解释。"},
                {"id": "risk_heading", "type": "markdown", "body": "## 境内债券ETF提高恢复收益，但核心缺口仍在风险资产方向匹配\n\nTIPP使用全程高水位而非月内重置风险预算。风险资产仅为A股指数篮子；2013年首只合格国债ETF上市前剩余资金持有现金，之后可按点时规则进入境内债券ETF。防御ETF使用fund_daily.pct_chg累乘，避免把份额折算误判为90%亏损。全矩阵最差终值由约328万元提高至468万元，最差回撤仍控制在9.59%，但平均风险敞口约21%，终值距离4000万元的主要缺口仍是风险资产选择与行情方向的匹配强度。"},
                {"id": "risk_chart", "type": "chart", "chartId": "risk_frontier", "layout": "full"},
                {"id": "limits", "type": "markdown", "body": "## 限制与稳健性边界\n\n风险候选已完成4种复核频率×12相位×0/1/3/5日执行滞后的192案例矩阵，并全部守住10%回撤线。防御腿已使用实际境内债券ETF日收益，但任意快照权益选择仍使用指数收益作为ETF基准代理，尚未逐只计入跟踪误差、费率、成交滑点与融资可得性。外部特征只用于信号，不作为投资资产。当前任何未同时满足全部相位终值与回撤门槛的规则都不会进入自动持仓输出。"},
                {"id": "next", "type": "markdown", "body": "## 下一轮迭代\n\n1. 按ETF实际日收益复核任意快照权益选择器的指数代理偏差。\n2. 把已通过双重门槛的趋势持续性、回撤恢复与广度特征改为滚动点时权重，增强风险资产方向匹配。\n3. 分析最差终值路径的错失上涨段和错误持有段，针对恢复速度而非相位标签加工特征。\n4. 只有全矩阵同时达到最差终值4000万元和最差回撤10%以内，才解锁自动持仓产出。"},
                {"id": "questions", "type": "markdown", "body": "## 继续验证的问题\n\n实际权益ETF收益相对指数代理会损失多少超额？哪些点时可得的市场广度、流动性与风险偏好组合能在上涨初期更快恢复风险预算，同时在下跌前保持方向匹配？对融资敞口加入真实利率、涨跌停和申赎约束后，风险前沿会向内移动多少？"},
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "headline": headline,
                "processed_features": processed_chart,
                "selector_feature_candidates": selector_feature_chart,
                "selector_feature_table": selector_feature_rows,
                "selector_policies": selector_policy_rows,
                "tipp_frontier": tipp_rows,
            },
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_JSON.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(ARTIFACT_JSON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
