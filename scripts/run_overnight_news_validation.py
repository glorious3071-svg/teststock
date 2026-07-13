#!/usr/bin/env python3
"""Overnight validation: backfill → ablation → aggregate → rank → verify.

Usage:
  python scripts/run_overnight_news_validation.py
  python scripts/run_overnight_news_validation.py --skip-backfill
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "logs" / "overnight-validation.log"


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_step(label: str, cmd: list[str], *, required: bool = True) -> int:
    log(f"START {label}")
    log(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.stdout:
        for line in r.stdout.strip().splitlines()[-20:]:
            log(f"  | {line}")
    if r.stderr:
        for line in r.stderr.strip().splitlines()[-5:]:
            log(f"  ! {line}")
    if r.returncode != 0:
        log(f"FAIL {label} exit={r.returncode}")
        if required:
            return r.returncode
    else:
        log(f"OK   {label}")
    return r.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-backfill", action="store_true")
    parser.add_argument("--year", type=int, default=2026)
    args = parser.parse_args()

    py = sys.executable
    log("=== Overnight news validation ===")

    steps: list[tuple[str, list[str], bool]] = [
        ("unit_tests", [py, "scripts/test_news_processing_unit.py"], True),
        ("unit_pipeline", [py, "scripts/test_news_pipeline_unit.py"], True),
    ]
    if not args.skip_backfill:
        steps.append(("backfill", [py, "scripts/run_news_daily_processing.py", "--backfill"], True))
    steps.extend([
        ("ablation", [py, "scripts/backtest_news_salience.py", "--year", str(args.year), "--live"], False),
        ("aggregate", [py, "scripts/aggregate_theme_news_signals.py", "--year", str(args.year), "--live"], True),
        ("rank_csi", [py, "scripts/rank_annual_csi.py", "--year", str(args.year), "--top", "30", "--save"], True),
        ("validate_csi", [py, "scripts/validate_csi_rank.py", "--year", str(args.year)], False),
        ("verify_processing", [py, "scripts/verify_news_processing.py"], False),
    ])

    for label, cmd, required in steps:
        rc = run_step(label, cmd, required=required)
        if rc != 0 and required:
            log("=== ABORTED ===")
            return rc

    log("=== COMPLETE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
