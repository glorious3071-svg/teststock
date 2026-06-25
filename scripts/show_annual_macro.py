#!/usr/bin/env python3
"""Print annual macro rate snapshot for 年初定方向."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from macro.annual_snapshot import annual_macro_brief, rebuild_annual_snapshots


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


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Show annual macro snapshot")
    parser.add_argument("year", type=int, nargs="?", help="apply_year (default: all)")
    parser.add_argument("--rebuild", action="store_true", help="rebuild snapshots from rate tables")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        if args.rebuild:
            n = rebuild_annual_snapshots(conn)
            print(f"Rebuilt {n} annual snapshots\n")

        if args.year:
            print(annual_macro_brief(conn, args.year))
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT apply_year FROM macro_annual_snapshot ORDER BY apply_year")
                years = [r[0] for r in cur.fetchall()]
            for y in years:
                print(annual_macro_brief(conn, y))
                print()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
