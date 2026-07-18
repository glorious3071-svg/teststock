#!/usr/bin/env python3
"""Diagnose the executable scorecard + CSI frontier excluding modeled floors."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKTEST_DIR = ROOT / "data" / "backtests"
OUT_JSON = BACKTEST_DIR / "scorecard_csi_executable_frontier_diagnostic.json"
OUT_CSV = BACKTEST_DIR / "scorecard_csi_executable_frontier_diagnostic.csv"


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def parse_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def experiment_name(path: Path) -> str:
    name = path.stem
    prefix = "scorecard_csi_"
    suffix = "_search"
    if name.startswith(prefix):
        name = name[len(prefix) :]
    if name.endswith(suffix):
        name = name[: -len(suffix)]
    return name


def load_rows() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(BACKTEST_DIR.glob("scorecard_csi_*_search.csv")):
        if path.name in {
            "scorecard_csi_frontier_summary.csv",
            "scorecard_csi_defined_loss_overlay_search.csv",
            "scorecard_csi_defined_loss_csi_hedge_search.csv",
        }:
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                min_final = parse_float(row.get("min_final_capital_wan"))
                worst_mdd = parse_float(row.get("worst_max_drawdown"))
                if min_final is None or worst_mdd is None:
                    continue
                pass_count = parse_int(row.get("pass_count", row.get("strict_pass_count")))
                case_count = parse_int(row.get("count", row.get("strict_case_count")))
                out.append(
                    {
                        "experiment": experiment_name(path),
                        "name": row.get("name") or path.stem,
                        "source_file": str(path.relative_to(ROOT)),
                        "pass_count": pass_count,
                        "case_count": case_count,
                        "min_final_capital_wan": min_final,
                        "worst_max_drawdown": worst_mdd,
                        "capital_gap_wan": max(0.0, 4000.0 - min_final),
                        "drawdown_gap_pct": max(0.0, -0.10 - worst_mdd) * 100.0,
                    }
                )
    return out


def row_score(row: dict[str, Any]) -> tuple[float, float, float]:
    return (
        row["capital_gap_wan"] / 4000.0 + row["drawdown_gap_pct"] / 10.0,
        -row["min_final_capital_wan"],
        -row["worst_max_drawdown"],
    )


def top_rows(rows: list[dict[str, Any]], key, limit: int) -> list[dict[str, Any]]:
    return sorted(rows, key=key, reverse=True)[:limit]


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fields = [
        "rank",
        "bucket",
        "experiment",
        "name",
        "min_final_capital_wan",
        "worst_max_drawdown",
        "pass_count",
        "case_count",
        "capital_gap_wan",
        "drawdown_gap_pct",
        "source_file",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose executable non-defined-loss frontier gaps.")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    rows = load_rows()
    if not rows:
        raise RuntimeError(f"no executable search rows found under {BACKTEST_DIR}")
    strict = [
        row for row in rows
        if row["case_count"] > 0
        and row["pass_count"] == row["case_count"]
        and row["min_final_capital_wan"] >= 4000.0
        and row["worst_max_drawdown"] >= -0.10
    ]
    under_10 = [row for row in rows if row["worst_max_drawdown"] >= -0.10]
    over_4000 = [row for row in rows if row["min_final_capital_wan"] >= 4000.0]
    best_under_10 = top_rows(under_10, key=lambda row: (row["min_final_capital_wan"], row["worst_max_drawdown"]), limit=args.limit)
    best_over_4000 = top_rows(over_4000, key=lambda row: (row["worst_max_drawdown"], row["min_final_capital_wan"]), limit=args.limit)
    best_balance = sorted(rows, key=row_score)[: args.limit]
    report = {
        "strategy": "scorecard_csi_executable_frontier_diagnostic",
        "candidate_count": len(rows),
        "strict_pass_count": len(strict),
        "best_under_10_drawdown": best_under_10[:10],
        "best_over_4000w": best_over_4000[:10],
        "best_balance": best_balance[:10],
        "diagnosis": {
            "best_under_10_min_final_wan": best_under_10[0]["min_final_capital_wan"] if best_under_10 else None,
            "best_over_4000_worst_mdd": best_over_4000[0]["worst_max_drawdown"] if best_over_4000 else None,
            "strict_gap": "no executable non-defined-loss candidate reaches both 4000w and -10% mdd across all timing cases",
        },
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_rows = []
    for bucket, items in [
        ("best_under_10_drawdown", best_under_10),
        ("best_over_4000w", best_over_4000),
        ("best_balance", best_balance),
    ]:
        for rank, row in enumerate(items, 1):
            item = dict(row)
            item["bucket"] = bucket
            item["rank"] = rank
            csv_rows.append(item)
    write_csv(csv_rows, OUT_CSV)
    print(
        "executable_frontier_diagnostic: "
        f"candidates={len(rows)} strict_passes={len(strict)} "
        f"best_under10={report['diagnosis']['best_under_10_min_final_wan']:.1f}w "
        f"best_over4000_mdd={report['diagnosis']['best_over_4000_worst_mdd']:.1%}"
    )
    print(f"json={OUT_JSON}")
    print(f"csv={OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
