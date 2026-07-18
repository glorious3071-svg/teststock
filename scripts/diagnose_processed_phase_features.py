#!/usr/bin/env python3
"""Diagnose processed market features against phase-shifted return direction."""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_scorecard_csi_generalization import (  # noqa: E402
    FORMAL_SCHEDULES,
    LEGACY_ALLOCATION_POLICY,
    MONTH_DRIFT_PHASES,
    run_phase_schedule,
)

OUT_DIR = ROOT / "data" / "backtests" / "processed_phase_features"
OUT_JSON = OUT_DIR / "report.json"
OUT_CSV = OUT_DIR / "features.csv"
EXECUTION_LAG_DAYS = 3
MIN_TRAIN_OBSERVATIONS = 80


def safe_spearman(rows: list[dict[str, Any]], feature: str) -> float | None:
    usable = [row for row in rows if row.get(feature) is not None]
    if len(usable) < 5:
        return None
    x = pd.Series([float(row[feature]) for row in usable]).rank(method="average")
    y = pd.Series([float(row["forward_return"]) for row in usable]).rank(method="average")
    value = x.corr(y)
    return None if pd.isna(value) else float(value)


def score_predictions(predictions: list[tuple[int, float]]) -> dict[str, Any]:
    usable = [(prediction, outcome) for prediction, outcome in predictions if prediction and outcome]
    if not usable:
        return {
            "count": 0,
            "hit_rate": None,
            "return_weighted_hit_rate": None,
            "mean_signed_return": None,
        }
    signed = [prediction * outcome for prediction, outcome in usable]
    magnitude = sum(abs(outcome) for _prediction, outcome in usable)
    return {
        "count": len(usable),
        "hit_rate": sum(value > 0 for value in signed) / len(signed),
        "return_weighted_hit_rate": (
            sum(abs(outcome) for value, (_prediction, outcome) in zip(signed, usable) if value > 0)
            / magnitude
            if magnitude
            else None
        ),
        "mean_signed_return": mean(signed),
    }


def predictions_from_train(
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
    feature: str,
) -> list[tuple[int, float]]:
    usable_train = [row for row in train if row.get(feature) is not None]
    if len(usable_train) < MIN_TRAIN_OBSERVATIONS:
        return []
    correlation = safe_spearman(usable_train, feature)
    if correlation is None or abs(correlation) < 0.03:
        return []
    center = median(float(row[feature]) for row in usable_train)
    orientation = 1 if correlation > 0 else -1
    predictions = []
    for row in test:
        value = row.get(feature)
        if value is None or float(value) == center:
            continue
        direction = orientation * (1 if float(value) > center else -1)
        predictions.append((direction, float(row["forward_return"])))
    return predictions


def leave_phase_out(rows: list[dict[str, Any]], feature: str) -> dict[str, Any]:
    predictions = []
    by_phase = {}
    for phase in MONTH_DRIFT_PHASES:
        train = [row for row in rows if row["phase"] != phase]
        test = [row for row in rows if row["phase"] == phase]
        phase_predictions = predictions_from_train(train, test, feature)
        predictions.extend(phase_predictions)
        by_phase[str(phase)] = score_predictions(phase_predictions)
    summary = score_predictions(predictions)
    phase_weighted = [
        result["return_weighted_hit_rate"]
        for result in by_phase.values()
        if result["return_weighted_hit_rate"] is not None
    ]
    return summary | {
        "by_phase": by_phase,
        "phase_count_above_50pct": sum(value > 0.5 for value in phase_weighted),
        "worst_phase_weighted_hit_rate": min(phase_weighted) if phase_weighted else None,
    }


def expanding_year(rows: list[dict[str, Any]], feature: str) -> dict[str, Any]:
    predictions = []
    years = sorted({row["snapshot_year"] for row in rows})
    by_year = {}
    for year in years:
        train = [row for row in rows if row["snapshot_year"] < year]
        test = [row for row in rows if row["snapshot_year"] == year]
        year_predictions = predictions_from_train(train, test, feature)
        if year_predictions:
            by_year[str(year)] = score_predictions(year_predictions)
            predictions.extend(year_predictions)
    return score_predictions(predictions) | {"by_year": by_year}


