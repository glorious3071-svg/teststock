#!/usr/bin/env python3
"""Multi-year batch backtest for annual direction framework.

流程:
  1. 对每个年份 (start_year ~ end_year) 以回测模式运行 annual direction agent
  2. 从 fund_daily 取 LLM 选中的 ETF 当年实际收益
  3. 与 CSI300 全年收益比较，输出汇总表格

用法:
  python scripts/run_backtest_multi_year.py               # 默认 2015-2025
  python scripts/run_backtest_multi_year.py --start 2013 --end 2024
  python scripts/run_backtest_multi_year.py --no-llm       # 仅查已有会话
  python scripts/run_backtest_multi_year.py --year 2022   # 单年
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from agents.annual_direction.agent import load_session, save_session, start_session
from agents.annual_direction.mode import resolve_mode

DEFAULT_START = 2015
DEFAULT_END = 2025
SESSION_DIR = ROOT / "data" / "annual_direction_sessions"
OUT_DIR = ROOT / "data" / "backtests"
CSI300_CODE = "000300.SH"


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def _first_close(cur, ts_code: str, year: int, table: str, date_col: str, close_col: str) -> float | None:
    cur.execute(
        f"SELECT {close_col} FROM {table} WHERE {date_col} >= %s AND {date_col} < %s "
        f"AND {close_col} IS NOT NULL ORDER BY {date_col} ASC LIMIT 1",
        (f"{year}-01-01", f"{year}-02-01"),
    )
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _last_close(cur, ts_code: str, year: int, table: str, date_col: str, close_col: str) -> float | None:
    cur.execute(
        f"SELECT {close_col} FROM {table} WHERE {date_col} BETWEEN %s AND %s "
        f"AND {close_col} IS NOT NULL ORDER BY {date_col} DESC LIMIT 1",
        (f"{year}-01-01", f"{year}-12-31"),
    )
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def etf_annual_return(cur, ts_code: str, year: int) -> float | None:
    """ETF 当年全年实际收益（fund_daily → 若无数据则用 tracking index from index_daily）."""
    cur.execute(
        "SELECT close FROM fund_daily WHERE ts_code = %s AND trade_date >= %s AND trade_date < %s "
        "AND close IS NOT NULL ORDER BY trade_date ASC LIMIT 1",
        (ts_code, f"{year}-01-01", f"{year}-02-01"),
    )
    row = cur.fetchone()
    first = float(row[0]) if row else None

    cur.execute(
        "SELECT close FROM fund_daily WHERE ts_code = %s AND trade_date BETWEEN %s AND %s "
        "AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
        (ts_code, f"{year}-01-01", f"{year}-12-31"),
    )
    row = cur.fetchone()
    last = float(row[0]) if row else None

    if first and last and first > 0:
        return round(last / first - 1, 6)

    # Fallback: use tracking index from passive_etf → index_daily
    cur.execute("SELECT index_ts_code FROM passive_etf WHERE ts_code = %s", (ts_code,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    idx_code = row[0]
    return index_annual_return(cur, idx_code, year)


def index_annual_return(cur, ts_code: str, year: int) -> float | None:
    """指数当年全年实际收益（from index_daily）."""
    cur.execute(
        "SELECT close FROM index_daily WHERE ts_code = %s AND trade_date >= %s AND trade_date < %s "
        "AND close IS NOT NULL ORDER BY trade_date ASC LIMIT 1",
        (ts_code, f"{year}-01-01", f"{year}-02-01"),
    )
    row = cur.fetchone()
    first = float(row[0]) if row else None

    cur.execute(
        "SELECT close FROM index_daily WHERE ts_code = %s AND trade_date BETWEEN %s AND %s "
        "AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
        (ts_code, f"{year}-01-01", f"{year}-12-31"),
    )
    row = cur.fetchone()
    last = float(row[0]) if row else None

    if first and last and first > 0:
        return round(last / first - 1, 6)
    return None


def compute_portfolio_return(cur, etf_allocs: list[dict], year: int, equity_weight_pct: float) -> float | None:
    """加权 ETF 组合收益（仅权益部分，最终乘以权益仓位占比）."""
    total_weight = sum(a.get("weight_pct", 0) for a in etf_allocs)
    if total_weight <= 0:
        return None

    weighted_sum = 0.0
    covered_weight = 0.0
    for a in etf_allocs:
        ts_code = a.get("ts_code", "")
        w = a.get("weight_pct", 0)
        ret = etf_annual_return(cur, ts_code, year)
        if ret is not None:
            weighted_sum += ret * w
            covered_weight += w

    if covered_weight <= 0:
        return None

    equity_ret = weighted_sum / covered_weight
    # 组合收益 = 权益部分 × 股权占比 + 现金 × 0 (货币基金收益忽略不计)
    return round(equity_ret * (equity_weight_pct / 100.0), 6)


def run_or_load_session(year: int, *, use_llm: bool, sleep_between: float = 2.0):
    """Load cached session or run agent for a year."""
    existing = load_session(year, mode="backtest")
    if existing and existing.last_allocation and existing.last_allocation.get("etf_allocations"):
        print(f"  [cache] {year}: loaded existing backtest session")
        return existing

    if not use_llm:
        print(f"  [skip] {year}: no cached session and --no-llm specified")
        return None

    print(f"  [llm] {year}: running agent (mode=backtest, no-web)...")
    session = start_session(year, enable_web=False, use_llm=True, mode="backtest")
    if sleep_between > 0:
        time.sleep(sleep_between)
    return session


def backtest_year(cur, session, year: int) -> dict:
    alloc = session.last_allocation or {}
    etf_allocs = alloc.get("etf_allocations") or []
    equity_pct = alloc.get("equity_weight_pct") or 80

    portfolio_ret = compute_portfolio_return(cur, etf_allocs, year, equity_pct)
    benchmark_ret = index_annual_return(cur, CSI300_CODE, year)

    etf_summary = [
        {
            "ts_code": a.get("ts_code"),
            "name": a.get("name"),
            "theme": a.get("theme"),
            "weight_pct": a.get("weight_pct"),
            "actual_return": etf_annual_return(cur, a.get("ts_code", ""), year),
        }
        for a in etf_allocs
    ]

    excess = None
    if portfolio_ret is not None and benchmark_ret is not None:
        excess = round(portfolio_ret - benchmark_ret, 6)

    return {
        "year": year,
        "equity_weight_pct": equity_pct,
        "etf_count": len(etf_allocs),
        "portfolio_return": portfolio_ret,
        "csi300_return": benchmark_ret,
        "excess_return": excess,
        "finalized": session.finalized,
        "etf_detail": etf_summary,
    }


def print_results(results: list[dict]) -> None:
    print("\n" + "=" * 90)
    print(f"{'年份':^6} {'权益%':^6} {'ETF数':^5} {'组合收益':^10} {'沪深300':^10} {'超额':^10} {'定稿':^5}")
    print("-" * 90)
    portfolio_rets = []
    benchmark_rets = []
    excess_rets = []
    for r in results:
        pret = f"{r['portfolio_return']:.1%}" if r["portfolio_return"] is not None else "N/A"
        bret = f"{r['csi300_return']:.1%}" if r["csi300_return"] is not None else "N/A"
        exc = f"{r['excess_return']:+.1%}" if r["excess_return"] is not None else "N/A"
        fin = "✓" if r["finalized"] else "草稿"
        print(f"  {r['year']:^4}   {r['equity_weight_pct']:^6} {r['etf_count']:^5} {pret:^10} {bret:^10} {exc:^10} {fin:^5}")
        if r["portfolio_return"] is not None:
            portfolio_rets.append(r["portfolio_return"])
        if r["csi300_return"] is not None:
            benchmark_rets.append(r["csi300_return"])
        if r["excess_return"] is not None:
            excess_rets.append(r["excess_return"])
    print("-" * 90)
    if portfolio_rets:
        avg_p = sum(portfolio_rets) / len(portfolio_rets)
        avg_b = sum(benchmark_rets) / len(benchmark_rets) if benchmark_rets else None
        avg_e = sum(excess_rets) / len(excess_rets) if excess_rets else None
        wins = sum(1 for e in excess_rets if e > 0)
        win_rate = wins / len(excess_rets) if excess_rets else None
        print(f"  {'均值':^4}   {'—':^6} {'—':^5} {avg_p:.1%}    {avg_b:.1%}   {avg_e:+.1%}   胜率{win_rate:.0%}" if avg_b and avg_e and win_rate else "")
    print("=" * 90)


def main() -> int:
    parser = argparse.ArgumentParser(description="多年回测：年初定方向 Agent 策略表现")
    parser.add_argument("--start", type=int, default=DEFAULT_START)
    parser.add_argument("--end", type=int, default=DEFAULT_END)
    parser.add_argument("--year", type=int, help="仅跑单年（覆盖 --start/--end）")
    parser.add_argument("--no-llm", action="store_true", help="不调用 LLM，仅读缓存")
    parser.add_argument("--sleep", type=float, default=2.0, help="LLM 调用之间的间隔秒数")
    args = parser.parse_args()

    if args.year:
        years = [args.year]
    else:
        years = list(range(args.start, args.end + 1))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    conn = pymysql.connect(**mysql_config())

    try:
        results: list[dict] = []
        for year in years:
            print(f"\n{'='*40}")
            print(f"年份 {year}")
            session = run_or_load_session(year, use_llm=not args.no_llm, sleep_between=args.sleep)
            if session is None:
                continue
            if not session.last_allocation:
                print(f"  [warn] {year}: no allocation in session, skip")
                continue

            with conn.cursor() as cur:
                result = backtest_year(cur, session, year)
            results.append(result)

            # Print ETF details for this year
            print(f"  权益仓位: {result['equity_weight_pct']}%")
            for e in result["etf_detail"]:
                ret_str = f"{e['actual_return']:.1%}" if e["actual_return"] is not None else "N/A"
                print(f"  {e['ts_code']} {e.get('name','')} ({e.get('theme','')}) {e.get('weight_pct')}% → 实际{ret_str}")
            pret = f"{result['portfolio_return']:.1%}" if result["portfolio_return"] is not None else "N/A"
            bret = f"{result['csi300_return']:.1%}" if result["csi300_return"] is not None else "N/A"
            exc = f"{result['excess_return']:+.1%}" if result["excess_return"] is not None else "N/A"
            print(f"  组合: {pret} | 沪深300: {bret} | 超额: {exc}")

        if results:
            print_results(results)
            out_path = OUT_DIR / "multi_year_backtest.json"
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"\n汇总已保存: {out_path}")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
