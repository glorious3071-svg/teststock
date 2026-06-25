"""
signal_loader.py — 从原始日线/估值/政策事件数据构建 signal_monthly 月度信号表。

输入来源：
- valuation_monthly: 沪深 300 月末 PE_TTM / PB（已有 240 月）
- daily_quotes: 各指数日线 → 聚合为月线 → 计算月度涨跌幅
- policy_events: 央行政策事件 → 标记当月是否降准/降息

输出：
- signal_monthly 一张宽表，trigger_engine 直接消费

兼容 SQLite 源（portfolio/signals_db/signals.db）和 MySQL 目标（teststock）。
本文件先实现 SQLite → SQLite 的同库回灌，MySQL 适配通过统一 DAO 后续扩展。

红线：
- 严防上帝视角：year_month 的所有信号必须基于 ≤ 当月最后一个交易日的数据计算
- 不前看：月末 PE 取该月最后一个交易日的值
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# 各指数在 signal_monthly 里对应的列名
INDEX_TO_COLUMN = {
    "000300.SH": "cs300_pct",   # 沪深 300
    "000016.SH": "sse50_pct",   # 上证 50
    "000905.SH": "cs500_pct",   # 中证 500
    "399006.SZ": "cyb_pct",     # 创业板
}


@dataclass
class MonthlyQuote:
    """单个指数某月的月度行情。"""

    ts_code: str
    year_month: str
    last_trade_date: str
    close: float
    pct_chg: float  # 当月涨跌幅 %


def aggregate_daily_to_monthly(
    conn: sqlite3.Connection,
    ts_codes: List[str],
) -> Dict[Tuple[str, str], MonthlyQuote]:
    """
    将 daily_quotes 聚合为月度行情。

    返回: {(ts_code, year_month): MonthlyQuote}

    聚合规则：
    - 月末收盘 = 当月最后一个交易日的 close
    - 月度涨跌幅 = (本月末收盘 - 上月末收盘) / 上月末收盘 * 100
    - 第一个月的 pct_chg 为 None（无法计算）
    """
    result: Dict[Tuple[str, str], MonthlyQuote] = {}

    for code in ts_codes:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT trade_date, close
            FROM daily_quotes
            WHERE ts_code = ?
            ORDER BY trade_date
            """,
            (code,),
        )

        # 取每个 year_month 的最后一行
        month_to_last: Dict[str, Tuple[str, float]] = {}
        for trade_date, close in cur.fetchall():
            if close is None:
                continue
            ym = trade_date[:6]
            month_to_last[ym] = (trade_date, close)

        # 按月排序并计算月度涨跌幅
        sorted_months = sorted(month_to_last.keys())
        prev_close: Optional[float] = None
        for ym in sorted_months:
            td, close = month_to_last[ym]
            if prev_close is None or prev_close == 0:
                pct = 0.0  # 首月无 pct_chg，用 0 占位
            else:
                pct = (close - prev_close) / prev_close * 100.0
            result[(code, ym)] = MonthlyQuote(
                ts_code=code,
                year_month=ym,
                last_trade_date=td,
                close=close,
                pct_chg=pct,
            )
            prev_close = close

    return result


def compute_3m_cumulative(
    monthly_pct: Dict[str, float],
) -> Dict[str, float]:
    """
    计算 3 月累计涨跌幅（按几何复合）。

    公式: cum_3m = (1+r1)(1+r2)(1+r3) - 1, 全部用百分比

    返回: {year_month: cum_3m_pct}, 前两月没有完整 3 月窗口，跳过
    """
    sorted_months = sorted(monthly_pct.keys())
    result: Dict[str, float] = {}
    for i, ym in enumerate(sorted_months):
        if i < 2:
            continue
        r1 = monthly_pct[sorted_months[i - 2]] / 100.0
        r2 = monthly_pct[sorted_months[i - 1]] / 100.0
        r3 = monthly_pct[ym] / 100.0
        cum = (1 + r1) * (1 + r2) * (1 + r3) - 1
        result[ym] = cum * 100.0
    return result


