"""Backtest vs live mode — prevent look-ahead bias."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class AgentMode:
    """Decision context for annual direction."""

    apply_year: int
    mode: str  # "backtest" | "live"
    as_of_date: str  # ISO date, inclusive knowledge cutoff
    decision_date: str  # when the allocation takes effect

    @property
    def is_backtest(self) -> bool:
        return self.mode == "backtest"

    def to_prompt_dict(self) -> dict[str, str | bool]:
        return {
            "mode": self.mode,
            "is_backtest": self.is_backtest,
            "apply_year": str(self.apply_year),
            "decision_date": self.decision_date,
            "knowledge_cutoff": self.as_of_date,
            "web_search_allowed": not self.is_backtest,
        }


def default_as_of(apply_year: int) -> str:
    """年初定方向：仅使用上年 12-31 及之前已发布的数据。"""
    return f"{apply_year - 1}-12-31"


def resolve_mode(apply_year: int, *, mode: str = "auto") -> AgentMode:
    """
    Resolve agent mode.

    - backtest: apply_year 早于当前自然年 → 严禁未来信息与网络搜索
    - live: 当年或未来年份的实盘/前瞻定方向
    - auto: apply_year < today.year → backtest，否则 live
    """
    today = date.today()
    if mode not in ("auto", "backtest", "live"):
        raise ValueError(f"invalid mode: {mode}")

    if mode == "auto":
        resolved = "backtest" if apply_year < today.year else "live"
    else:
        resolved = mode

    as_of = default_as_of(apply_year)
    return AgentMode(
        apply_year=apply_year,
        mode=resolved,
        as_of_date=as_of,
        decision_date=f"{apply_year}-01-01",
    )
