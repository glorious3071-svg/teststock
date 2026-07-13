#!/usr/bin/env python3
"""CSI ablation: baseline / +news / +salience / full → JSON report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "backtests" / "csi_news_ablation.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    py = sys.executable
    report: dict = {"year": args.year, "live": args.live, "runs": {}}

    for mode in ("baseline", "news", "salience", "full"):
        cmd = [py, "scripts/rank_annual_csi.py", "--year", str(args.year), "--top", "20"]
        if mode == "baseline":
            cmd.append("--no-news")
        elif mode == "salience":
            cmd.extend(["--live" if args.live else ""])
        elif mode == "full":
            cmd.extend(["--full", "--save"])
        cmd = [c for c in cmd if c]
        subprocess.call(cmd, cwd=ROOT)

    from db.connection import get_connection
    from scripts.backtest_news_salience import main as salience_main

    conn = get_connection()
    with conn.cursor() as cur:
        for label, sql in [
            ("theme_daily", "SELECT COUNT(*) FROM theme_news_daily"),
            ("theme_weekly", "SELECT COUNT(*) FROM theme_news_weekly"),
            ("events", "SELECT COUNT(*) FROM news_event"),
            ("extractions_linked", "SELECT COUNT(*) FROM news_extraction WHERE event_id IS NOT NULL"),
        ]:
            cur.execute(sql)
            report[label] = cur.fetchone()[0]
        cur.execute(
            """
            SELECT rank_position, ts_code, index_name, final_score, best_theme
            FROM csi_annual_recommendation WHERE apply_year=%s
            ORDER BY rank_position LIMIT 10
            """,
            (args.year,),
        )
        report["top10"] = [
            {"rank": r[0], "ts_code": r[1], "name": r[2], "score": float(r[3]), "theme": r[4]}
            for r in cur.fetchall()
        ]
    conn.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
