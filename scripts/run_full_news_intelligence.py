#!/usr/bin/env python3
"""Full news intelligence pipeline — implements master plan end-to-end.

Usage:
  python scripts/run_full_news_intelligence.py
  python scripts/run_full_news_intelligence.py --quick   # skip FULLTEXT index build
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG = ROOT / "data" / "logs" / "full-intelligence.log"


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_py(script: str, *args: str) -> int:
    cmd = [sys.executable, script, *args]
    log(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Skip FULLTEXT index creation")
    parser.add_argument("--year", type=int, default=2026)
    args = parser.parse_args()

    log("=== Full news intelligence pipeline START ===")

    from db.connection import get_connection
    from news.processing.batch import (
        backfill_cluster_by_day,
        backfill_daily_signals,
        backfill_weekly_signals,
        ensure_all_schema,
        extract_pending_events,
        link_existing_extractions,
    )
    from news.retrieval.prefilter import backfill_prefilter, ensure_fulltext_index, seed_theme_keywords

    conn = get_connection()
    ensure_all_schema(conn)

    log("B1 seed theme_keywords")
    n_kw = seed_theme_keywords(conn)
    log(f"  keywords={n_kw}")

    log("B4 prefilter backfill")
    pf = backfill_prefilter(conn)
    log(f"  {pf}")

    if not args.quick:
        log("B2 FULLTEXT index")
        ok_ft = ensure_fulltext_index(conn)
        log(f"  fulltext={'ok' if ok_ft else 'skipped'}")

    log("C2 cluster all remaining")
    cstats = backfill_cluster_by_day(conn)
    log(f"  {cstats}")

    log("C5 link + extract events")
    link_existing_extractions(conn)
    total_ext = {"extracted": 0, "skipped_prefilter": 0}
    while True:
        batch = extract_pending_events(conn, limit=2000, mock=True, use_prefilter=True)
        total_ext["extracted"] += batch["extracted"]
        total_ext["skipped_prefilter"] += batch["skipped_prefilter"]
        log(f"  batch {batch}")
        if batch["extracted"] == 0:
            break

    with conn.cursor() as cur:
        cur.execute("SELECT MIN(DATE(COALESCE(pub_time,created_at))), MAX(DATE(COALESCE(pub_time,created_at))) FROM news_article")
        d0, d1 = cur.fetchone()
    if d0 and d1:
        log(f"C6 daily signals {d0}..{d1}")
        nd = backfill_daily_signals(conn, start_date=d0, end_date=d1)
        log(f"  days_with_signals={nd}")
        log("C7 weekly rollup")
        nw = backfill_weekly_signals(conn, start_date=d0, end_date=d1)
        log(f"  weeks={nw}")

    conn.close()

    log("C9 aggregate theme_news_signals")
    run_py("scripts/aggregate_theme_news_signals.py", "--year", str(args.year), "--live")

    log("D rank CSI full mode")
    run_py("scripts/rank_annual_csi.py", "--year", str(args.year), "--top", "30", "--full", "--save")

    log("D5 ablation")
    run_py("scripts/validate_csi_ablation.py", "--year", str(args.year), "--live")

    log("D7 ETF mapping")
    run_py("scripts/map_csi_to_etf.py", "--year", str(args.year))

    log("E1 unit tests")
    run_py("scripts/test_news_processing_unit.py")
    run_py("scripts/test_news_retrieval_unit.py")

    log("E2 verify processing")
    rc = run_py("scripts/verify_news_processing.py")

    log("=== COMPLETE ===" if rc == 0 else "=== COMPLETE WITH WARNINGS ===")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
