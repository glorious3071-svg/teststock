#!/usr/bin/env python3
"""MCP server for 年初定方向 Agent — stdio transport.

Exposes tools so the Cursor agent can drive the full dialogue:
  prepare → start → chat → finalize → backtest
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from agents.annual_direction.agent import (  # noqa: E402
    continue_session,
    format_allocation_summary,
    load_session,
    prepare_context,
    session_path,
    start_session,
)
from agents.annual_direction.gaps import gaps_report  # noqa: E402
from agents.annual_direction.mode import resolve_mode  # noqa: E402

mcp = FastMCP(
    "annual-direction",
    instructions=(
        "年初定方向投资顾问 MCP。典型流程："
        "1) annual_direction_status 查看会话；"
        "2) annual_direction_start 收集数据并生成初稿；"
        "3) annual_direction_chat 传递用户追问直到定稿；"
        "4) annual_direction_backtest 定稿后回测。"
        "用户说「确认」「定稿」时用 annual_direction_chat 传递该消息。"
    ),
)


def _status_payload(year: int, mode: str = "auto") -> dict:
    session = load_session(year, mode=mode)
    resolved = resolve_mode(year, mode=mode)
    if not session:
        return {
            "year": year,
            "mode": resolved.mode,
            "knowledge_cutoff": resolved.as_of_date,
            "session_exists": False,
            "finalized": False,
            "session_file": str(session_path(year, mode=mode)),
        }
    return {
        "year": year,
        "mode": session.mode,
        "knowledge_cutoff": resolved.as_of_date,
        "is_backtest": resolved.is_backtest,
        "session_exists": True,
        "finalized": session.finalized,
        "session_file": str(session_path(year, mode=mode)),
        "allocation_summary": format_allocation_summary(session.last_allocation),
        "last_allocation": session.last_allocation,
        "message_count": len(session.messages),
    }


@mcp.tool()
def annual_direction_status(year: int, mode: str = "auto") -> str:
    """查看指定年份的定方向会话状态（是否存在、是否已定稿、当前配置摘要）。"""
    return json.dumps(_status_payload(year, mode), ensure_ascii=False, indent=2)


@mcp.tool()
def annual_direction_prepare(year: int, mode: str = "auto") -> str:
    """仅收集数据并返回缺口报告，不调用 LLM。"""
    resolved = resolve_mode(year, mode=mode)
    ctx = prepare_context(year, enable_web=not resolved.is_backtest, mode=mode)
    return gaps_report(ctx)


@mcp.tool()
def annual_direction_start(
    year: int,
    mode: str = "auto",
    fresh: bool = False,
) -> str:
    """收集数据并调用 LLM 生成战略配置初稿。fresh=true 时忽略已有会话重新生成。"""
    if fresh:
        pass  # 始终落到下方 start_session
    elif (session := load_session(year, mode=mode)) and session.finalized:
        return json.dumps(
            {
                "action": "already_finalized",
                "hint": "该年度已定稿。传 fresh=true 重新讨论，或调用 annual_direction_backtest 回测。",
                **_status_payload(year, mode),
            },
            ensure_ascii=False,
            indent=2,
        )
    elif (session := load_session(year, mode=mode)) and any(
        m.get("role") == "assistant" for m in session.messages
    ) and not fresh:
        last = next(m["content"] for m in reversed(session.messages) if m["role"] == "assistant")
        return json.dumps(
            {
                "action": "resumed_existing",
                "hint": "已有初稿，请用 annual_direction_chat 继续追问或定稿。",
                "agent_reply": last,
                **_status_payload(year, mode),
            },
            ensure_ascii=False,
            indent=2,
        )

    resolved = resolve_mode(year, mode=mode)
    enable_web = not resolved.is_backtest
    session = start_session(year, enable_web=enable_web, use_llm=True, mode=mode)
    reply = next(m["content"] for m in reversed(session.messages) if m["role"] == "assistant")
    return json.dumps(
        {
            "action": "started",
            "agent_reply": reply,
            "data_gaps_report": gaps_report(session.context) if session.context.gaps else None,
            **_status_payload(year, mode),
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


@mcp.tool()
def annual_direction_chat(year: int, message: str, mode: str = "auto") -> str:
    """向定方向 Agent 发送用户消息（追问、修订、确认定稿），返回 Agent 回复与最新配置。"""
    session = load_session(year, mode=mode)
    if not session:
        return json.dumps(
            {
                "error": "no_session",
                "hint": f"请先调用 annual_direction_start(year={year})",
            },
            ensure_ascii=False,
        )

    if session.finalized and message.strip() not in ("",):
        # allow re-open only with explicit fresh on start
        return json.dumps(
            {
                "error": "already_finalized",
                "hint": "已定稿。如需修改请 annual_direction_start(fresh=true)。",
                **_status_payload(year, mode),
            },
            ensure_ascii=False,
            indent=2,
        )

    session = continue_session(session, message)
    reply = session.messages[-1]["content"]
    return json.dumps(
        {
            "action": "finalized" if session.finalized else "revised",
            "agent_reply": reply,
            "finalized": session.finalized,
            "allocation_summary": format_allocation_summary(session.last_allocation),
            "last_allocation": session.last_allocation,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


@mcp.tool()
def annual_direction_backtest(year: int, initial_cash: float = 1_000_000, mode: str = "auto") -> str:
    """对已定稿的年度配置执行买入持有回测（全年战略仓位）。"""
    session = load_session(year, mode=mode)
    if not session or not session.last_allocation:
        return json.dumps({"error": "no_allocation", "hint": "请先完成定方向"}, ensure_ascii=False)
    if not session.finalized:
        return json.dumps(
            {
                "error": "not_finalized",
                "hint": "配置尚未定稿。请用户确认后发送「确认」或「定稿」。",
                "allocation_summary": format_allocation_summary(session.last_allocation),
            },
            ensure_ascii=False,
            indent=2,
        )

    from backtest.data import fetch_etf_daily
    from backtest.engine import BacktestConfig, format_metrics, run_strategic_allocation

    alloc = session.last_allocation
    etf_allocs = alloc.get("etf_allocations") or []
    codes = [a["ts_code"] for a in etf_allocs]
    start = f"{year}-01-01"
    end = f"{year}-12-31"

    prices = fetch_etf_daily(codes, start, end)
    if prices.empty:
        return json.dumps({"error": "no_prices"}, ensure_ascii=False)

    config = BacktestConfig(initial_cash=initial_cash, start_date=start, end_date=end)
    result = run_strategic_allocation(prices, etf_allocs, config)

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"annual_{year}"
    result.equity_curve.to_csv(out_dir / f"{tag}_equity.csv")
    result.trades.to_csv(out_dir / f"{tag}_trades.csv", index=False)

    summary = {
        "year": year,
        "initial_cash": initial_cash,
        "metrics_text": format_metrics(result.metrics),
        "metrics": result.metrics,
        "first_trade_date": result.trades.iloc[0]["trade_date"] if not result.trades.empty else None,
        "equity_csv": str(out_dir / f"{tag}_equity.csv"),
    }
    (out_dir / f"{tag}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return json.dumps(summary, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
