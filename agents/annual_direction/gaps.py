"""Identify data gaps and supplement via web search."""

from __future__ import annotations

from typing import Any

from agents.annual_direction.models import AnnualContext, DataGap
from agents.annual_direction.search import web_search

# Dimensions useful for 年初定方向; key used in supplemented dict
REQUIRED_DIMENSIONS: list[dict[str, str]] = [
    {"key": "cewc", "label": "中央经济工作会议定调", "importance": "high"},
    {"key": "macro_snapshot", "label": "宏观利率/GDP/通胀/货币/社融/PMI快照", "importance": "high"},
    {"key": "index_valuation", "label": "主要宽基指数估值(PE/PB)", "importance": "high"},
    {"key": "etf_universe", "label": "可投资ETF标的池", "importance": "high"},
    {"key": "pboc_report", "label": "央行货币政策执行报告", "importance": "medium"},
    {"key": "market_sentiment", "label": "市场情绪(两融/新基等)", "importance": "medium"},
    {"key": "geopolitical", "label": "国际地缘与贸易环境", "importance": "medium"},
    {"key": "policy_calendar", "label": "当年重大政策日程", "importance": "low"},
]


def _has_valuation_in_db(ctx: AnnualContext) -> bool:
    snap = ctx.macro_snapshot or {}
    return any(
        k in snap and snap[k] is not None
        for k in ("hs300_pe_ttm", "sz50_pe_ttm", "pe_ttm", "pb", "index_pe")
    )


def _evaluate_db_coverage(ctx: AnnualContext) -> list[DataGap]:
    gaps: list[DataGap] = []
    snap = ctx.macro_snapshot or {}

    gaps.append(
        DataGap(
            key="cewc",
            label="中央经济工作会议定调",
            importance="high",
            in_db=ctx.cewc is not None,
            value=ctx.cewc,
            source="db" if ctx.cewc else None,
        )
    )

    macro_ok = snap and snap.get("shibor_3m") is not None
    gaps.append(
        DataGap(
            key="macro_snapshot",
            label="宏观快照",
            importance="high",
            in_db=macro_ok,
            value=ctx.macro_brief if macro_ok else None,
            source="db" if macro_ok else None,
        )
    )

    gaps.append(
        DataGap(
            key="index_valuation",
            label="宽基指数估值(PE/PB)",
            importance="high",
            in_db=_has_valuation_in_db(ctx),
            source="db" if _has_valuation_in_db(ctx) else None,
        )
    )

    etf_ok = ctx.etf_universe_count > 0
    gaps.append(
        DataGap(
            key="etf_universe",
            label="ETF标的池",
            importance="high",
            in_db=etf_ok,
            value={"count": ctx.etf_universe_count, "candidates": len(ctx.etf_candidates)},
            source="db" if etf_ok else None,
        )
    )

    pboc_ok = bool(snap and snap.get("pboc_report_date"))
    gaps.append(
        DataGap(
            key="pboc_report",
            label="央行货政报告",
            importance="medium",
            in_db=pboc_ok,
            value=snap.get("pboc_report_title") if pboc_ok else None,
            source="db" if pboc_ok else None,
        )
    )

    margin_ok = bool(snap and snap.get("margin_rzrqye") is not None)
    gaps.append(
        DataGap(
            key="market_sentiment",
            label="市场情绪(两融/新基等)",
            importance="medium",
            in_db=margin_ok,
            value={
                "margin_rzrqye": snap.get("margin_rzrqye"),
                "margin_date": snap.get("margin_date"),
                "margin_stance": snap.get("margin_stance"),
                "note": "两融已入库；新基发行规模仍缺失" if margin_ok else None,
            }
            if margin_ok
            else None,
            source="db" if margin_ok else None,
        )
    )

    for item in REQUIRED_DIMENSIONS[6:]:
        gaps.append(
            DataGap(
                key=item["key"],
                label=item["label"],
                importance=item["importance"],
                in_db=False,
            )
        )

    return gaps


