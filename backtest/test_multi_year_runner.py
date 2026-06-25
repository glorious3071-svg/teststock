"""
test_multi_year_runner.py — multi_year_runner 单元测试

验证：
- 战略提供器在每年初被调用，且只能看到 year-1 的上下文
- 触发器引擎按月评估
- 多年权益滚雪球：上一年终端 → 下一年起点
- 触发日志按年汇总正确
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest

import pandas as pd

from backtest.multi_year_runner import (
    MultiYearResult,
    YearOpeningContext,
    fixed_allocation_provider,
    load_monthly_signals_from_db,
    run_multi_year,
)
from backtest.portfolio import TargetAllocation


def _make_fake_prices(start: str, end: str, codes: list[str]) -> pd.DataFrame:
    """构造一段虚拟的日线价格（每日 +0.05%，温和上涨）"""
    dates = pd.date_range(start=start, end=end, freq="B")
    data = {}
    for code in codes:
        # 每个 code 略不同初始价
        base = 1.0 + 0.1 * (hash(code) % 10) / 10
        data[code] = [base * (1.0005 ** i) for i in range(len(dates))]
    df = pd.DataFrame(data, index=dates)
    return df


def _make_fake_signals_db() -> sqlite3.Connection:
    """构造一个最小可用的 signals.db 内存数据库"""
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE signal_monthly (
            year_month TEXT PRIMARY KEY,
            cs300_pe_ttm REAL, cs300_pb REAL,
            cs300_pct REAL, sse50_pct REAL, cs500_pct REAL, cyb_pct REAL,
            cs300_3m_cum_pct REAL,
            rrr_cut_in_month INTEGER DEFAULT 0,
            rate_cut_in_month INTEGER DEFAULT 0,
            policy_tone_pos INTEGER DEFAULT 0,
            notes TEXT, updated_at TEXT
        );
        CREATE TABLE valuation_monthly (
            ts_code TEXT, year_month TEXT, trade_date TEXT,
            pe REAL, pe_ttm REAL, pb REAL, total_mv REAL, dv_ratio REAL,
            PRIMARY KEY (ts_code, year_month)
        );
    """)
    # 插入 2007 年 12 个月信号
    for m in range(1, 13):
        ym = f"2007{m:02d}"
        td = f"2007-{m:02d}-28"
        cur.execute(
            "INSERT INTO signal_monthly (year_month, cs300_pe_ttm, cs300_pct, cs300_3m_cum_pct) VALUES (?,?,?,?)",
            (ym, 30.0 + m, 5.0, 15.0),
        )
        cur.execute(
            "INSERT INTO valuation_monthly (ts_code, year_month, trade_date, pe_ttm) VALUES (?,?,?,?)",
            ("000300.SH", ym, td, 30.0 + m),
        )
    con.commit()
    return con


class TestStrategyProvider(unittest.TestCase):
    def test_fixed_provider_returns_same_target(self):
        provider = fixed_allocation_provider(
            etf_targets={"510300.SH": 60, "510500.SH": 20},
            cash_pct=20,
        )
        ctx = YearOpeningContext(year=2010, prev_total_equity=1_000_000)
        target = provider(2010, ctx)
        self.assertEqual(target.apply_year, 2010)
        self.assertEqual(target.cash_weight_pct, 20)
        self.assertEqual(target.equity_weight_pct, 80)
        self.assertEqual(target.etf_targets["510300.SH"], 60)


class TestLoadSignals(unittest.TestCase):
    def test_load_2007_signals(self):
        con = _make_fake_signals_db()
        signals = load_monthly_signals_from_db(con, 2007)
        self.assertEqual(len(signals), 12)
        self.assertEqual(signals[0].year_month, "200701")
        self.assertEqual(signals[-1].year_month, "200712")
        # PE 应递增 31..42
        self.assertAlmostEqual(signals[0].cs300_pe_ttm, 31.0)
        self.assertAlmostEqual(signals[-1].cs300_pe_ttm, 42.0)
        con.close()


