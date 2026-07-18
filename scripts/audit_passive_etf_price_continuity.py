#!/usr/bin/env python3
"""Fail closed when investable domestic passive ETF prices contain long gaps."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.domestic_equity_etf import load_equity_etf_return_universe
from db.connection import get_connection


DEFAULT_OUTPUT = ROOT / "data/backtests/passive_etf_price_continuity_audit_report.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-internal-gap-days", type=int, default=30)
    parser.add_argument("--max-latest-staleness-days", type=int, default=14)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    conn = get_connection()
    try:
        metas_by_index, series = load_equity_etf_return_universe(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(trade_date) FROM index_daily WHERE ts_code='000300.SH'"
            )
            latest_market_date = cur.fetchone()[0]
    finally:
        conn.close()
    if latest_market_date is None:
        raise RuntimeError("missing 000300.SH market calendar")

    meta_by_code = {
        meta.code: meta for values in metas_by_index.values() for meta in values
    }
    internal_gap_issues = []
    latest_staleness_issues = []
    for code, values in sorted(series.items()):
        dates = [day for day, _value in values]
        if not dates:
            latest_staleness_issues.append(
                {"ts_code": code, "last_trade_date": None, "staleness_days": None}
            )
            continue
        largest = max(
            ((current - previous).days, previous, current)
            for previous, current in zip(dates, dates[1:])
        ) if len(dates) >= 2 else (0, dates[0], dates[0])
        if largest[0] > args.max_internal_gap_days:
            internal_gap_issues.append(
                {
                    "ts_code": code,
                    "name": meta_by_code[code].name,
                    "gap_days": largest[0],
                    "gap_start": largest[1].isoformat(),
                    "gap_end": largest[2].isoformat(),
                }
            )
        staleness = (latest_market_date - dates[-1]).days
        if staleness > args.max_latest_staleness_days:
            latest_staleness_issues.append(
                {
                    "ts_code": code,
                    "name": meta_by_code[code].name,
                    "last_trade_date": dates[-1].isoformat(),
                    "staleness_days": staleness,
                }
            )

    payload = {
        "scope": "A-share-investable domestic passive equity-index ETFs",
        "latest_market_date": latest_market_date.isoformat(),
        "etf_count": len(series),
        "max_internal_gap_days": args.max_internal_gap_days,
        "max_latest_staleness_days": args.max_latest_staleness_days,
        "internal_gap_issue_count": len(internal_gap_issues),
        "latest_staleness_issue_count": len(latest_staleness_issues),
        "passed": not internal_gap_issues and not latest_staleness_issues,
        "internal_gap_issues": internal_gap_issues,
        "latest_staleness_issues": latest_staleness_issues,
    }
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"etfs={len(series)} internal_gaps={len(internal_gap_issues)} "
        f"latest_stale={len(latest_staleness_issues)} passed={payload['passed']}"
    )
    print(f"Wrote {output}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
