"""
sanity_check_triggers.py — 在历史关键节点上跑触发器引擎，验证它能否正确反应。

这不是完整回测，只是单点 smoke test：把每个关键月份的信号喂给触发器，
看 C（减仓）/B（政策加仓）/D（深度加仓）是否在合理的位置上点亮。

红线：
- 严防上帝视角：评估时只用当月及之前已经发生的数据
- 不调阈值救场：v4.0 v3 阈值固定（PE>16/<12/<11，单月 ±10%/±15%，3M ±20%/±25%）

预期对照（人工标注的历史事实）：
- 2007-10 PE≈47, 大涨 → C 触发器应点亮
- 2007-11 紧接顶后开始跌 → C 不再触发（PE 仍高但月跌）
- 2008-10 PE≈12.5, 单月 -26% → B/D 候选区
- 2008-11 沪深 300 大幅波动 → 视当时政策
- 2015-06 PE>16, 大涨 → C 触发器应点亮（牛市顶）
- 2019-01/02 PE≈11, 单月大涨 → D 不该触发（D 要求月跌）
"""

from __future__ import annotations

import os
import sqlite3
import sys

# 添加项目根到 sys.path，便于直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.portfolio import Portfolio, Position, TargetAllocation  # noqa: E402
from backtest.trigger_engine import (  # noqa: E402
    MonthlySignals,
    TriggerEngine,
)


DB_PATH = "/home/user/workspace/portfolio/signals_db/signals.db"


def load_signal(conn: sqlite3.Connection, year_month: str) -> MonthlySignals:
    """从 signal_monthly + valuation_monthly 加载某月信号。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.cs300_pe_ttm, s.cs300_pb, s.cs300_pct, s.cs300_3m_cum_pct,
               s.rrr_cut_in_month, s.rate_cut_in_month, s.policy_tone_pos,
               v.trade_date
        FROM signal_monthly s
        LEFT JOIN valuation_monthly v
          ON v.year_month = s.year_month AND v.ts_code = '000300.SH'
        WHERE s.year_month = ?
        """,
        (year_month,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"No signal for {year_month}")
    pe, pb, pct, cum3m, rrr, rate, tone, td = row
    return MonthlySignals(
        year_month=year_month,
        trade_date=td or f"{year_month}__",
        cs300_pe_ttm=pe,
        cs300_pb=pb,
        cs300_pct=pct,
        cs300_3m_pct=cum3m,
        rrr_cut_in_month=bool(rrr),
        rate_cut_in_month=bool(rate),
        policy_tone_positive=bool(tone),
    )


def make_default_portfolio(year: int = 2007) -> Portfolio:
    """
    构造一个示例账户（总资产 100,000）：
    - 510300.SH 持仓市值 70,000 (70%)
    - 510500.SH 持仓市值 20,000 (20%)
    - 现金 10,000 (10%)

    目标配置：沪深 300 50%，中证 500 20%，现金 30%
    -> 沪深 300 偏离 +20pp，会触发 A 再平衡
    -> 现金 10pp 低于目标，但仍有粮食给 B/D
    """
    p = Portfolio(initial_cash=10_000.0)
    p.cash = 10_000.0
    p.positions["510300.SH"] = Position(
        ts_code="510300.SH", shares=700.0, last_price=100.0
    )
    p.positions["510500.SH"] = Position(
        ts_code="510500.SH", shares=200.0, last_price=100.0
    )
    p.target = TargetAllocation(
        apply_year=year,
        equity_weight_pct=70.0,
        cash_weight_pct=30.0,
        etf_targets={"510300.SH": 50.0, "510500.SH": 20.0},
    )
    return p


def evaluate_one(engine: TriggerEngine, port: Portfolio, sig: MonthlySignals):
    """在某月运行一次评估并返回 (rule_id, triggered) 列表。"""
    results = engine.evaluate_month(signals=sig, portfolio=port)
    return [(r.rule_id, r.triggered, r.notes) for r in results]


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)

    # 我们关注的关键节点
    checkpoints = [
        ("200707", "2007 牛市中段（PE 已高）"),
        ("200710", "2007 牛市顶（PE 极值）"),
        ("200711", "2007 顶后回落"),
        ("200810", "2008 熊市深处"),
        ("200811", "2008 政策底前夕"),
        ("201412", "2014 年底快牛启动"),
        ("201504", "2015 牛市狂奔 4 月（真正过热点）"),
        ("201506", "2015 牛市顶 6 月（动能衰减）"),
        ("201507", "2015 千股跌停后"),
        ("201902", "2019 熊转牛 V 反转"),
        ("202012", "2020 疫情后高估"),
    ]

    print("=" * 100)
    print(f"{'年月':<8}{'PE_TTM':>9}{'月%':>9}{'3M%':>9}{'RRR':>5}{'RATE':>6}  触发结果")
    print("-" * 100)

    for ym, desc in checkpoints:
        try:
            sig = load_signal(conn, ym)
        except ValueError as e:
            print(f"{ym}  [跳过：{e}]")
            continue

        # 每个 checkpoint 用独立 portfolio + engine，避免相互污染锁定状态
        port = make_default_portfolio(year=int(ym[:4]))
        engine = TriggerEngine()

        results = evaluate_one(engine, port, sig)
        triggered = [r for r in results if r[1]]

        if triggered:
            badge = "  ".join(f"[{rid}] {note}" for rid, _, note in triggered)
        else:
            badge = "（无触发）"

        print(
            f"{ym:<8}{sig.cs300_pe_ttm:>9.2f}{sig.cs300_pct:>+9.2f}{sig.cs300_3m_pct:>+9.2f}"
            f"{int(sig.rrr_cut_in_month):>5}{int(sig.rate_cut_in_month):>6}  {badge}"
        )
        print(f"        ↳ {desc}")

    print("=" * 100)
    print("注：政策事件表当前为空，故 B 触发器（PE<12 + 当月降准/降息）不会点亮。")
    print("     B 触发器待 policy_events 回灌后再次验证。")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
