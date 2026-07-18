#!/usr/bin/env python3
"""Measure scorecard feature stability across calendar-neutral phase offsets."""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_scorecard_csi_generalization import (
    FORMAL_SCHEDULES,
    LEGACY_ALLOCATION_POLICY,
    MONTH_DRIFT_PHASES,
    run_phase_schedule,
)

OUT_DIR = ROOT / "data" / "backtests" / "feature_phase_stability"
OUT_JSON = OUT_DIR / "report.json"
OUT_ITEM_CSV = OUT_DIR / "score_items.csv"
OUT_GROUP_CSV = OUT_DIR / "cadence_groups.csv"
OUT_CONTINUOUS_CSV = OUT_DIR / "continuous_features.csv"
EXECUTION_LAG_DAYS = 3

CALENDAR_LOCKED_GROUPS = {"annual_event_step", "quarterly_release_lagged"}


def cadence_for_item(item: dict[str, Any]) -> str:
    name = str(item["name"])
    if name.startswith("央行口径") or "中央会议" in name:
        return "annual_event_step"
    if name.startswith("企业景气"):
        return "quarterly_release_lagged"
    if name.startswith("PPI"):
        return "monthly_release_lagged"
    if name.startswith("PMI") or "生产>订单" in name:
        return "monthly_release_same_period"
    if "印花税" in name or "国家队" in name or "房地产政策" in name:
        return "policy_event_rolling"
    return "daily_or_rolling"


