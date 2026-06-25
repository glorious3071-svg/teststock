"""
multi_year_runner.py — v5.0 多年回测调度器

把三层架构串起来：
- Layer 1 战略层（年）: 由 LLM Agent 或评分卡决定年初目标配置（TargetAllocation）
- Layer 2 战术层（月）: 触发器引擎评估月度信号，记录触发事件
- Layer 3 状态层（日）: 复用 teststock 的 engine.run_strategic_allocation 做年内持有

调度循环：
    for year in [start..end]:
        # 1. 年初战略：从 strategy_provider 拿到当年的 TargetAllocation
        target = strategy_provider(year, snapshot_at_start_of_year)
        # 2. 年内持有：用 engine.run_strategic_allocation 跑该年
        result = engine.run_strategic_allocation(prices, etf_allocations, cfg)
        # 3. 月度评估：触发器引擎对每月信号做一遍评估，事件计入 trigger_log
        for month in months_in_year:
            triggers = trigger_engine.evaluate_month(...)
        # 4. 把终端权益作为下一年起点

注意：
- 本版"月度触发器评估"是 dry-run 模式 —— 仅记录触发事件，不在年内换仓
- 在年内换仓的全闭环属于 Phase 3 工作（要扩展 engine 支持月度 rebalance）
- 这样做能先验证战略层 + 月度信号的协调性，再加大复杂度

红线：
- as_of_date 严格控制：策略提供器只能看 year-1 及以前的数据
- 触发器评估同样：每月只用当月及之前的信号
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import pandas as pd

from backtest.engine import BacktestConfig, BacktestResult, run_strategic_allocation
from backtest.portfolio import Portfolio, TargetAllocation
from backtest.trigger_engine import MonthlySignals, TriggerEngine, TriggerResult


# =====================================================
# 战略层接口
# =====================================================
StrategyProvider = Callable[[int, "YearOpeningContext"], TargetAllocation]
"""
战略层提供器：年初决策函数。
入参: (year, snapshot_截至 year-1-12-31)
出参: TargetAllocation
"""


@dataclass
class YearOpeningContext:
    """年初战略快照 — 只含 year-1 及以前数据"""
    year: int
    prev_total_equity: float
    prev_signals_history: list[MonthlySignals] = field(default_factory=list)
    notes: str = ""


# =====================================================
# 月度信号加载（从 signals.db）
# =====================================================
def load_monthly_signals_from_db(
    conn: sqlite3.Connection,
    year: int,
) -> List[MonthlySignals]:
    """从 signal_monthly + valuation_monthly 加载某年 12 个月的信号"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.year_month, s.cs300_pe_ttm, s.cs300_pb,
               s.cs300_pct, s.cs300_3m_cum_pct,
               s.rrr_cut_in_month, s.rate_cut_in_month, s.policy_tone_pos,
               v.trade_date
        FROM signal_monthly s
        LEFT JOIN valuation_monthly v
          ON v.year_month = s.year_month AND v.ts_code = '000300.SH'
        WHERE s.year_month LIKE ?
        ORDER BY s.year_month
        """,
        (f"{year:04d}%",),
    )
    out = []
    for row in cur.fetchall():
        ym, pe, pb, pct, cum3m, rrr, rate, tone, td = row
        out.append(MonthlySignals(
            year_month=ym,
            trade_date=td or f"{ym}__",
            cs300_pe_ttm=pe,
            cs300_pb=pb,
            cs300_pct=pct,
            cs300_3m_pct=cum3m,
            rrr_cut_in_month=bool(rrr),
            rate_cut_in_month=bool(rate),
            policy_tone_positive=bool(tone),
        ))
    return out


# =====================================================
# 月度触发器日志条目
# =====================================================
@dataclass
class TriggerLogEntry:
    year: int
    year_month: str
    rule_id: str
    triggered: bool
    notes: str
    action: dict = field(default_factory=dict)


# =====================================================
# 多年回测结果
# =====================================================
@dataclass
class MultiYearResult:
    yearly_results: List[BacktestResult] = field(default_factory=list)
    trigger_log: List[TriggerLogEntry] = field(default_factory=list)
    final_equity: float = 0.0
    initial_cash: float = 0.0

    @property
    def total_return_pct(self) -> float:
        if self.initial_cash <= 0:
            return 0.0
        return (self.final_equity - self.initial_cash) / self.initial_cash * 100.0

    @property
    def years_covered(self) -> list[int]:
        return [r.config.start_date[:4] for r in self.yearly_results]

    def trigger_summary(self) -> dict:
        """按 rule_id 汇总触发次数"""
        summary: dict[str, int] = {}
        for entry in self.trigger_log:
            if entry.triggered:
                summary[entry.rule_id] = summary.get(entry.rule_id, 0) + 1
        return summary


# =====================================================
# 主调度器
# =====================================================
def run_multi_year(
    prices: pd.DataFrame,
    strategy_provider: StrategyProvider,
    initial_cash: float,
    start_year: int,
    end_year: int,
    signals_db_conn: Optional[sqlite3.Connection] = None,
    trigger_engine: Optional[TriggerEngine] = None,
    commission_rate: float = 0.0003,
) -> MultiYearResult:
    """
    多年回测主调度器。

    Args:
        prices: pd.DataFrame, index=日期, columns=ts_code, values=close
        strategy_provider: 年初战略决策函数（你的 LLM Agent 或评分卡）
        initial_cash: 起始资金
        start_year/end_year: 闭区间
        signals_db_conn: SQLite signals.db 连接（用于触发器评估）
        trigger_engine: 触发器引擎实例，None 用默认 v4.0_v3 四规则
        commission_rate: 单边佣金率
    """
    if trigger_engine is None:
        trigger_engine = TriggerEngine()

    result = MultiYearResult(initial_cash=initial_cash)
    current_cash = initial_cash
    signals_history: List[MonthlySignals] = []

    for year in range(start_year, end_year + 1):
        # ===== Layer 1 战略层: 年初决策 =====
        ctx = YearOpeningContext(
            year=year,
            prev_total_equity=current_cash,
            prev_signals_history=list(signals_history),
        )
        target = strategy_provider(year, ctx)

        # 转换为 engine 接受的格式
        etf_allocations = [
            {"ts_code": code, "weight_pct": w}
            for code, w in target.etf_targets.items()
        ]

        # 注：现金权重隐式 = 100 - sum(weights), engine 会按权重买入
        # ===== Layer 3 状态层: 年内持有 =====
        cfg = BacktestConfig(
            initial_cash=current_cash,
            start_date=f"{year}-01-01",
            end_date=f"{year}-12-31",
            commission_rate=commission_rate,
        )

        try:
            year_result = run_strategic_allocation(
                prices=prices,
                etf_allocations=etf_allocations,
                config=cfg,
            )
            result.yearly_results.append(year_result)
            # 年终权益（最后一行的 total_equity）作为下一年起点
            current_cash = float(year_result.equity_curve.iloc[-1]["equity"])
        except (ValueError, KeyError) as e:
            # 数据缺失/对齐问题，记录并跳过该年
            print(f"[WARN] {year} 年回测失败: {e}")
            continue

        # ===== Layer 2 战术层: 月度触发器评估（dry-run）=====
        if signals_db_conn is not None:
            year_signals = load_monthly_signals_from_db(signals_db_conn, year)
            # 用一个临时 portfolio 跟踪触发器锁定状态
            dry_run_port = Portfolio(initial_cash=current_cash)
            dry_run_port.target = target

            for sig in year_signals:
                trig_results: list[TriggerResult] = trigger_engine.evaluate_month(
                    signals=sig, portfolio=dry_run_port,
                )
                for tr in trig_results:
                    result.trigger_log.append(TriggerLogEntry(
                        year=year, year_month=sig.year_month,
                        rule_id=tr.rule_id, triggered=tr.triggered,
                        notes=tr.notes, action=tr.action,
                    ))
                signals_history.append(sig)

    result.final_equity = current_cash
    return result


# =====================================================
# 简单战略提供器（用于测试 / 基线）
# =====================================================
def fixed_allocation_provider(
    etf_targets: dict[str, float],
    cash_pct: float = 0.0,
) -> StrategyProvider:
    """
    固定权重战略 — 每年同样的目标配置。
    用作基线对照（不调仓 = 上帝视角校验组）。
    """
    def _provider(year: int, ctx: YearOpeningContext) -> TargetAllocation:
        return TargetAllocation(
            apply_year=year,
            equity_weight_pct=100 - cash_pct,
            cash_weight_pct=cash_pct,
            etf_targets=dict(etf_targets),
        )
    return _provider