def build_observations(spec) -> list[dict[str, Any]]:
    rows = []
    for phase in MONTH_DRIFT_PHASES:
        result = run_phase_schedule(
            spec,
            phase,
            EXECUTION_LAG_DAYS,
            include_rows=True,
            allocation_policy=LEGACY_ALLOCATION_POLICY,
        )
        for row in result["rows"]:
            item = {
                "phase": phase,
                "snapshot_date": row["start_snapshot_date"],
                "snapshot_year": int(row["start_snapshot_date"][:4]),
                "forward_return": float(row["mean_equity_return"]),
            }
            item.update(
                {
                    name: float(value)
                    for name, value in row["market_state"].items()
                    if value is not None and math.isfinite(float(value))
                }
            )
            rows.append(item)
    return rows


def diagnose_schedule(spec) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = build_observations(spec)
    features = sorted(
        set.intersection(
            *(set(row) for row in rows),
        )
        - {"phase", "snapshot_date", "snapshot_year", "forward_return"}
    )
    diagnostics = []
    for feature in features:
        phase_test = leave_phase_out(rows, feature)
        time_test = expanding_year(rows, feature)
        diagnostics.append(
            {
                "schedule": spec.name,
                "feature": feature,
                "observation_count": sum(row.get(feature) is not None for row in rows),
                "overall_spearman": safe_spearman(rows, feature),
                "leave_phase_out": phase_test,
                "expanding_year": time_test,
                "candidate": (
                    (phase_test["return_weighted_hit_rate"] or 0.0) >= 0.55
                    and phase_test["phase_count_above_50pct"] >= 8
                    and (time_test["return_weighted_hit_rate"] or 0.0) >= 0.53
                ),
            }
        )
    diagnostics.sort(
        key=lambda row: (
            row["candidate"],
            row["leave_phase_out"]["return_weighted_hit_rate"] or 0.0,
            row["expanding_year"]["return_weighted_hit_rate"] or 0.0,
        ),
        reverse=True,
    )
    return diagnostics, {
        "schedule": spec.name,
        "observation_count": len(rows),
        "candidate_count": sum(row["candidate"] for row in diagnostics),
        "features": diagnostics,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_rows = []
    schedules = {}
    for spec in FORMAL_SCHEDULES:
        diagnostics, report = diagnose_schedule(spec)
        all_rows.extend(diagnostics)
        schedules[spec.name] = report
        print(f"SCHEDULE {spec.name} observations={report['observation_count']}")
        for row in diagnostics[:10]:
            phase = row["leave_phase_out"]
            time = row["expanding_year"]
            print(
                f"  {row['feature']:<42} candidate={str(row['candidate']):<5} "
                f"phase_weighted={phase['return_weighted_hit_rate']} "
                f"phase_win={phase['phase_count_above_50pct']}/12 "
                f"time_weighted={time['return_weighted_hit_rate']}"
            )
    payload = {
        "method": {
            "outcome": "selected CSI basket return over the next review window",
            "execution_lag_days": EXECUTION_LAG_DAYS,
            "external_cutoff": "strictly before snapshot date",
            "domestic_cutoff": "at or before snapshot date; execution is strictly later",
            "leave_phase_out": "feature direction and median learned on eleven phases, evaluated on the held-out phase",
            "expanding_year": "feature direction and median learned only on prior snapshot years",
            "candidate_gate": "phase weighted hit >=55%, at least 8/12 phases above 50%, expanding-year weighted hit >=53%",
        },
        "schedules": schedules,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "schedule",
                "feature",
                "observation_count",
                "overall_spearman",
                "candidate",
                "phase_weighted_hit_rate",
                "phase_count_above_50pct",
                "worst_phase_weighted_hit_rate",
                "expanding_year_weighted_hit_rate",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in all_rows:
            writer.writerow(
                {
                    **row,
                    "phase_weighted_hit_rate": row["leave_phase_out"]["return_weighted_hit_rate"],
                    "phase_count_above_50pct": row["leave_phase_out"]["phase_count_above_50pct"],
                    "worst_phase_weighted_hit_rate": row["leave_phase_out"]["worst_phase_weighted_hit_rate"],
                    "expanding_year_weighted_hit_rate": row["expanding_year"]["return_weighted_hit_rate"],
                }
            )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
