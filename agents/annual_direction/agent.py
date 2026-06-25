"""Annual direction agent orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.annual_direction.context import build_context
from agents.annual_direction.gaps import fill_gaps, gaps_report
from agents.annual_direction.llm_client import LLMError, chat, extract_allocation_json
from agents.annual_direction.models import AnnualContext
from agents.annual_direction.prompts import GATHER_PROMPT, build_system_prompt

ROOT = Path(__file__).resolve().parents[2]
SESSION_DIR = ROOT / "data" / "annual_direction_sessions"

FINALIZE_KEYWORDS = ("确认", "定稿", "同意", "就这样", "可以了", "没问题", "采纳")


@dataclass
class AgentSession:
    apply_year: int
    context: AnnualContext
    mode: str = "auto"
    messages: list[dict[str, str]] = field(default_factory=list)
    finalized: bool = False
    last_allocation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "apply_year": self.apply_year,
            "mode": self.mode,
            "finalized": self.finalized,
            "last_allocation": self.last_allocation,
            "context": self.context.to_prompt_dict(),
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSession":
        from agents.annual_direction.mode import resolve_mode

        raw_ctx = data.get("context", {})
        ctx = AnnualContext(apply_year=data["apply_year"])
        ctx.supplemented = raw_ctx.get("supplemented_from_web", {})
        ctx.still_missing = raw_ctx.get("still_missing", [])
        ctx.macro_brief = raw_ctx.get("macro_brief", "")
        ctx.cewc = raw_ctx.get("cewc")
        ctx.macro_snapshot = raw_ctx.get("macro_snapshot")
        mode = data.get("mode", "auto")
        ctx.agent_mode = resolve_mode(data["apply_year"], mode=mode)
        return cls(
            apply_year=data["apply_year"],
            context=ctx,
            mode=mode,
            messages=data.get("messages", []),
            finalized=data.get("finalized", False),
            last_allocation=data.get("last_allocation"),
        )


def session_path(apply_year: int, *, mode: str = "auto") -> Path:
    from agents.annual_direction.mode import resolve_mode

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    resolved = resolve_mode(apply_year, mode=mode)
    if resolved.is_backtest:
        return SESSION_DIR / f"backtest_{apply_year}.json"
    return SESSION_DIR / f"{apply_year}.json"


def save_session(session: AgentSession) -> Path:
    path = session_path(session.apply_year, mode=session.mode)
    path.write_text(
        json.dumps(session.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def load_session(apply_year: int, *, mode: str = "auto") -> AgentSession | None:
    path = session_path(apply_year, mode=mode)
    if not path.exists() and mode == "auto":
        # fallback: legacy live session file
        legacy = SESSION_DIR / f"{apply_year}.json"
        if legacy.exists():
            path = legacy
    if not path.exists():
        return None
    return AgentSession.from_dict(json.loads(path.read_text(encoding="utf-8")))


def prepare_context(apply_year: int, *, enable_web: bool = True, mode: str = "auto") -> AnnualContext:
    ctx = build_context(apply_year, mode=mode)
    return fill_gaps(ctx, enable_web=enable_web)


def start_session(
    apply_year: int,
    *,
    enable_web: bool = True,
    use_llm: bool = True,
    mode: str = "auto",
) -> AgentSession:
    ctx = prepare_context(apply_year, enable_web=enable_web, mode=mode)
    session = AgentSession(apply_year=apply_year, context=ctx, mode=mode)

    report = gaps_report(ctx)
    data_json = json.dumps(ctx.to_prompt_dict(), ensure_ascii=False, indent=2, default=str)

    if not use_llm:
        session.messages.append({"role": "assistant", "content": report + "\n\n---\n\n" + data_json})
        save_session(session)
        return session

    user_content = GATHER_PROMPT.format(apply_year=apply_year, data_json=data_json)
    mode_dict = ctx.agent_mode.to_prompt_dict() if ctx.agent_mode else None
    session.messages = [
        {"role": "system", "content": build_system_prompt(mode_dict)},
        {"role": "user", "content": report + "\n\n" + user_content},
    ]

    try:
        reply = chat(session.messages)
    except LLMError as e:
        session.messages.append({"role": "assistant", "content": f"{report}\n\n（LLM 未就绪: {e}）\n\n{data_json}"})
        save_session(session)
        return session

    session.messages.append({"role": "assistant", "content": reply})
    session.last_allocation = extract_allocation_json(reply)
    if session.last_allocation and session.last_allocation.get("finalized"):
        session.finalized = True
    save_session(session)
    return session


def is_finalize_intent(user_input: str) -> bool:
    text = user_input.strip()
    if not text:
        return False
    if text in FINALIZE_KEYWORDS:
        return True
    return any(kw in text for kw in FINALIZE_KEYWORDS) and len(text) <= 12


def _wrap_user_message(session: AgentSession, user_input: str) -> str:
    parts: list[str] = []
    if session.context.agent_mode and session.context.agent_mode.is_backtest:
        cutoff = session.context.agent_mode.as_of_date
        parts.append(f"[回测约束] 知识截止 {cutoff}，请勿引入此后信息。")
    if is_finalize_intent(user_input):
        parts.append(
            "[用户意图：确认定稿] 用户已表示认可当前方案，请输出最终配置 JSON，"
            '务必设置 "finalized": true，并简要复述最终宏观结论。'
        )
    parts.append(f"用户：{user_input}")
    return "\n".join(parts)


def continue_session(session: AgentSession, user_input: str) -> AgentSession:
    session.messages.append({"role": "user", "content": _wrap_user_message(session, user_input)})
    reply = chat(session.messages)
    session.messages.append({"role": "assistant", "content": reply})
    alloc = extract_allocation_json(reply)
    if alloc:
        session.last_allocation = alloc
        if alloc.get("finalized"):
            session.finalized = True
    elif is_finalize_intent(user_input) and session.last_allocation:
        # 用户已确认但模型未输出 finalized 字段时，仍标记定稿
        session.finalized = True
        session.last_allocation = {**session.last_allocation, "finalized": True}
    save_session(session)
    return session


def last_assistant_message(session: AgentSession) -> str | None:
    for msg in reversed(session.messages):
        if msg["role"] == "assistant":
            return msg["content"]
    return None


def print_session_header(session: AgentSession) -> None:
    from agents.annual_direction.mode import resolve_mode

    resolved = resolve_mode(session.apply_year, mode=session.mode)
    tag = "回测" if resolved.is_backtest else "实盘"
    status = "已定稿" if session.finalized else "讨论中（初稿/修订）"
    print(f"=== {session.apply_year} 年初定方向 [{tag} | 知识截止 {resolved.as_of_date}] — {status} ===\n")


def print_latest_reply(session: AgentSession) -> None:
    reply = last_assistant_message(session)
    if reply:
        print(reply)
    if session.last_allocation:
        print("\n" + "=" * 50)
        print("当前配置摘要:")
        print(format_allocation_summary(session.last_allocation))
        if not session.finalized:
            print("\n（初稿，尚未定稿 — 可继续追问或回复「确认」定稿）")


def format_allocation_summary(alloc: dict[str, Any] | None) -> str:
    if not alloc:
        return "（尚未解析到结构化配置 JSON）"
    lines = [
        f"权益仓位: {alloc.get('equity_weight_pct', '?')}%",
        f"现金仓位: {alloc.get('cash_weight_pct', '?')}%",
        "",
        "ETF 配置:",
    ]
    for item in alloc.get("etf_allocations") or []:
        lines.append(
            f"  - {item.get('ts_code')} {item.get('name')} "
            f"({item.get('theme')}) {item.get('weight_pct')}%"
        )
    if alloc.get("key_risks"):
        lines.append("\n主要风险:")
        for r in alloc["key_risks"]:
            lines.append(f"  - {r}")
    return "\n".join(lines)