def load_policy_flags(conn: sqlite3.Connection) -> Dict[str, Dict[str, int]]:
    """
    从 policy_events 表读取每月的政策标记。

    返回: {year_month: {'rrr_cut': 0/1, 'rate_cut': 0/1, 'policy_tone_pos': 0/1}}

    映射规则：
    - rrr_cut_in_month: 当月存在 RRR_CUT 事件 → 1
    - rate_cut_in_month: 当月存在 RATE_CUT/MLF_CUT/OMO_CUT/LPR_CUT 任一 → 1
    - policy_tone_pos: 当月存在 POLICY_TONE 且 magnitude_bp > 0 → 1
    """
    cur = conn.cursor()
    cur.execute("SELECT event_date, event_type, magnitude_bp FROM policy_events")
    flags: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"rrr_cut": 0, "rate_cut": 0, "policy_tone_pos": 0}
    )
    rate_cut_types = {"RATE_CUT", "MLF_CUT", "OMO_CUT", "LPR_CUT"}
    for event_date, event_type, magnitude in cur.fetchall():
        ym = event_date[:6]
        if event_type == "RRR_CUT":
            flags[ym]["rrr_cut"] = 1
        elif event_type in rate_cut_types:
            flags[ym]["rate_cut"] = 1
        elif event_type == "POLICY_TONE" and magnitude and magnitude > 0:
            flags[ym]["policy_tone_pos"] = 1
    return dict(flags)


def build_signal_monthly(
    conn: sqlite3.Connection,
    truncate: bool = True,
) -> int:
    """
    构建 signal_monthly 宽表，返回插入行数。

    Args:
        conn: SQLite 连接（signals.db）
        truncate: True 时先清空 signal_monthly
    """
    # 1. 月度聚合所有目标指数
    ts_codes = list(INDEX_TO_COLUMN.keys())
    monthly_quotes = aggregate_daily_to_monthly(conn, ts_codes)

    # 2. 沪深 300 的 3 月累计动量
    cs300_pct_by_month = {
        ym: q.pct_chg
        for (code, ym), q in monthly_quotes.items()
        if code == "000300.SH"
    }
    cs300_3m = compute_3m_cumulative(cs300_pct_by_month)

    # 3. 读取估值数据（沪深 300 月末 PE_TTM / PB）
    cur = conn.cursor()
    cur.execute(
        """
        SELECT year_month, pe_ttm, pb
        FROM valuation_monthly
        WHERE ts_code = '000300.SH'
        """
    )
    pe_by_month: Dict[str, Tuple[Optional[float], Optional[float]]] = {
        ym: (pe_ttm, pb) for ym, pe_ttm, pb in cur.fetchall()
    }

    # 4. 读取政策标记
    policy_flags = load_policy_flags(conn)

    # 5. 汇总所有年月（取并集）
    all_months = (
        set(pe_by_month.keys())
        | {ym for (_, ym) in monthly_quotes.keys()}
    )

    # 6. 清空目标表
    if truncate:
        cur.execute("DELETE FROM signal_monthly")

    # 7. 逐月构建行
    inserted = 0
    for ym in sorted(all_months):
        pe_ttm, pb = pe_by_month.get(ym, (None, None))

        cs300 = monthly_quotes.get(("000300.SH", ym))
        sse50 = monthly_quotes.get(("000016.SH", ym))
        cs500 = monthly_quotes.get(("000905.SH", ym))
        cyb = monthly_quotes.get(("399006.SZ", ym))

        flags = policy_flags.get(ym, {"rrr_cut": 0, "rate_cut": 0, "policy_tone_pos": 0})

        cur.execute(
            """
            INSERT INTO signal_monthly (
                year_month, cs300_pe_ttm, cs300_pb,
                cs300_pct, sse50_pct, cs500_pct, cyb_pct,
                cs300_3m_cum_pct,
                rrr_cut_in_month, rate_cut_in_month, policy_tone_pos
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ym, pe_ttm, pb,
                cs300.pct_chg if cs300 else None,
                sse50.pct_chg if sse50 else None,
                cs500.pct_chg if cs500 else None,
                cyb.pct_chg if cyb else None,
                cs300_3m.get(ym),
                flags["rrr_cut"], flags["rate_cut"], flags["policy_tone_pos"],
            ),
        )
        inserted += 1

    conn.commit()
    return inserted


if __name__ == "__main__":
    import os
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else "/home/user/workspace/portfolio/signals_db/signals.db"
    if not os.path.exists(db_path):
        sys.exit(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    n = build_signal_monthly(conn)
    print(f"signal_monthly: 写入 {n} 行")

    # 抽样验证
    cur = conn.cursor()
    cur.execute(
        """
        SELECT year_month, cs300_pe_ttm, cs300_pct, cs300_3m_cum_pct
        FROM signal_monthly
        WHERE year_month IN ('200710','200810','201506','201902','202012')
        ORDER BY year_month
        """
    )
    print("\n关键节点抽样：")
    for r in cur.fetchall():
        print(f"  {r[0]}  PE={r[1]:.2f}  月={r[2]:+.2f}%  3M={r[3]:+.2f}%"
              if r[1] is not None and r[2] is not None and r[3] is not None
              else f"  {r[0]}  {r}")
    conn.close()
