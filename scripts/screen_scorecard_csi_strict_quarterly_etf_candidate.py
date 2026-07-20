#!/usr/bin/env python3
"""Fast screen one strict quarterly ETF candidate without path-cache writes.

This keeps the production path construction and evaluation logic, but builds
one phase/lag path at a time so rejected candidates can fail fast before all
48 drift samples are computed.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import statistics
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.domestic_defensive_etf import (  # noqa: E402
    DEFENSIVE_POLICIES,
    describe_universe,
)
from backtest.domestic_equity_etf import (  # noqa: E402
    DIRECT_ETF_POLICIES,
    describe_equity_universe,
)
from backtest.csi_snapshot_selector import SELECTOR_POLICIES  # noqa: E402
from scripts.backtest_calendar_neutral_csi_tipp import (  # noqa: E402
    build_daily_path,
)
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE  # noqa: E402
from scripts.backtest_scorecard_csi_strict_quarterly_etf import (  # noqa: E402
    ANNUAL_MARKET_SCORECARD,
    EXECUTION_LAGS,
    RULES,
    decode_path_value,
    encode_path_value,
    evaluate_path,
    summarize,
)
from scripts.validate_scorecard_csi_generalization import (  # noqa: E402
    DIRECTION_MATCHED_FEATURE_POLICY,
    MONTH_DRIFT_PHASES,
    SCHEDULE_12M_3M,
    run_phase_schedule,
)
from scripts.strict_quarterly_data_cache import load_strict_quarterly_market_data  # noqa: E402


CasePair = tuple[int, int]
_WORKER_CONTEXT: dict[str, Any] = {}


def named(items, name: str):
    for item in items:
        if item.name == name:
            return item
    raise ValueError(f"unknown policy: {name}")


def json_default(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value)!r}")


def base_cache_file(
    cache_dir: Path,
    *,
    selector_name: str,
    phase: int,
    lag: int,
) -> Path:
    slug = f"{selector_name}__phase{phase:02d}__lag{lag}"
    if len(slug) > 120:
        digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:16]
        slug = f"{selector_name[:64]}__{digest}__phase{phase:02d}__lag{lag}"
    return cache_dir / f"{slug}.json"


def load_base_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return decode_path_value(json.loads(path.read_text(encoding="utf-8")))


def write_base_cache(path: Path, base: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(encode_path_value(base), ensure_ascii=False),
        encoding="utf-8",
    )


def parse_case_pair(value: str) -> CasePair:
    sep = ":" if ":" in value else ","
    try:
        phase_text, lag_text = value.split(sep, 1)
        return int(phase_text), int(lag_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"case must use PHASE:LAG or PHASE,LAG, got {value!r}"
        ) from exc


def failed_structural_case_pairs(path: Path) -> list[CasePair]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    failed = payload.get("failed_structural_cases")
    if failed is None:
        failed = payload.get("structural_capture", {}).get("failed_structural_cases", [])
    pairs = {
        (int(item["phase_month_offset"]), int(item["execution_lag_days"]))
        for item in failed
    }
    return sorted(pairs)


def selected_case_pairs(args: argparse.Namespace) -> list[CasePair]:
    selected: set[CasePair] = set()
    if args.failed_structural_cases_from:
        report_path = (
            args.failed_structural_cases_from
            if args.failed_structural_cases_from.is_absolute()
            else ROOT / args.failed_structural_cases_from
        )
        selected.update(failed_structural_case_pairs(report_path))
    if args.case:
        selected.update(args.case)

    phase_filter = set(args.phase or [])
    lag_filter = set(args.lag or [])
    if selected:
        if phase_filter:
            selected = {pair for pair in selected if pair[0] in phase_filter}
        if lag_filter:
            selected = {pair for pair in selected if pair[1] in lag_filter}
        pairs = sorted(selected)
    else:
        phases = tuple(args.phase) if args.phase else tuple(MONTH_DRIFT_PHASES)
        lags = tuple(args.lag) if args.lag else tuple(EXECUTION_LAGS)
        pairs = [(phase, lag) for phase in phases for lag in lags]

    invalid_phases = sorted({phase for phase, _lag in pairs} - set(MONTH_DRIFT_PHASES))
    invalid_lags = sorted({lag for _phase, lag in pairs} - set(EXECUTION_LAGS))
    if invalid_phases or invalid_lags:
        raise ValueError(f"invalid phases={invalid_phases} lags={invalid_lags}")
    if not pairs:
        raise ValueError("no phase/lag cases selected")
    return pairs


def summarize_screen_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    if len(cases) == len(MONTH_DRIFT_PHASES) * len(EXECUTION_LAGS):
        summary = summarize(cases)
        summary["partial_matrix"] = False
        summary["screen_passed"] = bool(summary["objective_met"])
        return summary
    return {
        "count": len(cases),
        "pass_count": sum(case["target_met"] for case in cases),
        "min_final_capital_wan": min(case["final_capital_wan"] for case in cases),
        "median_final_capital_wan": statistics.median(
            case["final_capital_wan"] for case in cases
        ),
        "worst_max_drawdown": min(case["max_drawdown"] for case in cases),
        "median_max_drawdown": statistics.median(case["max_drawdown"] for case in cases),
        "median_average_exposure": statistics.median(case["average_exposure"] for case in cases),
        "median_online_guard_count": statistics.median(case["online_guard_count"] for case in cases),
        "median_direction_risk_gate_rejection_count": statistics.median(
            case["direction_risk_gate_rejection_count"] for case in cases
        ),
        "median_selector_dispersion_recovery_count": statistics.median(
            case["selector_dispersion_recovery_count"] for case in cases
        ),
        "median_recovery_count": statistics.median(case["recovery_count"] for case in cases),
        "median_quality_high_count": statistics.median(
            case["quality_high_count"] for case in cases
        ),
        "median_quality_low_count": statistics.median(
            case["quality_low_count"] for case in cases
        ),
        "case_matrix": {"matrix_complete": False, "all_cases_pass": False},
        "partial_matrix": True,
        "objective_met": False,
        "screen_passed": all(case["target_met"] for case in cases),
    }


def run_screen_case(
    *,
    phase: int,
    lag: int,
    selector,
    direct_policy,
    rule,
    defensive_policy,
    index_series,
    defensive_metas,
    defensive_series,
    equity_metas,
    equity_series,
    include_decision_rows: bool,
    no_base_cache: bool,
    cache_dir: Path,
) -> tuple[int, int, dict[str, Any], float]:
    case_start = time.perf_counter()
    trade_dates = [day for day, _value in index_series[CS300_CODE]]
    cache_path = base_cache_file(
        cache_dir,
        selector_name=selector.name,
        phase=phase,
        lag=lag,
    )
    base = None if no_base_cache else load_base_cache(cache_path)
    if base is None:
        base = run_phase_schedule(
            SCHEDULE_12M_3M,
            phase,
            lag,
            include_rows=True,
            allocation_policy=ANNUAL_MARKET_SCORECARD,
            feature_policy=DIRECTION_MATCHED_FEATURE_POLICY,
            include_market_features=True,
            selector_policy=selector,
            selector_refresh_every_review=True,
            online_selector=False,
            online_ridge_selector=False,
            calendar_year_allocation_reset=True,
            common_completion_phase_offset=max(MONTH_DRIFT_PHASES),
            common_completion_lag_days=max(EXECUTION_LAGS),
            schedule_anchor=date(2005, 2, 28),
        )
        if not no_base_cache:
            write_base_cache(cache_path, base)
    path = build_daily_path(
        index_series,
        trade_dates,
        SCHEDULE_12M_3M,
        phase,
        lag,
        equity_metas,
        equity_series,
        ANNUAL_MARKET_SCORECARD,
        True,
        True,
        selector,
        direct_policy,
        False,
        False,
        True,
        max(MONTH_DRIFT_PHASES),
        max(EXECUTION_LAGS),
        date(2005, 2, 28),
        "execution",
        base_override=base,
    )
    case = evaluate_path(
        path,
        rule,
        equity_series,
        defensive_metas,
        defensive_series,
        defensive_policy,
        include_decision_rows=include_decision_rows,
    )
    return phase, lag, case, time.perf_counter() - case_start


def init_worker(
    rule_name: str,
    defensive_policy_name: str,
    selector_policy_name: str,
    direct_policy_name: str,
    include_decision_rows: bool,
    no_base_cache: bool,
    base_cache_dir: str,
    data_cache_dir: str,
    refresh_data_cache: bool,
    no_data_cache: bool,
) -> None:
    market_data = load_strict_quarterly_market_data(
        ROOT,
        Path(data_cache_dir),
        include_selector_index_series=True,
        refresh=refresh_data_cache,
        use_cache=not no_data_cache,
    )
    _WORKER_CONTEXT.clear()
    _WORKER_CONTEXT.update(
        {
            "rule": named(RULES, rule_name),
            "defensive_policy": named(DEFENSIVE_POLICIES, defensive_policy_name),
            "selector": named(SELECTOR_POLICIES, selector_policy_name),
            "direct_policy": named(DIRECT_ETF_POLICIES, direct_policy_name),
            "include_decision_rows": include_decision_rows,
            "no_base_cache": no_base_cache,
            "cache_dir": Path(base_cache_dir),
            "index_series": market_data["index_series"],
            "defensive_metas": market_data["defensive_metas"],
            "defensive_series": market_data["defensive_series"],
            "equity_metas": market_data["equity_metas"],
            "equity_series": market_data["equity_series"],
        }
    )


def run_screen_case_worker(pair: CasePair) -> tuple[int, int, dict[str, Any], float]:
    phase, lag = pair
    return run_screen_case(phase=phase, lag=lag, **_WORKER_CONTEXT)


def print_case_result(phase: int, lag: int, case: dict[str, Any], elapsed: float) -> None:
    print(
        f"phase={phase:02d} lag={lag} pass={int(case['target_met'])} "
        f"final={case['final_capital_wan']:.1f}万 "
        f"mdd={case['max_drawdown'] * 100:.2f}% "
        f"elapsed={elapsed:.1f}s",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", required=True)
    parser.add_argument("--defensive-policy", required=True)
    parser.add_argument("--selector-policy", required=True)
    parser.add_argument("--direct-etf-policy", required=True)
    parser.add_argument(
        "--full-matrix",
        action="store_true",
        help=(
            "Continue after hard-gate failures and run every selected sample; "
            "with default selection this is the full 48-sample matrix."
        ),
    )
    parser.add_argument(
        "--phase",
        type=int,
        action="append",
        help="Only run the given month-drift phase; may be repeated.",
    )
    parser.add_argument(
        "--lag",
        type=int,
        action="append",
        help="Only run the given execution lag; may be repeated.",
    )
    parser.add_argument(
        "--case",
        type=parse_case_pair,
        action="append",
        help="Run one explicit PHASE:LAG sample; may be repeated.",
    )
    parser.add_argument(
        "--failed-structural-cases-from",
        type=Path,
        help=(
            "Run only phase/lag pairs that failed structural capture in a "
            "structural-adaptation report."
        ),
    )
    parser.add_argument(
        "--include-decision-rows",
        action="store_true",
        help="Persist decision rows for a candidate that is already expected to pass.",
    )
    parser.add_argument(
        "--output-prefix",
        default="data/backtests/strict_quarterly_fast_screen",
    )
    parser.add_argument(
        "--base-cache-dir",
        type=Path,
        default=Path("data/backtests/cache/strict_quarterly_base_paths"),
        help="Small selector/phase/lag cache reused across direct ETF policies.",
    )
    parser.add_argument("--no-base-cache", action="store_true")
    parser.add_argument(
        "--data-cache-dir",
        type=Path,
        default=Path("data/backtests/cache/strict_quarterly_market_data"),
        help="Local cache for raw market data loaded from MySQL.",
    )
    parser.add_argument(
        "--refresh-data-cache",
        action="store_true",
        help="Reload raw market data from MySQL and overwrite the local cache.",
    )
    parser.add_argument(
        "--no-data-cache",
        action="store_true",
        help="Disable the raw market-data cache for this run.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help=(
            "Run selected phase/lag cases in parallel. Parallel mode is used "
            "only with --full-matrix so fail-fast semantics stay unchanged."
        ),
    )
    args = parser.parse_args()

    rule = named(RULES, args.rule)
    defensive_policy = named(DEFENSIVE_POLICIES, args.defensive_policy)
    selector = named(SELECTOR_POLICIES, args.selector_policy)
    direct_policy = named(DIRECT_ETF_POLICIES, args.direct_etf_policy)

    start_time = time.perf_counter()
    market_data = load_strict_quarterly_market_data(
        ROOT,
        args.data_cache_dir,
        include_selector_index_series=True,
        refresh=args.refresh_data_cache,
        use_cache=not args.no_data_cache,
    )
    index_series = market_data["index_series"]
    defensive_metas = market_data["defensive_metas"]
    defensive_series = market_data["defensive_series"]
    equity_metas = market_data["equity_metas"]
    equity_series = market_data["equity_series"]

    cases: list[dict[str, Any]] = []
    stopped_early = False
    cache_dir = args.base_cache_dir if args.base_cache_dir.is_absolute() else ROOT / args.base_cache_dir
    case_pairs = selected_case_pairs(args)
    print(f"selected_cases={len(case_pairs)}", flush=True)
    jobs = max(1, int(args.jobs))
    if jobs > 1 and args.full_matrix and len(case_pairs) > 1:
        worker_results: dict[CasePair, dict[str, Any]] = {}
        with ProcessPoolExecutor(
            max_workers=jobs,
            initializer=init_worker,
            initargs=(
                rule.name,
                defensive_policy.name,
                selector.name,
                direct_policy.name,
                args.include_decision_rows,
                args.no_base_cache,
                str(cache_dir),
                str(args.data_cache_dir),
                args.refresh_data_cache,
                args.no_data_cache,
            ),
        ) as executor:
            futures = {
                executor.submit(run_screen_case_worker, pair): pair
                for pair in case_pairs
            }
            for future in as_completed(futures):
                phase, lag, case, elapsed = future.result()
                worker_results[(phase, lag)] = case
                print_case_result(phase, lag, case, elapsed)
        cases = [worker_results[pair] for pair in case_pairs]
    else:
        for phase, lag in case_pairs:
            _phase, _lag, case, elapsed = run_screen_case(
                phase=phase,
                lag=lag,
                selector=selector,
                direct_policy=direct_policy,
                rule=rule,
                defensive_policy=defensive_policy,
                index_series=index_series,
                defensive_metas=defensive_metas,
                defensive_series=defensive_series,
                equity_metas=equity_metas,
                equity_series=equity_series,
                include_decision_rows=args.include_decision_rows,
                no_base_cache=args.no_base_cache,
                cache_dir=cache_dir,
            )
            cases.append(case)
            print_case_result(_phase, _lag, case, elapsed)
            if not args.full_matrix and not case["target_met"]:
                stopped_early = True
                break

    summary = summarize_screen_cases(cases)
    payload = {
        "mode": "fast_screen",
        "stopped_early": stopped_early,
        "elapsed_seconds": time.perf_counter() - start_time,
        "selector_policy": asdict(selector),
        "direct_etf_policy": asdict(direct_policy),
        "rule": asdict(rule),
        "defensive_policy": asdict(defensive_policy),
        "equity_etf_universe": describe_equity_universe(equity_metas),
        "defensive_etf_universe": describe_universe(defensive_metas),
        "selected_case_pairs": [
            {"phase_month_offset": phase, "execution_lag_days": lag}
            for phase, lag in case_pairs
        ],
        "summary": summary,
        "cases": cases,
    }
    prefix = Path(args.output_prefix)
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    out_path = Path(f"{prefix}_report.json")
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    print(
        f"summary pass={summary['pass_count']}/{len(cases)} "
        f"min={summary['min_final_capital_wan']:.1f}万 "
        f"mdd={summary['worst_max_drawdown'] * 100:.2f}% "
        f"partial={int(summary['partial_matrix'])} "
        f"stopped_early={int(stopped_early)}"
    )
    print(f"Wrote {out_path}")
    return 1 if stopped_early or not summary["screen_passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
