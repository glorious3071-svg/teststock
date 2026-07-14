#!/usr/bin/env python3
"""Run the standardized CSI research-selection data pipeline.

Pipeline steps:
1. Optionally sync market/index/ETF daily data through the existing market sync.
2. Refresh East Money H2 industry research metadata used as ex-ante features.
3. Run the regime-aware CSI research backtest and validate the output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

from db.connection import get_connection
from scripts.import_eastmoney_industry_reports import import_range

REPORT_JSON = ROOT / "data" / "ml" / "regime_research_csi_strategy_report.json"


def parse_year_range(raw: str) -> tuple[int, int]:
    if ":" in raw:
        start, end = raw.split(":", 1)
    elif "-" in raw:
        start, end = raw.split("-", 1)
    else:
        year = int(raw)
        return year, year
    return int(start), int(end)


def default_research_h2_years(today: date) -> tuple[int, int]:
    # Backtest coverage needs 2020H2-2024H2.  Ongoing runs also keep the latest
    # completed/current H2 in sync for the next annual selection cycle.
    latest = today.year if today.month >= 7 else today.year - 1
    return 2020, max(2024, latest)


def h2_window(year: int, today: date) -> tuple[str, str]:
    start = date(year, 7, 1)
    nominal_end = date(year, 12, 31)
    end = min(nominal_end, today)
    return start.isoformat(), end.isoformat()


def run_command(name: str, command: list[str]) -> None:
    print(f"\n=== Step: {name} ===")
    print("$ " + " ".join(command))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    print(f"=== Step complete: {name}, exit={completed.returncode} ===")
    if completed.returncode != 0:
        raise RuntimeError(f"{name} failed with exit={completed.returncode}")


def print_db_summary() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print("\n=== DB coverage summary ===")
            for table, date_col, code_col in [
                ("index_daily", "trade_date", "ts_code"),
                ("index_dailybasic", "trade_date", "ts_code"),
                ("fund_daily", "trade_date", "ts_code"),
                ("index_constituent", "trade_date", "index_code"),
            ]:
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT {code_col}), COUNT(*), MIN({date_col}), MAX({date_col})
                    FROM {table}
                    """
                )
                n_codes, n_rows, min_date, max_date = cur.fetchone()
                print(f"{table}: codes={n_codes}, rows={n_rows}, range={min_date} ~ {max_date}")

            cur.execute(
                """
                SELECT YEAR(report_date), COUNT(*), COUNT(DISTINCT industry), COUNT(DISTINCT org_name)
                FROM broker_research_report
                WHERE source='eastmoney_api' AND report_type='industry'
                  AND MONTH(report_date) BETWEEN 7 AND 12
                GROUP BY YEAR(report_date)
                ORDER BY YEAR(report_date)
                """
            )
            print("eastmoney industry reports H2:")
            for y, reports, industries, orgs in cur.fetchall():
                print(f"  {y}: reports={reports}, industries={industries}, orgs={orgs}")
    finally:
        conn.close()


def refresh_research_reports(start_year: int, end_year: int, today: date, page_size: int, max_pages: int, sleep: float) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    for year in range(start_year, end_year + 1):
        start, end = h2_window(year, today)
        if start > end:
            continue
        print(f"\n=== Step: eastmoney_industry_reports_{year}H2 ===")
        result = import_range(start, end, page_size=page_size, max_pages=max_pages, sleep=sleep)
        item = {"h2_year": year, "from": start, "to": end, **result}
        stats.append(item)
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return stats


def validate_report(min_hits: int, min_worst_return: float) -> dict[str, Any]:
    payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    summary = payload["summary"]
    hits = int(summary["total_winner_hit"])
    worst = float(summary["worst_strategy_return"])
    if hits < min_hits:
        raise RuntimeError(f"winner-hit QA failed: hits={hits} < min_hits={min_hits}")
    if worst < min_worst_return:
        raise RuntimeError(f"worst-return QA failed: worst={worst:.4f} < min_worst_return={min_worst_return:.4f}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CSI research-selection data sync, feature build, and backtest QA")
    parser.add_argument("--skip-market-sync", action="store_true", help="Skip scripts/sync_daily_market_data.py")
    parser.add_argument("--summary-only", action="store_true", help="Only verify DB coverage and exit")
    parser.add_argument("--research-h2-years", help="H2 research years to refresh, e.g. 2020:2026. Default: 2020 through current H2.")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=300)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--min-winner-hits", type=int, default=8)
    parser.add_argument("--min-worst-return", type=float, default=-0.08)
    args = parser.parse_args()

    today = date.today()
    print("Verifying MySQL connection with project config...")
    print_db_summary()
    if args.summary_only:
        return 0

    if not args.skip_market_sync:
        run_command("daily_market_data_sync", [sys.executable, "scripts/sync_daily_market_data.py"])

    start_year, end_year = parse_year_range(args.research_h2_years) if args.research_h2_years else default_research_h2_years(today)
    research_stats = refresh_research_reports(start_year, end_year, today, args.page_size, args.max_pages, args.sleep)

    run_command("regime_research_csi_backtest", [sys.executable, "scripts/backtest_regime_research_csi_strategy.py"])
    summary = validate_report(args.min_winner_hits, args.min_worst_return)

    print("\n=== CSI research pipeline complete ===")
    print(
        "summary: "
        f"mean_strategy_return={float(summary['mean_strategy_return']) * 100:.1f}% "
        f"mean_excess_return={float(summary['mean_excess_return']) * 100:.1f}% "
        f"worst_strategy_return={float(summary['worst_strategy_return']) * 100:.1f}% "
        f"total_winner_hit={summary['total_winner_hit']}"
    )
    print("research_imports=" + json.dumps(research_stats, ensure_ascii=False, sort_keys=True))
    print(f"report={REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
