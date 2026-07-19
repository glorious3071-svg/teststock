#!/usr/bin/env python3
"""Run strict-quarterly rule candidates in parallel subprocesses.

The strict backtest is intentionally pure-Python and mostly single-core during
rule evaluation.  This wrapper raises local CPU utilization by running several
independent one-rule backtests against the same reusable path cache.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backtest_scorecard_csi_strict_quarterly_etf.py"


def run_rule(args: argparse.Namespace, rule: str) -> dict[str, Any]:
    prefix = args.output_dir / rule
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--rule",
        rule,
        "--selector-policy",
        args.selector_policy,
        "--direct-etf-policy",
        args.direct_etf_policy,
        "--defensive-policy",
        args.defensive_policy,
        "--path-cache-dir",
        str(args.path_cache_dir),
        "--output-prefix",
        str(prefix),
    ]
    if args.bear_signal_timing:
        cmd.extend(["--bear-signal-timing", args.bear_signal_timing])
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    csv_path = Path(f"{prefix}_search.csv")
    row: dict[str, Any] = {
        "rule": rule,
        "returncode": completed.returncode,
        "csv_path": str(csv_path.relative_to(ROOT)) if csv_path.exists() else "",
    }
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if rows:
            row.update(rows[0])
    row["stdout_tail"] = "\n".join(completed.stdout.splitlines()[-6:])
    return row


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "rule",
        "returncode",
        "name",
        "pass_count",
        "count",
        "objective_met",
        "min_final_capital_wan",
        "median_final_capital_wan",
        "worst_max_drawdown",
        "median_max_drawdown",
        "median_average_exposure",
        "csv_path",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", action="append", required=True)
    parser.add_argument("--jobs", type=int, default=max(1, min(6, (os.cpu_count() or 2) - 2)))
    parser.add_argument("--selector-policy", default="expanded_value_risk_top7_power8_cap45")
    parser.add_argument("--direct-etf-policy", default="blend_index_weighted_stable_v9_roe050_top1_regime_w49_s92")
    parser.add_argument("--defensive-policy", default="bondfine_91d_vp41_top1_min-50")
    parser.add_argument("--bear-signal-timing", default="execution")
    parser.add_argument("--path-cache-dir", type=Path, default=ROOT / "data/backtests/cache/strict_quarterly_paths")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/backtests/parallel_strict_rules")
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    args.output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    args.path_cache_dir = args.path_cache_dir if args.path_cache_dir.is_absolute() else ROOT / args.path_cache_dir
    summary_path = args.summary or args.output_dir / "summary.csv"

    rules = list(dict.fromkeys(args.rule))
    if not rules:
        raise ValueError("at least one --rule is required")

    print(f"running {len(rules)} rules with jobs={args.jobs}")
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_rule, args, rule): rule for rule in rules}
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(
                f"{row['rule']:<42} rc={row['returncode']} "
                f"min={row.get('min_final_capital_wan')} "
                f"mdd={row.get('worst_max_drawdown')}"
            )
    rows.sort(
        key=lambda row: (
            float(row.get("min_final_capital_wan") or "-inf"),
            float(row.get("worst_max_drawdown") or "-inf"),
        ),
        reverse=True,
    )
    write_summary(summary_path, rows)
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