def _search_queries(apply_year: int, gap: DataGap) -> list[str]:
    """仅使用决策时点之前的信息检索词（禁止 '{当年}展望' 类前视查询）。"""
    prev = apply_year - 1
    prev2 = apply_year - 2
    mapping = {
        "index_valuation": [
            f"A股 沪深300 市盈率 PE {prev}年12月",
            f"中证500 市净率 PB {prev}年底",
        ],
        "pboc_report": [f"中国人民银行 {prev}年 货币政策执行报告 摘要"],
        "market_sentiment": [f"A股 融资余额 新基金发行 {prev}年"],
        "geopolitical": [f"中国经济 {prev}年 国际环境 地缘政治 回顾"],
        "policy_calendar": [f"中国 {prev2}年 重大经济政策 回顾"],
    }
    return mapping.get(gap.key, [f"{gap.label} {prev}年"])


def _extract_snippet(results: list[dict[str, str]], max_len: int = 800) -> str | None:
    if not results:
        return None
    parts = []
    for r in results[:3]:
        title = r.get("title", "")
        body = r.get("snippet", "")
        if title or body:
            parts.append(f"{title}: {body}".strip())
    text = " | ".join(parts)
    return text[:max_len] if text else None


def fill_gaps(ctx: AnnualContext, *, enable_web: bool = True) -> AnnualContext:
    gaps = _evaluate_db_coverage(ctx)
    backtest = bool(ctx.agent_mode and ctx.agent_mode.is_backtest)
    if backtest:
        enable_web = False

    for gap in gaps:
        if gap.in_db or gap.importance == "low":
            continue
        if not enable_web:
            gap.source = "missing"
            gap.note = "回测模式禁用网络搜索" if backtest else "未启用网络补充"
            if backtest and gap.importance != "low":
                ctx.still_missing.append(gap.label)
            continue

        queries = _search_queries(ctx.apply_year, gap)
        combined = []
        for q in queries[:2]:
            results = web_search(q, max_results=3)
            snippet = _extract_snippet(results)
            if snippet:
                combined.append({"query": q, "snippet": snippet, "results": results[:2]})

        if combined:
            gap.source = "web"
            gap.value = combined
            ctx.supplemented[gap.key] = combined
        else:
            gap.source = "missing"
            gap.note = "互联网搜索无有效结果，视为缺失"
            ctx.still_missing.append(gap.label)

    ctx.gaps = gaps
    return ctx


def gaps_report(ctx: AnnualContext) -> str:
    mode = ctx.agent_mode
    header = f"## {ctx.apply_year} 年初定方向 — 数据就绪报告"
    if mode:
        tag = "回测" if mode.is_backtest else "实盘"
        header += f" [{tag} | 知识截止 {mode.as_of_date}]"
    lines = [header + "\n"]
    if mode and mode.is_backtest:
        lines.append(
            "> 回测模式：仅使用数据库中截止 "
            f"{mode.as_of_date} 的数据；已禁用网络搜索，避免上帝视角。\n"
        )
    lines.append("### 数据库已有")
    for g in ctx.gaps:
        if g.source == "db":
            lines.append(f"- ✅ {g.label}")

    lines.append("\n### 网络补充")
    for g in ctx.gaps:
        if g.source == "web":
            lines.append(f"- 🌐 {g.label}")
            if isinstance(g.value, list) and g.value:
                lines.append(f"  - {g.value[0].get('snippet', '')[:200]}...")

    lines.append("\n### 仍缺失（分析时降权或忽略）")
    for label in ctx.still_missing:
        lines.append(f"- ⚠️ {label}")
    for g in ctx.gaps:
        if g.source == "missing" and g.label not in ctx.still_missing:
            lines.append(f"- ⚠️ {g.label}")

    return "\n".join(lines)