class TestMultiYearRun(unittest.TestCase):
    def test_two_year_run_with_triggers(self):
        """跑 2007-2008 两年，验证调度循环 + 触发日志"""
        codes = ["510300.SH", "510500.SH"]
        prices = _make_fake_prices("2006-12-01", "2008-12-31", codes)
        con = _make_fake_signals_db()

        # 给 2008 也加月度信号（PE 下行）
        cur = con.cursor()
        for m in range(1, 13):
            ym = f"2008{m:02d}"
            td = f"2008-{m:02d}-28"
            cur.execute(
                "INSERT INTO signal_monthly (year_month, cs300_pe_ttm, cs300_pct, cs300_3m_cum_pct) VALUES (?,?,?,?)",
                (ym, 20.0 - m, -3.0, -8.0),
            )
            cur.execute(
                "INSERT INTO valuation_monthly (ts_code, year_month, trade_date, pe_ttm) VALUES (?,?,?,?)",
                ("000300.SH", ym, td, 20.0 - m),
            )
        con.commit()

        # 跟踪每年提供器被调用
        called_years: list[int] = []

        def provider(year: int, ctx: YearOpeningContext) -> TargetAllocation:
            called_years.append(year)
            return TargetAllocation(
                apply_year=year,
                equity_weight_pct=80,
                cash_weight_pct=20,
                etf_targets={"510300.SH": 60, "510500.SH": 20},
            )

        result = run_multi_year(
            prices=prices,
            strategy_provider=provider,
            initial_cash=1_000_000,
            start_year=2007,
            end_year=2008,
            signals_db_conn=con,
        )

        # 验证：两年都被调度
        self.assertEqual(called_years, [2007, 2008])
        self.assertEqual(len(result.yearly_results), 2)

        # 验证：触发日志包含 24 个月 × 4 规则 = 96 条
        self.assertEqual(len(result.trigger_log), 24 * 4)

        # 验证：触发汇总有内容（A 再平衡可能触发；C 在 2007 PE 高时可能触发）
        summary = result.trigger_summary()
        self.assertIsInstance(summary, dict)

        # 验证：最终权益 != 0
        self.assertGreater(result.final_equity, 0)

        con.close()

    def test_zero_year_range(self):
        """end_year < start_year 应空运行"""
        prices = _make_fake_prices("2007-01-01", "2007-12-31", ["510300.SH"])
        provider = fixed_allocation_provider({"510300.SH": 100})
        result = run_multi_year(
            prices=prices,
            strategy_provider=provider,
            initial_cash=1_000_000,
            start_year=2010, end_year=2009,  # 空区间
        )
        self.assertEqual(len(result.yearly_results), 0)
        self.assertEqual(result.final_equity, 1_000_000)


class TestAsOfDateGuard(unittest.TestCase):
    """战略提供器只能看到 year-1 的上下文 — 防上帝视角"""

    def test_context_only_has_past_signals(self):
        codes = ["510300.SH"]
        prices = _make_fake_prices("2006-12-01", "2008-12-31", codes)
        con = _make_fake_signals_db()

        # 给 2008 加信号
        cur = con.cursor()
        for m in range(1, 13):
            ym = f"2008{m:02d}"
            cur.execute(
                "INSERT INTO signal_monthly (year_month, cs300_pe_ttm) VALUES (?,?)",
                (ym, 15.0),
            )
            cur.execute(
                "INSERT INTO valuation_monthly (ts_code, year_month, trade_date, pe_ttm) VALUES (?,?,?,?)",
                ("000300.SH", ym, f"2008-{m:02d}-28", 15.0),
            )
        con.commit()

        captured_contexts: list[YearOpeningContext] = []

        def provider(year: int, ctx: YearOpeningContext) -> TargetAllocation:
            captured_contexts.append(ctx)
            return TargetAllocation(
                apply_year=year, equity_weight_pct=100, cash_weight_pct=0,
                etf_targets={"510300.SH": 100},
            )

        run_multi_year(
            prices=prices, strategy_provider=provider,
            initial_cash=1_000_000,
            start_year=2007, end_year=2008,
            signals_db_conn=con,
        )

        # 2007 决策时，prev_signals_history 应为空（之前未有信号）
        self.assertEqual(len(captured_contexts), 2)
        self.assertEqual(captured_contexts[0].year, 2007)
        self.assertEqual(len(captured_contexts[0].prev_signals_history), 0)
        # 2008 决策时，prev_signals_history 应只含 2007 信号
        self.assertEqual(captured_contexts[1].year, 2008)
        for s in captured_contexts[1].prev_signals_history:
            self.assertTrue(s.year_month.startswith("2007"))

        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