def safe_corr(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    ranked_x = pd.Series(x).rank(method="average")
    ranked_y = pd.Series(y).rank(method="average")
    value = ranked_x.corr(ranked_y)
    return None if pd.isna(value) else float(value)


def phase_correlations(rows: list[dict[str, Any]], field: str) -> dict[int, float | None]:
    out: dict[int, float | None] = {}
    for phase in MONTH_DRIFT_PHASES:
        phase_rows = [row for row in rows if row["phase_month_offset"] == phase]
        usable = [row for row in phase_rows if row.get(field) is not None]
        out[phase] = safe_corr(
            [float(row[field]) for row in usable],
            [float(row["forward_equity_return"]) for row in usable],
        )
    return out


def sign_summary(values: list[float | None], expected_sign: int | None = None) -> dict[str, Any]:
    usable = [float(value) for value in values if value is not None]
    positive = sum(value > 0 for value in usable)
    negative = sum(value < 0 for value in usable)
    if expected_sign is None:
        consistency = max(positive, negative) / len(usable) if usable else None
    else:
        consistency = sum(value * expected_sign > 0 for value in usable) / len(usable) if usable else None
    return {
        "phase_count": len(usable),
        "positive_phase_count": positive,
        "negative_phase_count": negative,
        "sign_consistency": consistency,
        "min": min(usable) if usable else None,
        "max": max(usable) if usable else None,
    }


def direction_match_summary(scores: list[float], returns: list[float]) -> dict[str, Any]:
    pairs = [(score, ret) for score, ret in zip(scores, returns) if score != 0 and ret != 0]
    if not pairs:
        return {
            "prediction_count": 0,
            "hit_rate": None,
            "return_weighted_hit_rate": None,
            "mean_signed_return": None,
        }
    signed_returns = [(-1.0 if score > 0 else 1.0) * ret for score, ret in pairs]
    absolute_total = sum(abs(ret) for _, ret in pairs)
    return {
        "prediction_count": len(pairs),
        "hit_rate": sum(value > 0 for value in signed_returns) / len(signed_returns),
        "return_weighted_hit_rate": (
            sum(abs(ret) for value, (_, ret) in zip(signed_returns, pairs) if value > 0) / absolute_total
            if absolute_total
            else None
        ),
        "mean_signed_return": mean(signed_returns),
    }


def build_observations(spec) -> list[dict[str, Any]]:
    observations = []
    for phase in MONTH_DRIFT_PHASES:
        result = run_phase_schedule(
            spec,
            phase,
            EXECUTION_LAG_DAYS,
            include_rows=True,
            allocation_policy=LEGACY_ALLOCATION_POLICY,
        )
        for row in result["rows"]:
            group_scores: dict[str, int] = defaultdict(int)
            for item in row["score_items"]:
                group_scores[cadence_for_item(item)] += int(item["score"])
            locked_score = sum(group_scores.get(group, 0) for group in CALENDAR_LOCKED_GROUPS)
            observation = {
                "schedule": spec.name,
                "phase_month_offset": phase,
                "snapshot_month": int(row["start_snapshot_date"][5:7]),
                "snapshot_date": row["start_snapshot_date"],
                "cycle_index": row["cycle_index"],
                "review_index": row["review_index"],
                "forward_equity_return": float(row["mean_equity_return"]),
                "full_score": int(row["score"]),
                "calendar_locked_score": int(locked_score),
                "non_calendar_score": int(row["score"]) - int(locked_score),
                "score_items": row["score_items"],
                "feature_inputs": row["feature_inputs"],
            }
            for group in [
                "annual_event_step",
                "quarterly_release_lagged",
                "monthly_release_lagged",
                "monthly_release_same_period",
                "policy_event_rolling",
                "daily_or_rolling",
            ]:
                observation[f"group::{group}"] = int(group_scores.get(group, 0))
            observations.append(observation)
    return observations


def item_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    item_meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        for item in row["score_items"]:
            item_meta.setdefault(
                item["name"],
                {
                    "name": item["name"],
                    "dimension": item["dimension"],
                    "direction": item["direction"],
                    "score": int(item["score"]),
                    "cadence": cadence_for_item(item),
                },
            )

    output = []
    for name, meta in item_meta.items():
        phase_edges: dict[int, float | None] = {}
        trigger_months: Counter[int] = Counter()
        triggered_all = []
        not_triggered_all = []
        for phase in MONTH_DRIFT_PHASES:
            phase_rows = [row for row in rows if row["phase_month_offset"] == phase]
            triggered = [
                row["forward_equity_return"]
                for row in phase_rows
                if any(item["name"] == name for item in row["score_items"])
            ]
            not_triggered = [
                row["forward_equity_return"]
                for row in phase_rows
                if not any(item["name"] == name for item in row["score_items"])
            ]
            triggered_all.extend(triggered)
            not_triggered_all.extend(not_triggered)
            for row in phase_rows:
                if any(item["name"] == name for item in row["score_items"]):
                    trigger_months[row["snapshot_month"]] += 1
            if len(triggered) >= 3 and len(not_triggered) >= 3:
                raw_edge = mean(triggered) - mean(not_triggered)
                phase_edges[phase] = raw_edge if meta["score"] < 0 else -raw_edge
            else:
                phase_edges[phase] = None

        raw_overall_edge = mean(triggered_all) - mean(not_triggered_all) if triggered_all and not_triggered_all else None
        directional_edge = (
            raw_overall_edge if meta["score"] < 0 else -raw_overall_edge
        ) if raw_overall_edge is not None else None
        item_scores = [float(meta["score"])] * len(triggered_all)
        phase_direction_match = {}
        for phase in MONTH_DRIFT_PHASES:
            phase_triggered = [
                row["forward_equity_return"]
                for row in rows
                if row["phase_month_offset"] == phase
                and any(item["name"] == name for item in row["score_items"])
            ]
            phase_direction_match[phase] = direction_match_summary(
                [float(meta["score"])] * len(phase_triggered),
                phase_triggered,
            )
        output.append(
            meta
            | {
                "trigger_count": len(triggered_all),
                "trigger_rate": len(triggered_all) / len(rows),
                "triggered_mean_forward_return": mean(triggered_all) if triggered_all else None,
                "not_triggered_mean_forward_return": mean(not_triggered_all) if not_triggered_all else None,
                "directional_edge": directional_edge,
                "direction_match": direction_match_summary(item_scores, triggered_all),
                "phase_direction_match": phase_direction_match,
                "phase_directional_edges": phase_edges,
                "phase_edge_summary": sign_summary(list(phase_edges.values()), expected_sign=1),
                "trigger_month_counts": dict(sorted(trigger_months.items())),
            }
        )
    return sorted(output, key=lambda item: (item["cadence"], item["name"]))


def group_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ["full_score", "calendar_locked_score", "non_calendar_score"] + [
        key for key in rows[0] if key.startswith("group::")
    ]
    output = []
    for field in fields:
        usable = [row for row in rows if row.get(field) is not None]
        overall_corr = safe_corr(
            [float(row[field]) for row in usable],
            [float(row["forward_equity_return"]) for row in usable],
        )
        phase_corrs = phase_correlations(rows, field)
        phase_direction_match = {
            phase: direction_match_summary(
                [float(row[field]) for row in rows if row["phase_month_offset"] == phase],
                [float(row["forward_equity_return"]) for row in rows if row["phase_month_offset"] == phase],
            )
            for phase in MONTH_DRIFT_PHASES
        }
        output.append(
            {
                "field": field,
                "overall_spearman": overall_corr,
                "expected_risk_alignment": -overall_corr if overall_corr is not None else None,
                "phase_spearman": phase_corrs,
                "phase_sign_summary": sign_summary(list(phase_corrs.values()), expected_sign=-1),
                "direction_match": direction_match_summary(
                    [float(row[field]) for row in usable],
                    [float(row["forward_equity_return"]) for row in usable],
                ),
                "phase_direction_match": phase_direction_match,
            }
        )
    return output


def continuous_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = sorted({field for row in rows for field in row["feature_inputs"]})
    output = []
    for field in fields:
        flattened = []
        for row in rows:
            value = row["feature_inputs"].get(field)
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                flattened.append(row | {"feature_value": float(value)})
        if len(flattened) < 100 or len({row["feature_value"] for row in flattened}) < 3:
            continue
        overall_corr = safe_corr(
            [row["feature_value"] for row in flattened],
            [row["forward_equity_return"] for row in flattened],
        )
        phase_corrs = {}
        for phase in MONTH_DRIFT_PHASES:
            phase_rows = [row for row in flattened if row["phase_month_offset"] == phase]
            phase_corrs[phase] = safe_corr(
                [row["feature_value"] for row in phase_rows],
                [row["forward_equity_return"] for row in phase_rows],
            )
        output.append(
            {
                "feature": field,
                "observation_count": len(flattened),
                "overall_spearman": overall_corr,
                "phase_spearman": phase_corrs,
                "phase_sign_summary": sign_summary(list(phase_corrs.values())),
            }
        )
    return sorted(output, key=lambda item: item["feature"])


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    schedule_reports = {}
    all_items = []
    all_groups = []
    all_continuous = []
    total_observations = 0
    for spec in FORMAL_SCHEDULES:
        observations = build_observations(spec)
        total_observations += len(observations)
        items = item_diagnostics(observations)
        groups = group_diagnostics(observations)
        continuous = continuous_diagnostics(observations)
        for row in items:
            row["schedule"] = spec.name
        for row in groups:
            row["schedule"] = spec.name
        for row in continuous:
            row["schedule"] = spec.name
        all_items.extend(items)
        all_groups.extend(groups)
        all_continuous.extend(continuous)
        schedule_reports[spec.name] = {
            "schedule": {
                "cycle_months": spec.cycle_months,
                "review_interval_months": spec.review_interval_months,
            },
            "observation_count": len(observations),
            "cadence_groups": groups,
            "score_items": items,
            "continuous_features": continuous,
        }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "method": {
            "schedules": [spec.name for spec in FORMAL_SCHEDULES],
            "phase_month_offsets": MONTH_DRIFT_PHASES,
            "execution_lag_days": EXECUTION_LAG_DAYS,
            "observation_count": total_observations,
            "outcome": "selected CSI basket return over the next review window",
            "directional_edge": (
                "For a risk item, non-trigger return minus trigger return; for an opportunity item, "
                "trigger return minus non-trigger return. Positive is directionally correct."
            ),
            "direction_match": (
                "Positive score predicts a negative forward return and negative score predicts a positive "
                "forward return. Hit rates are computed observation by observation after each phase shift."
            ),
        },
        "by_schedule": schedule_reports,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(
        OUT_ITEM_CSV,
        all_items,
        [
            "schedule",
            "name",
            "dimension",
            "direction",
            "score",
            "cadence",
            "trigger_count",
            "trigger_rate",
            "triggered_mean_forward_return",
            "not_triggered_mean_forward_return",
            "directional_edge",
            "direction_match",
        ],
    )
    write_csv(
        OUT_GROUP_CSV,
        all_groups,
        ["schedule", "field", "overall_spearman", "expected_risk_alignment", "direction_match"],
    )
    write_csv(
        OUT_CONTINUOUS_CSV,
        all_continuous,
        ["schedule", "feature", "observation_count", "overall_spearman"],
    )

    print(f"observations={total_observations}")
    for spec in FORMAL_SCHEDULES:
        report = schedule_reports[spec.name]
        print(f"SCHEDULE {spec.name} observations={report['observation_count']}")
        for group in report["cadence_groups"]:
            if group["field"] in {"full_score", "calendar_locked_score", "non_calendar_score"}:
                print(
                    f"  GROUP {group['field']} corr={group['overall_spearman']} "
                    f"hit={group['direction_match']['hit_rate']} "
                    f"weighted_hit={group['direction_match']['return_weighted_hit_rate']}"
                )
        for item in sorted(
            [item for item in report["score_items"] if item["cadence"] in CALENDAR_LOCKED_GROUPS],
            key=lambda item: item["directional_edge"] if item["directional_edge"] is not None else -999,
        ):
            print(
                f"  ITEM {item['name']} cadence={item['cadence']} n={item['trigger_count']} "
                f"hit={item['direction_match']['hit_rate']} "
                f"weighted_hit={item['direction_match']['return_weighted_hit_rate']}"
            )
    print(f"Wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
