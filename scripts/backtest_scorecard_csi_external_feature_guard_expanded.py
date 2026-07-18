#!/usr/bin/env python3
"""Focused expanded external-feature loss guard search.

The full external-feature guard grid is expensive after adding the extended ETF
feature set.  This focused run preserves the highest-signal checks: ordinary
loss and 2% loss labels across external/risk-market feature groups, using
mid-to-high risk-score quantiles and the same all-phase validation matrix.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backtest_scorecard_csi_external_feature_guard import (  # noqa: E402
    EXTERNAL_SYMBOLS,
    ExternalFeatureRule,
    evaluate_rule,
    feature_coverage,
    label_balance,
    load_rows,
)
from scripts.backtest_scorecard_csi_midyear_risk import TARGET_CAPITAL  # noqa: E402
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD  # noqa: E402

OUT_DIR = ROOT / "data" / "backtests"
OUT_JSON = OUT_DIR / "scorecard_csi_external_feature_guard_expanded_report.json"
OUT_CSV = OUT_DIR / "scorecard_csi_external_feature_guard_expanded_search.csv"


def build_rules() -> list[ExternalFeatureRule]:
    rules: list[ExternalFeatureRule] = []
    for group in ["external", "risk_market"]:
        for label_threshold in [-0.01, -0.02]:
            suffix = f"loss{abs(label_threshold) * 100:.1f}".replace(".", "p")
            for flag_quantile in [0.50, 0.60, 0.70, 0.80]:
                q_name = int(round(flag_quantile * 100))
                for cap_pct in [0.0, 20.0, 40.0, 60.0]:
                    rules.append(
                        ExternalFeatureRule(
                            f"xext_{suffix}_{group}_q{q_name}_cap{int(cap_pct)}",
                            label_threshold,
                            group,
                            cap_pct,
                            flag_quantile,
                            min_loss_count=80,
                            max_features=20,
                        )
                    )
    return rules


RULES = build_rules()


def write_outputs(rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": "Focused expanded external ETF feature walk-forward loss guards on scorecard+CSI phase rows.",
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "external_symbols": EXTERNAL_SYMBOLS,
        "rule_count": len(RULES),
        "label_balance": label_balance(rows),
        "feature_coverage": feature_coverage(rows),
        "model_limits": "Linear loss-vs-ok standardized score trained only on prior-year snapshots; focused grid over expanded external ETF/risk-market features.",
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "feature_group",
            "label_threshold",
            "cap_pct",
            "flag_quantile",
            "max_features",
            "cooldown_months",
            "pass_count",
            "count",
            "min_final_capital_wan",
            "median_final_capital_wan",
            "worst_max_drawdown",
            "median_max_drawdown",
            "min_annualized_return",
            "median_guard_count",
            "median_loss_recall",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> int:
    rows = load_rows()
    balances = label_balance(rows)
    print(
        "label_balance "
        + " ".join(f"{item['label_threshold']}:{item['count']}/{len(rows)}({item['pct']:.1%})" for item in balances)
    )
    coverage = feature_coverage(rows)
    sparse = sorted(coverage.items(), key=lambda item: item[1])[:5]
    print("lowest_feature_coverage " + " ".join(f"{name}:{pct:.1%}" for name, pct in sparse))

    results = []
    for rule in RULES:
        result = evaluate_rule(rows, rule)
        results.append(result)
        summary = result["summary"]
        model_summary = result["model_summary"]
        print(
            f"{rule.name:<46} pass={summary['pass_count']:>2}/{summary['count']} "
            f"min={summary['min_final_capital_wan']:8.1f}万 "
            f"median={summary['median_final_capital_wan']:8.1f}万 "
            f"worst_mdd={summary['worst_max_drawdown'] * 100:6.1f}% "
            f"guards={summary['median_guard_count']:5.1f} "
            f"recall={summary['median_loss_recall'] * 100:5.1f}% "
            f"trained={model_summary['trained_snapshot_count']}"
        )
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["min_final_capital_wan"],
            item["summary"]["worst_max_drawdown"],
        ),
        reverse=True,
    )
    write_outputs(rows, results)
    best = results[0]["summary"]
    print(
        f"Wrote {OUT_JSON}; rules={len(RULES)} "
        f"best_min={best['min_final_capital_wan']:.1f}万 "
        f"best_worst_mdd={best['worst_max_drawdown']:.1%}"
    )
    print(f"Wrote {OUT_CSV}")
    return 0 if results and results[0]["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
