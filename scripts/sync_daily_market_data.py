#!/usr/bin/env python3
"""Run the daily market-data sync jobs used by the CSI/ETF workflow."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

from db.connection import get_connection


STEPS = [
    ("core_index_daily", [sys.executable, "scripts/import_index_daily.py"]),
    (
        "core_index_valuation",
        [sys.executable, "scripts/import_index_valuation.py", "--incremental"],
    ),
    (
        "passive_index_daily",
        [
            sys.executable,
            "scripts/import_passive_index_daily.py",
            "--update-existing",
            "--existing-basic-only",
            "--skip-stale-days",
            "21",
        ],
    ),
    ("sector_etf_daily", [sys.executable, "scripts/import_fund_daily.py"]),
]


def print_db_summary(label: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print(f"\n=== DB summary: {label} ===")
            for table in ("index_daily", "index_dailybasic", "fund_daily"):
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT ts_code), COUNT(*), MIN(trade_date), MAX(trade_date)
                    FROM {table}
                    """
                )
                n_codes, n_rows, min_date, max_date = cur.fetchone()
                print(
                    f"{table}: codes={n_codes}, rows={n_rows}, "
                    f"range={min_date} ~ {max_date}"
                )
    finally:
        conn.close()


def run_step(name: str, command: list[str]) -> int:
    print(f"\n=== Step: {name} ===")
    print("$ " + " ".join(command))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    print(f"=== Step complete: {name}, exit={completed.returncode} ===")
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync daily market data for CSI/ETF workflows")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="只验证 MySQL 并打印当前 index_daily/index_dailybasic/fund_daily 覆盖情况",
    )
    args = parser.parse_args()

    print("Verifying MySQL connection with project config...")
    print_db_summary("before")
    if args.summary_only:
        return 0

    failed: list[tuple[str, int]] = []
    for name, command in STEPS:
        rc = run_step(name, command)
        if rc != 0:
            failed.append((name, rc))
            break

    print_db_summary("after")

    if failed:
        name, rc = failed[0]
        print(f"\nDaily market-data sync failed at {name}, exit={rc}")
        return rc

    print("\nDaily market-data sync completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
