#!/usr/bin/env python3
"""2026-Q1 行业指数排行榜

按政策信号强度 × 指数-题材关联强度对指数打分，不引入动量/估值等市场数据。

  政策分 = signal_strength_score(强3/中2/弱1) × relevance_score(强3/中2/弱1)  → 1-9

Usage:
  python scripts/rank_2026q1_indices.py
  python scripts/rank_2026q1_indices.py --top 30
  python scripts/rank_2026q1_indices.py --suffix CSI
  python scripts/rank_2026q1_indices.py --suffix all
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import argparse
from collections import defaultdict
from datetime import date

import pymysql
from dotenv import load_dotenv

STRENGTH_SCORE = {"强": 3, "中": 2, "弱": 1}
MOMENTUM_WINDOW_DAYS = 125   # ~半年交易日
VALUATION_HISTORY_YEARS = 5  # PB 百分位参照区间（5年）

AS_OF_DATE = "2026-01-01"    # 政策信号截止日


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host":     os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


def fetch_signals(conn) -> list[dict]:
    """返回 2026-Q1 所有题材信号。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT theme, signal_strength, policy_basis
               FROM annual_sector_signals
               WHERE as_of_date = %s
               ORDER BY FIELD(signal_strength,'强','中','弱'), theme""",
            (AS_OF_DATE,),
        )
        return [{"theme": r[0], "signal_strength": r[1], "policy_basis": r[2]}
                for r in cur.fetchall()]


def fetch_theme_map(conn, suffix: str) -> list[dict]:
    """返回指定后缀的指数-题材映射。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ts_code, index_name, theme, relevance, reason
               FROM theme_index_map
               WHERE ts_code LIKE %s
               ORDER BY ts_code""",
            (f"%.{suffix}",),
        )
        return [{"ts_code": r[0], "index_name": r[1], "theme": r[2],
                 "relevance": r[3], "reason": r[4]}
                for r in cur.fetchall()]


def fetch_price_data(conn, suffix: str) -> dict[str, list]:
    """返回所有指定后缀指数的最近 N+30 天价格列表（ts_code → [(date, close)]）。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ts_code, trade_date, close
               FROM index_daily
               WHERE ts_code LIKE %s
                 AND trade_date >= '2024-12-01'
                 AND trade_date <= '2026-01-05'
               ORDER BY ts_code, trade_date""",
            (f"%.{suffix}",),
        )
        result: dict[str, list] = defaultdict(list)
        for ts_code, td, close in cur.fetchall():
            if close is not None:
                result[ts_code].append((td, float(close)))
    return result


def fetch_valuation_data(conn, suffix: str) -> dict[str, list]:
    """返回 5 年内 PB 历史数据（ts_code → [pb_value]）。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ts_code, pb
               FROM index_dailybasic
               WHERE ts_code LIKE %s
                 AND trade_date >= '2021-01-01'
                 AND trade_date <= '2026-01-05'
                 AND pb IS NOT NULL AND pb > 0
               ORDER BY ts_code, trade_date""",
            (f"%.{suffix}",),
        )
        result: dict[str, list] = defaultdict(list)
        for ts_code, pb in cur.fetchall():
            result[ts_code].append(float(pb))
    return result


def compute_momentum(price_series: list[tuple]) -> float | None:
    """用近 125 个交易日价格计算动量（涨跌幅）。"""
    if len(price_series) < 20:
        return None
    # 取最后一个价格（截止 2025 年底/2026 年初的最新价）
    # 以 2025-12-31 之前的最后一个为终点
    end_prices = [(d, c) for d, c in price_series if d <= date(2025, 12, 31)]
    if not end_prices:
        # 如果没有 2025 年底数据，用最新值
        end_prices = price_series
    end_date, end_close = end_prices[-1]

    # 往前找 ~125 个交易日前的价格
    lookback = [(d, c) for d, c in price_series if d <= end_date]
    if len(lookback) < MOMENTUM_WINDOW_DAYS:
        start_close = lookback[0][1]
    else:
        start_close = lookback[-MOMENTUM_WINDOW_DAYS][1]

    if start_close <= 0:
        return None
    return (end_close - start_close) / start_close


def compute_pb_percentile(pb_history: list[float], current_pb: float) -> float:
    """当前 PB 在历史中的百分位（越低=越便宜）。"""
    if not pb_history:
        return float("nan")
    below = sum(1 for v in pb_history if v < current_pb)
    return below / len(pb_history)


def percentile_rank(values: list[float]) -> list[float]:
    """把一组值转为各自的百分位（0~1，值越大百分位越高）。"""
    n = len(values)
    if n == 0:
        return []
    sorted_vals = sorted(values)
    return [sorted_vals.index(v) / max(n - 1, 1) for v in values]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top",    type=int, default=20, help="展示前 N 名")
    parser.add_argument("--suffix", default="SI",         help="SI / CSI / all")
    parser.add_argument("--min-signal", default="中",      help="最低政策信号强度（强/中/弱）")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        suffixes = ["SI", "CSI"] if args.suffix == "all" else [args.suffix.upper()]

        # ── 1. 政策信号 ──────────────────────────────────────────
        signals = fetch_signals(conn)
        min_score = STRENGTH_SCORE.get(args.min_signal, 2)
        active_signals = {
            s["theme"]: s for s in signals
            if STRENGTH_SCORE.get(s["signal_strength"], 0) >= min_score
        }
        print(f"\n=== 2026-Q1 政策信号（≥{args.min_signal}）===")
        for theme, s in sorted(active_signals.items(),
                               key=lambda x: -STRENGTH_SCORE[x[1]["signal_strength"]]):
            print(f"  [{s['signal_strength']}] {theme}")
        print()

        all_rows: list[dict] = []

        for suffix in suffixes:
            print(f"── 处理 .{suffix} 指数 ──")
            theme_map  = fetch_theme_map(conn, suffix)
            price_data = fetch_price_data(conn, suffix)
            val_data   = fetch_valuation_data(conn, suffix)

            # 仅保留关联到 active signal 题材的指数-题材对
            relevant = [m for m in theme_map if m["theme"] in active_signals]
            print(f"  关联行: {len(relevant)} / 总映射: {len(theme_map)}")

            # 按 ts_code 聚合：取最强的政策分组合
            code_best: dict[str, dict] = {}
            for m in relevant:
                sig   = active_signals[m["theme"]]
                pscore = STRENGTH_SCORE[sig["signal_strength"]] * STRENGTH_SCORE[m["relevance"]]
                key   = m["ts_code"]
                if key not in code_best or pscore > code_best[key]["policy_score"]:
                    code_best[key] = {
                        "ts_code":       m["ts_code"],
                        "index_name":    m["index_name"],
                        "suffix":        suffix,
                        "best_theme":    m["theme"],
                        "signal_str":    sig["signal_strength"],
                        "relevance":     m["relevance"],
                        "policy_score":  pscore,
                        "all_themes":    [],
                    }
                code_best[key]["all_themes"].append(
                    f"{m['theme']}[{m['relevance']}]({sig['signal_strength']})"
                )

            # 计算动量
            for code, row in code_best.items():
                row["momentum"] = compute_momentum(price_data.get(code, []))

            # 计算 PB 百分位
            for code, row in code_best.items():
                history = val_data.get(code, [])
                if history:
                    current_pb = history[-1]
                    row["current_pb"] = current_pb
                    row["pb_pct"]     = compute_pb_percentile(history, current_pb)
                else:
                    row["current_pb"] = None
                    row["pb_pct"]     = None

            all_rows.extend(code_best.values())

        # ── 2. 百分位标准化（同 suffix 内分别算，all 则混合） ─────
        mom_vals  = [r["momentum"] for r in all_rows if r["momentum"] is not None]
        pb_vals   = [r["pb_pct"]   for r in all_rows if r["pb_pct"]   is not None]

        # 建立值→百分位的映射
        def make_pct_map(vals: list[float]) -> dict:
            if not vals:
                return {}
            s = sorted(vals)
            n = len(s)
            return {v: i / max(n - 1, 1) for i, v in enumerate(s)}

        mom_pct_map = make_pct_map(mom_vals)
        pb_pct_map  = make_pct_map(pb_vals)   # pb_pct 本身已是百分位；再对它排 pct 意义不大
        # 对于估值：pb_pct 越低 = 越便宜 = 加分，所以估值得分 = 1 - pb_pct
        pb_score_map = {v: 1 - v for v in pb_vals}

        # ── 3. 综合评分 ────────────────────────────────────────────
        for row in all_rows:
            mom    = row["momentum"]
            pb_p   = row["pb_pct"]
            p_norm = row["policy_score"] / 9.0   # 归一化到 0~1

            mom_norm  = mom_pct_map.get(mom, 0.5)   if mom  is not None else 0.5
            val_score = 1 - pb_p                     if pb_p is not None else 0.5

            row["final_score"] = p_norm * 0.50 + mom_norm * 0.30 + val_score * 0.20

        # ── 4. 排序输出 ─────────────────────────────────────────────
        all_rows.sort(key=lambda r: -r["final_score"])

        print(f"\n{'='*90}")
        print(f"{'排名':<4} {'代码':<14} {'名称':<22} {'政策分':>5} {'动量':>7} {'PB当前':>7} "
              f"{'PB分位':>7} {'综合分':>6}  主力题材")
        print(f"{'='*90}")

        for rank, row in enumerate(all_rows[:args.top], 1):
            mom_str = f"{row['momentum']*100:+.1f}%" if row["momentum"] is not None else "  N/A"
            pb_str  = f"{row['current_pb']:.2f}"     if row["current_pb"] is not None else "  N/A"
            pbp_str = f"{row['pb_pct']*100:.0f}%"    if row["pb_pct"]    is not None else "  N/A"
            themes_short = " / ".join(t.split("[")[0] for t in row["all_themes"][:2])
            if len(row["all_themes"]) > 2:
                themes_short += f" +{len(row['all_themes'])-2}"
            print(
                f"{rank:<4} {row['ts_code']:<14} {row['index_name']:<22} "
                f"{row['policy_score']:>5} {mom_str:>7} {pb_str:>7} {pbp_str:>7} "
                f"{row['final_score']:>6.3f}  {themes_short}"
            )

        # ── 5. 按题材分组展示前 5 ───────────────────────────────────
        print(f"\n{'='*90}")
        print("按主力题材分组（各取前 5）：")
        by_theme: dict[str, list] = defaultdict(list)
        for row in all_rows:
            by_theme[row["best_theme"]].append(row)

        for theme, sig in sorted(active_signals.items(),
                                 key=lambda x: -STRENGTH_SCORE[x[1]["signal_strength"]]):
            rows = sorted(by_theme.get(theme, []), key=lambda r: -r["final_score"])
            sig_str = sig["signal_strength"]
            print(f"\n  [{sig_str}] {theme}")
            for i, row in enumerate(rows[:5]):
                mom_str = f"{row['momentum']*100:+.1f}%" if row["momentum"] is not None else "N/A"
                pb_str  = f"PB={row['current_pb']:.2f}" if row["current_pb"] is not None else ""
                pbp_str = f"({row['pb_pct']*100:.0f}%ile)" if row["pb_pct"] is not None else ""
                print(f"    {i+1}. {row['ts_code']:14} {row['index_name']:<22} "
                      f"动量{mom_str:>7}  {pb_str} {pbp_str}")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
