#!/usr/bin/env python3
"""Run the standardized CSI research-selection data pipeline.

Pipeline steps:
1. Optionally sync market/index/ETF daily data through the existing market sync.
2. Fail closed on stale or internally discontinuous domestic passive ETF prices.
3. Refresh East Money H2 industry research metadata used as ex-ante features.
4. Run the regime-aware CSI research backtest and validate the output.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

from db.connection import get_connection
from scripts.import_eastmoney_industry_reports import import_range

REPORT_JSON = ROOT / "data" / "ml" / "regime_research_csi_strategy_report.json"
PORTFOLIO_REPORT_JSON = ROOT / "data" / "backtests" / "scorecard_csi_portfolio_backtest.json"
MIDYEAR_RISK_REPORT_JSON = ROOT / "data" / "backtests" / "scorecard_csi_midyear_risk_report.json"
QUARTERLY_RISK_REPORT_JSON = ROOT / "data" / "backtests" / "scorecard_csi_quarterly_risk_report.json"
GENERALIZATION_REPORT_JSON = ROOT / "data" / "backtests" / "scorecard_csi_generalization_report.json"
DEFAULT_PORTFOLIO_SOURCE = "regime_momentum_hybrid_score0_floor95"
DEFAULT_MIDYEAR_RULE = "risk_off_score_positive_floor95"
DEFAULT_MIDYEAR_MODE = "semiannual_overlay_tightening_overheat_and_weak_rally_cap10"
DEFAULT_QUARTERLY_RULE = "risk_off_score_positive_floor95"
DEFAULT_QUARTERLY_MODE = "quarterly_overlay_quarterly_weak_repair_cap30"
DEFAULT_PHASE_ENSEMBLE_TARGET_RULE = "phase12_lever120_us10y"
DEFAULT_DEFINED_LOSS_TARGET_RULE = "defloss_spread95call108_mix95_8_floor010_prem075_up0"
DEFAULT_DOMESTIC_TIPP_TARGET_RULE = "max_mdd_margin"


def parse_year_range(raw: str) -> tuple[int, int]:
    if ":" in raw:
        start, end = raw.split(":", 1)
    elif "-" in raw:
        start, end = raw.split("-", 1)
    else:
        year = int(raw)
        return year, year
    return int(start), int(end)


def default_research_h2_years(today: date) -> tuple[int, int]:
    # Backtest coverage needs 2020H2-2024H2.  Ongoing runs also keep the latest
    # completed/current H2 in sync for the next annual selection cycle.
    latest = today.year if today.month >= 7 else today.year - 1
    return 2020, max(2024, latest)


def h2_window(year: int, today: date) -> tuple[str, str]:
    start = date(year, 7, 1)
    nominal_end = date(year, 12, 31)
    end = min(nominal_end, today)
    return start.isoformat(), end.isoformat()


def run_command(name: str, command: list[str], *, allow_failure: bool = False) -> int:
    print(f"\n=== Step: {name} ===")
    print("$ " + " ".join(command))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    print(f"=== Step complete: {name}, exit={completed.returncode} ===")
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(f"{name} failed with exit={completed.returncode}")
    return completed.returncode


def print_db_summary() -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print("\n=== DB coverage summary ===")
            for table, date_col, code_col in [
                ("index_daily", "trade_date", "ts_code"),
                ("index_dailybasic", "trade_date", "ts_code"),
                ("fund_daily", "trade_date", "ts_code"),
                ("index_constituent", "trade_date", "index_code"),
            ]:
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT {code_col}), COUNT(*), MIN({date_col}), MAX({date_col})
                    FROM {table}
                    """
                )
                n_codes, n_rows, min_date, max_date = cur.fetchone()
                print(f"{table}: codes={n_codes}, rows={n_rows}, range={min_date} ~ {max_date}")

            cur.execute(
                """
                SELECT YEAR(report_date), COUNT(*), COUNT(DISTINCT industry), COUNT(DISTINCT org_name)
                FROM broker_research_report
                WHERE source='eastmoney_api' AND report_type='industry'
                  AND MONTH(report_date) BETWEEN 7 AND 12
                GROUP BY YEAR(report_date)
                ORDER BY YEAR(report_date)
                """
            )
            print("eastmoney industry reports H2:")
            for y, reports, industries, orgs in cur.fetchall():
                print(f"  {y}: reports={reports}, industries={industries}, orgs={orgs}")
    finally:
        conn.close()


def refresh_research_reports(start_year: int, end_year: int, today: date, page_size: int, max_pages: int, sleep: float) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    for year in range(start_year, end_year + 1):
        start, end = h2_window(year, today)
        if start > end:
            continue
        print(f"\n=== Step: eastmoney_industry_reports_{year}H2 ===")
        result = import_range(start, end, page_size=page_size, max_pages=max_pages, sleep=sleep)
        item = {"h2_year": year, "from": start, "to": end, **result}
        stats.append(item)
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return stats


def validate_report(min_hits: int, min_worst_return: float) -> dict[str, Any]:
    payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    summary = payload["summary"]
    hits = int(summary["total_winner_hit"])
    worst = float(summary["worst_strategy_return"])
    if hits < min_hits:
        raise RuntimeError(f"winner-hit QA failed: hits={hits} < min_hits={min_hits}")
    if worst < min_worst_return:
        raise RuntimeError(f"worst-return QA failed: worst={worst:.4f} < min_worst_return={min_worst_return:.4f}")
    return summary


def validate_portfolio_report(source: str, min_final_capital: float) -> dict[str, Any]:
    payload = json.loads(PORTFOLIO_REPORT_JSON.read_text(encoding="utf-8"))
    summaries = {item["source"]: item for item in payload["summaries"]}
    if source not in summaries:
        raise RuntimeError(f"portfolio QA failed: source={source} missing from {PORTFOLIO_REPORT_JSON}")
    summary = summaries[source]
    final_capital = float(summary["final_capital"])
    if final_capital < min_final_capital:
        raise RuntimeError(
            f"portfolio QA failed: final_capital={final_capital:,.0f} < min_final_capital={min_final_capital:,.0f}"
        )
    return summary


def validate_midyear_risk_report(rule: str, mode: str, min_final_capital: float) -> dict[str, Any]:
    payload = json.loads(MIDYEAR_RISK_REPORT_JSON.read_text(encoding="utf-8"))
    summaries = {
        (item["rule"], item["mode"]): item
        for item in payload["reports"]
    }
    key = (rule, mode)
    if key not in summaries:
        raise RuntimeError(f"midyear risk QA failed: rule={rule} mode={mode} missing from {MIDYEAR_RISK_REPORT_JSON}")
    summary = summaries[key]
    final_capital = float(summary["final_capital"])
    max_drawdown = float(summary["max_drawdown"])
    target_mdd = float(summary["target_mdd"])
    if final_capital < min_final_capital:
        raise RuntimeError(
            f"midyear risk QA failed: final_capital={final_capital:,.0f} < min_final_capital={min_final_capital:,.0f}"
        )
    if max_drawdown < target_mdd:
        raise RuntimeError(
            f"midyear risk QA failed: max_drawdown={max_drawdown:.4f} < target_mdd={target_mdd:.4f}"
        )
    if not summary.get("target_met"):
        raise RuntimeError(f"midyear risk QA failed: target_met is false for rule={rule} mode={mode}")
    return summary


def validate_quarterly_risk_report(rule: str, mode: str, min_final_capital: float, max_drawdown_floor: float) -> dict[str, Any]:
    payload = json.loads(QUARTERLY_RISK_REPORT_JSON.read_text(encoding="utf-8"))
    summaries = {
        (item["rule"], item["mode"]): item
        for item in payload["reports"]
    }
    key = (rule, mode)
    if key not in summaries:
        raise RuntimeError(f"quarterly risk QA failed: rule={rule} mode={mode} missing from {QUARTERLY_RISK_REPORT_JSON}")
    summary = summaries[key]
    final_capital = float(summary["final_capital"])
    max_drawdown = float(summary["max_drawdown"])
    if final_capital < min_final_capital:
        raise RuntimeError(
            f"quarterly risk QA failed: final_capital={final_capital:,.0f} < min_final_capital={min_final_capital:,.0f}"
        )
    if max_drawdown < max_drawdown_floor:
        raise RuntimeError(
            f"quarterly risk QA failed: max_drawdown={max_drawdown:.4f} < max_drawdown_floor={max_drawdown_floor:.4f}"
        )
    if not summary.get("target_met"):
        raise RuntimeError(f"quarterly risk QA failed: target_met is false for rule={rule} mode={mode}")
    return summary


def validate_generalization_report() -> dict[str, Any]:
    payload = json.loads(GENERALIZATION_REPORT_JSON.read_text(encoding="utf-8"))
    validation = payload["validation"]
    if not validation.get("stable"):
        failed = [name for name, ok in validation["checks"].items() if not ok]
        summaries = payload.get("summaries", {})
        context = []
        for key in ["execution_drift", "phase_schedule_matrix"]:
            if key in summaries:
                summary = summaries[key]
                context.append(
                    f"{key}={summary['pass_count']}/{summary['count']} "
                    f"min_final={summary['min_final_capital_wan']:.1f}万 "
                    f"worst_mdd={summary['worst_max_drawdown'] * 100:.1f}%"
                )
        for key, summary in summaries.get("by_schedule", {}).items():
            context.append(
                f"{key}={summary['pass_count']}/{summary['count']} "
                f"min_final={summary['min_final_capital_wan']:.1f}万 "
                f"worst_mdd={summary['worst_max_drawdown'] * 100:.1f}%"
            )
        raise RuntimeError(f"generalization QA failed: {', '.join(failed)}; {'; '.join(context)}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CSI research-selection data sync, feature build, and backtest QA")
    parser.add_argument("--skip-market-sync", action="store_true", help="Skip scripts/sync_daily_market_data.py")
    parser.add_argument("--summary-only", action="store_true", help="Only verify DB coverage and exit")
    parser.add_argument("--research-h2-years", help="H2 research years to refresh, e.g. 2020:2026. Default: 2020 through current H2.")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=300)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--min-winner-hits", type=int, default=8)
    parser.add_argument("--min-worst-return", type=float, default=-0.08)
    parser.add_argument("--portfolio-source", default=DEFAULT_PORTFOLIO_SOURCE)
    parser.add_argument("--min-final-capital", type=float, default=40_000_000.0)
    parser.add_argument("--midyear-rule", default=DEFAULT_MIDYEAR_RULE)
    parser.add_argument("--midyear-mode", default=DEFAULT_MIDYEAR_MODE)
    parser.add_argument("--quarterly-rule", default=DEFAULT_QUARTERLY_RULE)
    parser.add_argument("--quarterly-mode", default=DEFAULT_QUARTERLY_MODE)
    parser.add_argument("--max-drawdown-floor", type=float, default=-0.10)
    parser.add_argument("--target-year", type=int, help="Apply year for production CSI portfolio targets. Default: current year.")
    parser.add_argument("--target-top", type=int, default=10)
    parser.add_argument("--target-as-of", help="Decision date for production targets, YYYY-MM-DD. Default: today.")
    parser.add_argument("--skip-phase-ensemble-targets", action="store_true", help="Skip phase-diversified production target generation.")
    parser.add_argument("--phase-ensemble-target-rule", default=DEFAULT_PHASE_ENSEMBLE_TARGET_RULE)
    parser.add_argument("--phase-ensemble-target-top-per-sleeve", type=int, default=0)
    parser.add_argument("--phase-ensemble-target-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--skip-defined-loss-targets", action="store_true", help="Skip modeled defined-loss production-shape target generation.")
    parser.add_argument("--skip-pre-option-regime-targets", action="store_true", help="Skip strict-pass pre-option regime target generation.")
    parser.add_argument("--skip-domestic-tipp-targets", action="store_true", help="Skip domestic-only strict-pass TIPP target generation.")
    parser.add_argument("--pre-option-regime-target-rule", default="best_balance", help="best_balance, best_drawdown, or exact strict-pass pre-option regime rule name.")
    parser.add_argument("--domestic-tipp-target-rule", default=DEFAULT_DOMESTIC_TIPP_TARGET_RULE, help="max_mdd_margin, max_min_capital, or exact domestic-only strict-pass TIPP rule name.")
    parser.add_argument("--domestic-tipp-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--skip-defined-loss-execution-audit", action="store_true", help="Skip execution-feasibility audit for the modeled defined-loss targets.")
    parser.add_argument("--skip-executable-option-package-search", action="store_true", help="Skip executable option-package search around the defined-loss target.")
    parser.add_argument("--skip-defined-loss-replication-stress", action="store_true", help="Skip listed-option replication stress test for the defined-loss target.")
    parser.add_argument("--skip-option-package-stress-ranking", action="store_true", help="Skip stress ranking for executable option-package candidates.")
    parser.add_argument("--skip-option-package-floor-cost-diagnostic", action="store_true", help="Skip unrestricted listed-option floor-cost diagnostic.")
    parser.add_argument("--require-defined-loss-execution-validated", action="store_true", help="Fail the pipeline if the defined-loss execution audit is not validated.")
    parser.add_argument("--defined-loss-target-rule", default=DEFAULT_DEFINED_LOSS_TARGET_RULE)
    parser.add_argument("--defined-loss-core-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--defined-loss-satellite-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--sync-external-assets", action="store_true", help="Refresh cached external ETF/index daily prices for hedge experiments.")
    parser.add_argument("--sync-cboe-option-indices", action="store_true", help="Refresh cached CBOE option-strategy and volatility indices.")
    parser.add_argument("--sync-us-option-chain", action="store_true", help="Refresh current US option-chain snapshots for defined-loss execution audits.")
    parser.add_argument("--option-chain-symbols", nargs="+", default=["QQQ"])
    parser.add_argument("--option-chain-provider", choices=["cboe_delayed_quotes", "yahoo_options"], default="cboe_delayed_quotes")
    parser.add_argument("--option-chain-min-dte", type=int, default=5)
    parser.add_argument("--option-chain-max-dte", type=int, default=120)
    parser.add_argument("--option-chain-max-expirations", type=int, default=12)
    parser.add_argument("--sync-fred-macro", action="store_true", help="Refresh cached FRED macro/credit-risk series.")
    parser.add_argument("--run-overlay-search", action="store_true", help="Run strict overlay candidate search after generalization QA.")
    parser.add_argument("--overlay-search-full", action="store_true", help="Run full lag/phase overlay search instead of quick search.")
    parser.add_argument("--run-dynamic-defense", action="store_true", help="Run dynamic monthly risk/defensive-leg experiment.")
    parser.add_argument("--run-vol-target", action="store_true", help="Run volatility-target/risk-budget experiment.")
    parser.add_argument("--run-phase-ensemble", action="store_true", help="Run phase-diversified rolling sleeve experiment.")
    parser.add_argument("--run-daily-guard", action="store_true", help="Run daily risk-guard experiment on phase-diversified sleeves.")
    parser.add_argument("--run-daily-tipp", action="store_true", help="Run daily TIPP/CPPI wrapper experiment on phase-diversified sleeves.")
    parser.add_argument("--run-feature-guard", action="store_true", help="Run pre-month feature guard experiment on phase-diversified sleeves.")
    parser.add_argument("--run-external-rotation", action="store_true", help="Run cached external ETF/index rotation hedge-sleeve experiment.")
    parser.add_argument("--run-external-daily-risk", action="store_true", help="Run daily VIX/trend/volatility risk-control experiment on cached external assets.")
    parser.add_argument("--run-option-protection", action="store_true", help="Run CBOE option-strategy protection-sleeve experiment.")
    parser.add_argument("--run-option-protection-tipp", action="store_true", help="Run daily TIPP overlay search on CBOE option-protection sleeves.")
    parser.add_argument("--run-cboe-blend", action="store_true", help="Run CSI phase-ensemble plus CBOE option-strategy blend search.")
    parser.add_argument("--run-cboe-feature-guard", action="store_true", help="Run observable pre-month feature guards on CSI plus CBOE blends.")
    parser.add_argument("--run-synthetic-option-hedge", action="store_true", help="Run modelled monthly defined-loss option hedge experiment.")
    parser.add_argument("--run-blended-protection", action="store_true", help="Run blended CSI scorecard plus synthetic option-protection experiment.")
    parser.add_argument("--run-blend-tipp-overlay", action="store_true", help="Run portfolio-level TIPP/CPPI sizing over blended protection sleeves.")
    parser.add_argument("--run-blend-tipp-expanded", action="store_true", help="Run expanded monthly TIPP/CPPI pressure grid over blended protection sleeves.")
    parser.add_argument("--run-cross-asset-tipp", action="store_true", help="Run cross-asset ETF momentum/defense sleeve TIPP experiment.")
    parser.add_argument("--run-trend-follow-tipp", action="store_true", help="Run cross-asset long/short trend-following TIPP experiment.")
    parser.add_argument("--run-futures-crisis-alpha", action="store_true", help="Run long-history futures trend sleeve crisis-alpha search.")
    parser.add_argument("--run-monthly-csi-selector", action="store_true", help="Run monthly ex-ante CSI price-feature selector experiment.")
    parser.add_argument("--run-cppi-protection", action="store_true", help="Run daily CPPI/TIPP portfolio-insurance experiment.")
    parser.add_argument("--run-tipp-option-overlay", action="store_true", help="Run TIPP/CPPI sizing on synthetic option-protected sleeves.")
    parser.add_argument("--run-crypto-tipp-overlay", action="store_true", help="Run daily TIPP/CPPI sizing on cached BTC/ETH external alpha sleeves.")
    parser.add_argument("--run-crypto-satellite-mix", action="store_true", help="Run small BTC/crypto satellite mix on the strongest low-drawdown CSI core.")
    parser.add_argument("--run-defined-loss-overlay", action="store_true", help="Run modeled monthly defined-loss overlay cost-boundary experiment.")
    parser.add_argument("--run-defined-loss-csi-hedge", action="store_true", help="Run modeled defined-loss overlay with CSI-linked hedge drag.")
    parser.add_argument("--run-cn-option-package-history", action="store_true", help="Run historical rolling proxy for the China ETF option package hedge.")
    parser.add_argument("--run-cn-option-package-real-history", action="store_true", help="Run historical listed-contract diagnostic for the China ETF option package hedge.")
    parser.add_argument("--run-cn-option-package-real-tipp", action="store_true", help="Run TIPP/CPPI wrapper search over historical listed China ETF option packages.")
    parser.add_argument("--run-cn-option-package-real-pre-guard", action="store_true", help="Run observable pre-month guard search over historical listed China ETF option packages.")
    parser.add_argument("--run-cn-option-package-real-daily-stop-proxy", action="store_true", help="Run proxy daily stop diagnostic over historical listed China ETF option packages.")
    parser.add_argument("--backfill-cn-option-daily-mtm", action="store_true", help="Backfill opt_daily snapshots needed by real option-package MTM diagnostics.")
    parser.add_argument("--cn-option-daily-mtm-limit", type=int, default=0, help="Maximum missing MTM trade dates to backfill; 0 means all.")
    parser.add_argument("--cn-option-daily-mtm-sleep", type=float, default=0.25, help="Sleep seconds between MTM opt_daily backfill requests.")
    parser.add_argument("--cn-option-daily-mtm-dry-run", action="store_true", help="Only print missing MTM opt_daily dates without fetching.")
    parser.add_argument("--run-cn-option-package-daily-mtm-stop", action="store_true", help="Run real option-leg daily MTM stop diagnostic over historical listed China ETF option packages.")
    parser.add_argument("--run-cn-option-package-hybrid-mtm-proxy-stop", action="store_true", help="Run hybrid option-MTM / CS300-proxy stop diagnostic.")
    parser.add_argument("--run-csi-pre-option-regime-defense", action="store_true", help="Run pre-option CS300 regime-defense diagnostic with listed-option MTM stop.")
    parser.add_argument("--diagnose-cn-option-package-daily-mtm-drawdowns", action="store_true", help="Diagnose worst drawdown windows for daily MTM stop rules.")
    parser.add_argument("--audit-cn-option-history-coverage", action="store_true", help="Audit historical opt_daily/basic coverage for China ETF options.")
    parser.add_argument("--run-walkforward-crash-guard", action="store_true", help="Run walk-forward crash-risk feature guard experiment.")
    parser.add_argument("--run-walkforward-loss-guard", action="store_true", help="Run walk-forward ordinary negative-month feature guard experiment.")
    parser.add_argument("--run-stump-loss-guard", action="store_true", help="Run nonlinear threshold-stump negative-month feature guard experiment.")
    parser.add_argument("--run-external-feature-guard", action="store_true", help="Run external full-window feature negative-month guard experiment.")
    parser.add_argument("--run-external-feature-guard-expanded", action="store_true", help="Run focused expanded external ETF feature negative-month guard experiment.")
    parser.add_argument("--run-boosted-loss-guard", action="store_true", help="Run boosted walk-forward ordinary-loss guard experiment.")
    parser.add_argument("--run-calendar-loss-guard", action="store_true", help="Run walk-forward calendar/phase ordinary-loss guard experiment.")
    parser.add_argument("--run-macro-risk-guard", action="store_true", help="Run FRED macro/credit-risk guard experiment.")
    parser.add_argument("--run-oracle-upper-bound", action="store_true", help="Run non-investable oracle upper-bound diagnostic.")
    parser.add_argument("--run-executable-frontier-diagnostic", action="store_true", help="Diagnose executable non-defined-loss frontier gaps.")
    parser.add_argument("--summarize-csi-frontier", action="store_true", help="Summarize all scorecard + CSI strict-search outputs before final QA.")
    args = parser.parse_args()

    today = date.today()
    target_year = args.target_year or today.year
    print("Verifying MySQL connection with project config...")
    print_db_summary()
    if args.summary_only:
        return 0

    if not args.skip_market_sync:
        run_command("daily_market_data_sync", [sys.executable, "scripts/sync_daily_market_data.py"])
    run_command(
        "passive_etf_price_continuity_audit",
        [sys.executable, "scripts/audit_passive_etf_price_continuity.py"],
    )
    if args.sync_external_assets:
        run_command("external_asset_daily_sync", [sys.executable, "scripts/import_external_asset_daily.py"])
    if args.sync_cboe_option_indices:
        run_command("cboe_option_indices_sync", [sys.executable, "scripts/import_cboe_option_indices.py"])
    if args.sync_us_option_chain:
        run_command(
            "us_option_chain_snapshot_sync",
            [
                sys.executable,
                "scripts/import_us_option_chain_snapshot.py",
                "--provider",
                args.option_chain_provider,
                "--symbols",
                *args.option_chain_symbols,
                "--quote-date",
                args.target_as_of or today.isoformat(),
                "--min-dte",
                str(args.option_chain_min_dte),
                "--max-dte",
                str(args.option_chain_max_dte),
                "--max-expirations",
                str(args.option_chain_max_expirations),
            ],
        )
    if args.sync_fred_macro:
        run_command("fred_macro_sync", [sys.executable, "scripts/import_fred_macro_series.py"])

    start_year, end_year = parse_year_range(args.research_h2_years) if args.research_h2_years else default_research_h2_years(today)
    research_stats = refresh_research_reports(start_year, end_year, today, args.page_size, args.max_pages, args.sleep)

    run_command("regime_research_csi_backtest", [sys.executable, "scripts/backtest_regime_research_csi_strategy.py"])
    summary = validate_report(args.min_winner_hits, args.min_worst_return)
    run_command("regime_momentum_hybrid_backtest", [sys.executable, "scripts/backtest_csi_regime_momentum_hybrid.py"])
    run_command("scorecard_20y_baseline", [sys.executable, "scripts/simulate_scorecard_20y.py"])
    run_command(
        "scorecard_csi_portfolio_backtest",
        [
            sys.executable,
            "scripts/backtest_scorecard_csi_portfolio.py",
            "--source",
            args.portfolio_source,
        ],
    )
    portfolio_summary = validate_portfolio_report(args.portfolio_source, args.min_final_capital)
    run_command("scorecard_csi_midyear_risk_backtest", [sys.executable, "scripts/backtest_scorecard_csi_midyear_risk.py"])
    midyear_summary = validate_midyear_risk_report(args.midyear_rule, args.midyear_mode, args.min_final_capital)
    run_command("scorecard_csi_quarterly_risk_backtest", [sys.executable, "scripts/backtest_scorecard_csi_quarterly_risk.py"])
    quarterly_summary = validate_quarterly_risk_report(
        args.quarterly_rule,
        args.quarterly_mode,
        args.min_final_capital,
        args.max_drawdown_floor,
    )
    run_command(
        "scorecard_csi_generalization_validation",
        [sys.executable, "scripts/validate_scorecard_csi_generalization.py"],
        allow_failure=True,
    )
    if args.run_overlay_search:
        command = [sys.executable, "scripts/search_scorecard_csi_strict_overlays.py"]
        if args.overlay_search_full:
            command.append("--full")
        run_command("scorecard_csi_overlay_search", command, allow_failure=True)
    if args.run_dynamic_defense:
        run_command(
            "scorecard_csi_dynamic_defense",
            [sys.executable, "scripts/backtest_scorecard_csi_dynamic_defense.py"],
            allow_failure=True,
        )
    if args.run_vol_target:
        run_command(
            "scorecard_csi_vol_target",
            [sys.executable, "scripts/backtest_scorecard_csi_vol_target.py"],
            allow_failure=True,
        )
    if args.run_phase_ensemble:
        run_command(
            "scorecard_csi_phase_ensemble",
            [sys.executable, "scripts/backtest_scorecard_csi_phase_ensemble.py"],
            allow_failure=True,
        )
    if args.run_daily_guard:
        run_command(
            "scorecard_csi_daily_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_daily_guard.py"],
            allow_failure=True,
        )
    if args.run_daily_tipp:
        run_command(
            "scorecard_csi_daily_tipp",
            [sys.executable, "scripts/backtest_scorecard_csi_daily_tipp.py"],
            allow_failure=True,
        )
    if args.run_feature_guard:
        run_command(
            "scorecard_csi_feature_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_feature_guard.py"],
            allow_failure=True,
        )
    if args.run_external_rotation:
        run_command(
            "scorecard_csi_external_rotation",
            [sys.executable, "scripts/backtest_scorecard_csi_external_rotation.py"],
            allow_failure=True,
        )
    if args.run_external_daily_risk:
        run_command(
            "scorecard_csi_external_daily_risk",
            [sys.executable, "scripts/backtest_scorecard_csi_external_daily_risk.py"],
            allow_failure=True,
        )
    if args.run_option_protection:
        run_command(
            "scorecard_csi_option_protection",
            [sys.executable, "scripts/backtest_scorecard_csi_option_protection.py"],
            allow_failure=True,
        )
    if args.run_option_protection_tipp:
        run_command(
            "scorecard_csi_option_protection_tipp",
            [sys.executable, "scripts/search_scorecard_csi_option_protection_tipp.py"],
            allow_failure=True,
        )
    if args.run_cboe_blend:
        run_command(
            "scorecard_csi_cboe_blend",
            [sys.executable, "scripts/search_scorecard_csi_cboe_blend.py"],
            allow_failure=True,
        )
    if args.run_cboe_feature_guard:
        run_command(
            "scorecard_csi_cboe_feature_guard",
            [sys.executable, "scripts/search_scorecard_csi_cboe_feature_guard.py"],
            allow_failure=True,
        )
    if args.run_synthetic_option_hedge:
        run_command(
            "scorecard_csi_synthetic_option_hedge",
            [sys.executable, "scripts/backtest_scorecard_csi_synthetic_option_hedge.py"],
            allow_failure=True,
        )
    if args.run_blended_protection:
        run_command(
            "scorecard_csi_blended_protection",
            [sys.executable, "scripts/backtest_scorecard_csi_blended_protection.py"],
            allow_failure=True,
        )
    if args.run_blend_tipp_overlay:
        run_command(
            "scorecard_csi_blend_tipp_overlay",
            [sys.executable, "scripts/backtest_scorecard_csi_blend_tipp_overlay.py"],
            allow_failure=True,
        )
    if args.run_blend_tipp_expanded:
        run_command(
            "scorecard_csi_blend_tipp_expanded",
            [sys.executable, "scripts/backtest_scorecard_csi_blend_tipp_expanded.py"],
            allow_failure=True,
        )
    if args.run_cross_asset_tipp:
        run_command(
            "scorecard_csi_cross_asset_tipp",
            [sys.executable, "scripts/backtest_scorecard_csi_cross_asset_tipp.py"],
            allow_failure=True,
        )
    if args.run_trend_follow_tipp:
        run_command(
            "scorecard_csi_trend_follow_tipp",
            [sys.executable, "scripts/backtest_scorecard_csi_trend_follow_tipp.py"],
            allow_failure=True,
        )
    if args.run_futures_crisis_alpha:
        run_command(
            "scorecard_csi_futures_crisis_alpha",
            [sys.executable, "scripts/search_scorecard_csi_futures_crisis_alpha.py"],
            allow_failure=True,
        )
    if args.run_monthly_csi_selector:
        run_command(
            "scorecard_csi_monthly_selector",
            [sys.executable, "scripts/backtest_scorecard_csi_monthly_selector.py"],
            allow_failure=True,
        )
    if args.run_cppi_protection:
        run_command(
            "scorecard_csi_cppi_protection",
            [sys.executable, "scripts/backtest_scorecard_csi_cppi_protection.py"],
            allow_failure=True,
        )
    if args.run_tipp_option_overlay:
        run_command(
            "scorecard_csi_tipp_option_overlay",
            [sys.executable, "scripts/backtest_scorecard_csi_tipp_option_overlay.py"],
            allow_failure=True,
        )
    if args.run_crypto_tipp_overlay:
        run_command(
            "scorecard_csi_crypto_tipp_overlay",
            [sys.executable, "scripts/backtest_scorecard_csi_crypto_tipp_overlay.py"],
            allow_failure=True,
        )
    if args.run_crypto_satellite_mix:
        run_command(
            "scorecard_csi_crypto_satellite_mix",
            [sys.executable, "scripts/backtest_scorecard_csi_crypto_satellite_mix.py"],
            allow_failure=True,
        )
    if args.run_defined_loss_overlay:
        run_command(
            "scorecard_csi_defined_loss_overlay",
            [sys.executable, "scripts/backtest_scorecard_csi_defined_loss_overlay.py"],
            allow_failure=True,
        )
    if args.run_defined_loss_csi_hedge:
        run_command(
            "scorecard_csi_defined_loss_csi_hedge",
            [sys.executable, "scripts/search_scorecard_csi_defined_loss_csi_hedge.py"],
            allow_failure=True,
        )
    if args.run_cn_option_package_history:
        run_command(
            "scorecard_csi_cn_option_package_history",
            [sys.executable, "scripts/search_scorecard_csi_cn_option_package_history.py"],
            allow_failure=True,
        )
    if args.run_cn_option_package_real_history:
        run_command(
            "scorecard_csi_cn_option_package_real_history_misszero",
            [
                sys.executable,
                "scripts/search_scorecard_csi_cn_option_package_real_history.py",
                "--missing-package-policy",
                "zero",
            ],
            allow_failure=True,
        )
        run_command(
            "scorecard_csi_cn_option_package_real_history_missproxy",
            [
                sys.executable,
                "scripts/search_scorecard_csi_cn_option_package_real_history.py",
                "--missing-package-policy",
                "proxy",
            ],
            allow_failure=True,
        )
    if args.run_cn_option_package_real_tipp:
        run_command(
            "scorecard_csi_cn_option_package_real_tipp_misszero",
            [
                sys.executable,
                "scripts/search_scorecard_csi_cn_option_package_real_tipp.py",
                "--missing-package-policy",
                "zero",
            ],
            allow_failure=True,
        )
    if args.run_cn_option_package_real_pre_guard:
        run_command(
            "scorecard_csi_cn_option_package_real_pre_guard_misszero",
            [
                sys.executable,
                "scripts/search_scorecard_csi_cn_option_package_real_pre_guard.py",
                "--missing-package-policy",
                "zero",
            ],
            allow_failure=True,
        )
    if args.run_cn_option_package_real_daily_stop_proxy:
        run_command(
            "scorecard_csi_cn_option_package_real_daily_stop_proxy_misszero",
            [
                sys.executable,
                "scripts/search_scorecard_csi_cn_option_package_real_daily_stop_proxy.py",
                "--missing-package-policy",
                "zero",
            ],
            allow_failure=True,
        )
    if args.backfill_cn_option_daily_mtm:
        command = [
            sys.executable,
            "scripts/backfill_cn_option_daily_for_mtm.py",
            "--missing-package-policy",
            "zero",
            "--limit",
            str(args.cn_option_daily_mtm_limit),
            "--sleep",
            str(args.cn_option_daily_mtm_sleep),
        ]
        if args.cn_option_daily_mtm_dry_run:
            command.append("--dry-run")
        run_command("cn_option_daily_mtm_backfill", command, allow_failure=True)
    if args.run_cn_option_package_daily_mtm_stop:
        run_command(
            "scorecard_csi_cn_option_package_daily_mtm_stop_misszero",
            [
                sys.executable,
                "scripts/search_scorecard_csi_cn_option_package_daily_mtm_stop.py",
                "--missing-package-policy",
                "zero",
            ],
            allow_failure=True,
        )
    if args.run_cn_option_package_hybrid_mtm_proxy_stop:
        run_command(
            "scorecard_csi_cn_option_package_hybrid_mtm_proxy_stop_misszero",
            [
                sys.executable,
                "scripts/search_scorecard_csi_cn_option_package_hybrid_mtm_proxy_stop.py",
                "--missing-package-policy",
                "zero",
            ],
            allow_failure=True,
        )
    if args.diagnose_cn_option_package_daily_mtm_drawdowns:
        run_command(
            "scorecard_csi_cn_option_package_daily_mtm_drawdowns",
            [
                sys.executable,
                "scripts/diagnose_scorecard_csi_cn_option_daily_mtm_drawdowns.py",
                "--missing-package-policy",
                "zero",
            ],
            allow_failure=True,
        )
    if args.run_csi_pre_option_regime_defense:
        run_command(
            "scorecard_csi_pre_option_regime_defense",
            [sys.executable, "scripts/search_scorecard_csi_pre_option_regime_defense.py"],
            allow_failure=True,
        )
    if args.audit_cn_option_history_coverage:
        run_command(
            "cn_option_history_coverage_audit",
            [sys.executable, "scripts/audit_cn_option_history_coverage.py"],
            allow_failure=True,
        )
    if args.run_walkforward_crash_guard:
        run_command(
            "scorecard_csi_walkforward_crash_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_walkforward_crash_guard.py"],
            allow_failure=True,
        )
    if args.run_walkforward_loss_guard:
        run_command(
            "scorecard_csi_walkforward_loss_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_walkforward_loss_guard.py"],
            allow_failure=True,
        )
    if args.run_stump_loss_guard:
        run_command(
            "scorecard_csi_stump_loss_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_stump_loss_guard.py"],
            allow_failure=True,
        )
    if args.run_external_feature_guard:
        run_command(
            "scorecard_csi_external_feature_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_external_feature_guard.py"],
            allow_failure=True,
        )
    if args.run_external_feature_guard_expanded:
        run_command(
            "scorecard_csi_external_feature_guard_expanded",
            [sys.executable, "scripts/backtest_scorecard_csi_external_feature_guard_expanded.py"],
            allow_failure=True,
        )
    if args.run_boosted_loss_guard:
        run_command(
            "scorecard_csi_boosted_loss_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_boosted_loss_guard.py"],
            allow_failure=True,
        )
    if args.run_calendar_loss_guard:
        run_command(
            "scorecard_csi_calendar_loss_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_calendar_loss_guard.py"],
            allow_failure=True,
        )
    if args.run_macro_risk_guard:
        run_command(
            "scorecard_csi_macro_risk_guard",
            [sys.executable, "scripts/backtest_scorecard_csi_macro_risk_guard.py"],
            allow_failure=True,
        )
    if args.run_oracle_upper_bound:
        run_command(
            "scorecard_csi_oracle_upper_bound",
            [sys.executable, "scripts/diagnose_scorecard_csi_oracle_upper_bound.py"],
            allow_failure=True,
        )
    if args.run_executable_frontier_diagnostic:
        run_command(
            "scorecard_csi_executable_frontier_diagnostic",
            [sys.executable, "scripts/diagnose_scorecard_csi_executable_frontier.py"],
            allow_failure=True,
        )
    if args.summarize_csi_frontier:
        run_command(
            "scorecard_csi_frontier_summary",
            [sys.executable, "scripts/summarize_scorecard_csi_frontier.py"],
            allow_failure=True,
        )
    generalization_summary = validate_generalization_report()
    run_command(
        "annual_csi_recommendation",
        [
            sys.executable,
            "scripts/rank_annual_csi.py",
            "--year",
            str(target_year),
            "--top",
            "30",
            "--suffix",
            "CSI",
            "--full",
            "--save",
        ],
    )
    run_command(
        "annual_csi_portfolio_targets",
        [
            sys.executable,
            "scripts/generate_csi_portfolio_targets.py",
            "--year",
            str(target_year),
            "--top",
            str(args.target_top),
            "--as-of",
            args.target_as_of or today.isoformat(),
        ],
    )
    run_command(
        "calendar_neutral_monthly_csi_targets",
        [sys.executable, "scripts/generate_calendar_neutral_csi_targets.py"],
    )
    if not args.skip_phase_ensemble_targets:
        run_command(
            "phase_ensemble_csi_portfolio_targets",
            [
                sys.executable,
                "scripts/generate_csi_phase_ensemble_targets.py",
                "--rule",
                args.phase_ensemble_target_rule,
                "--as-of",
                args.target_as_of or today.isoformat(),
                "--top-per-sleeve",
                str(args.phase_ensemble_target_top_per_sleeve),
                "--portfolio-drawdown-pct",
                str(args.phase_ensemble_target_drawdown_pct),
            ],
        )
    if not args.skip_defined_loss_targets:
        defined_loss_as_of = args.target_as_of or today.isoformat()
        run_command(
            "defined_loss_csi_portfolio_targets",
            [
                sys.executable,
                "scripts/generate_csi_defined_loss_overlay_targets.py",
                "--rule",
                args.defined_loss_target_rule,
                "--as-of",
                defined_loss_as_of,
                "--top-per-sleeve",
                str(args.phase_ensemble_target_top_per_sleeve),
                "--portfolio-drawdown-pct",
                str(args.phase_ensemble_target_drawdown_pct),
                "--core-drawdown-pct",
                str(args.defined_loss_core_drawdown_pct),
                "--satellite-drawdown-pct",
                str(args.defined_loss_satellite_drawdown_pct),
            ],
        )
        if not args.skip_defined_loss_execution_audit:
            stamp = defined_loss_as_of.replace("-", "")
            audit_command = [
                sys.executable,
                "scripts/audit_defined_loss_execution_feasibility.py",
                "--as-of",
                defined_loss_as_of,
                "--target-json",
                f"data/portfolio/csi_defined_loss_overlay_targets_{stamp}.json",
            ]
            if args.require_defined_loss_execution_validated:
                audit_command.append("--strict")
            run_command(
                "defined_loss_execution_feasibility_audit",
                audit_command,
                allow_failure=not args.require_defined_loss_execution_validated,
            )
            if not args.skip_defined_loss_replication_stress:
                run_command(
                    "defined_loss_replication_stress",
                    [
                        sys.executable,
                        "scripts/stress_defined_loss_replication.py",
                        "--as-of",
                        defined_loss_as_of,
                        "--target-json",
                        f"data/portfolio/csi_defined_loss_overlay_targets_{stamp}.json",
                        "--audit-json",
                        f"data/portfolio/csi_defined_loss_execution_audit_{stamp}.json",
                    ],
                    allow_failure=True,
                )
            if not args.skip_executable_option_package_search:
                run_command(
                    "executable_option_package_search",
                    [
                        sys.executable,
                        "scripts/search_executable_option_package_candidates.py",
                        "--target-json",
                        f"data/portfolio/csi_defined_loss_overlay_targets_{stamp}.json",
                        "--as-of",
                        defined_loss_as_of,
                        "--source",
                        args.option_chain_provider,
                    ],
                    allow_failure=True,
                )
                if not args.skip_option_package_stress_ranking:
                    run_command(
                        "option_package_stress_ranking",
                        [
                            sys.executable,
                            "scripts/rank_option_package_stress_candidates.py",
                            "--as-of",
                            defined_loss_as_of,
                            "--target-json",
                            f"data/portfolio/csi_defined_loss_overlay_targets_{stamp}.json",
                            "--candidate-csv",
                            f"data/portfolio/executable_option_package_search_{stamp}.csv",
                        ],
                        allow_failure=True,
                    )
                if not args.skip_option_package_floor_cost_diagnostic:
                    run_command(
                        "option_package_floor_cost_diagnostic",
                        [
                            sys.executable,
                            "scripts/diagnose_option_package_floor_cost.py",
                            "--as-of",
                            defined_loss_as_of,
                            "--target-json",
                            f"data/portfolio/csi_defined_loss_overlay_targets_{stamp}.json",
                            "--source",
                            args.option_chain_provider,
                            "--put-cover-multipliers",
                            "1,1.5,2,3,4,5",
                            "--csi-hedge-pcts",
                            "0,10,20,23,25,30",
                            "--csi-hedge-cost-annual-pct",
                            "1.0",
                            "--output-prefix",
                            f"data/portfolio/option_package_floor_cost_csihedge_{stamp}",
                        ],
                        allow_failure=True,
                    )
                    run_command(
                        "cn_etf_put_hedge_stress_search",
                        [
                            sys.executable,
                            "scripts/search_cn_etf_put_hedge_stress.py",
                            "--as-of",
                            defined_loss_as_of,
                            "--target-json",
                            f"data/portfolio/csi_defined_loss_overlay_targets_{stamp}.json",
                            "--floor-cost-json",
                            f"data/portfolio/option_package_floor_cost_csihedge_{stamp}.json",
                        ],
                        allow_failure=True,
                    )
                    run_command(
                        "cn_etf_option_package_hedge_stress_search",
                        [
                            sys.executable,
                            "scripts/search_cn_etf_option_package_hedge.py",
                            "--as-of",
                            defined_loss_as_of,
                            "--target-json",
                            f"data/portfolio/csi_defined_loss_overlay_targets_{stamp}.json",
                            "--floor-cost-json",
                            f"data/portfolio/option_package_floor_cost_csihedge_{stamp}.json",
                        ],
                        allow_failure=True,
                    )

    if not args.skip_pre_option_regime_targets:
        pre_option_as_of = args.target_as_of or today.isoformat()
        run_command(
            "pre_option_regime_csi_portfolio_targets",
            [
                sys.executable,
                "scripts/generate_csi_pre_option_regime_targets.py",
                "--rule",
                args.pre_option_regime_target_rule,
                "--as-of",
                pre_option_as_of,
                "--top-per-sleeve",
                str(args.phase_ensemble_target_top_per_sleeve),
                "--portfolio-drawdown-pct",
                str(args.phase_ensemble_target_drawdown_pct),
                "--core-drawdown-pct",
                str(args.defined_loss_core_drawdown_pct),
                "--satellite-drawdown-pct",
                str(args.defined_loss_satellite_drawdown_pct),
            ],
        )
    if not args.skip_domestic_tipp_targets:
        domestic_tipp_as_of = args.target_as_of or today.isoformat()
        run_command(
            "domestic_only_tipp_csi_portfolio_targets",
            [
                sys.executable,
                "scripts/generate_csi_domestic_tipp_targets.py",
                "--rule-selector",
                args.domestic_tipp_target_rule,
                "--as-of",
                domestic_tipp_as_of,
                "--top-per-sleeve",
                str(args.phase_ensemble_target_top_per_sleeve),
                "--strategy-drawdown-pct",
                str(args.domestic_tipp_drawdown_pct),
            ],
        )

    print("\n=== CSI research pipeline complete ===")
    print(
        "summary: "
        f"mean_strategy_return={float(summary['mean_strategy_return']) * 100:.1f}% "
        f"mean_excess_return={float(summary['mean_excess_return']) * 100:.1f}% "
        f"worst_strategy_return={float(summary['worst_strategy_return']) * 100:.1f}% "
        f"total_winner_hit={summary['total_winner_hit']}"
    )
    print(
        "portfolio: "
        f"source={portfolio_summary['source']} "
        f"final_capital={float(portfolio_summary['final_capital']):,.0f} "
        f"multiple={float(portfolio_summary['multiple']):.2f} "
        f"annualized_return={float(portfolio_summary['annualized_return']) * 100:.1f}% "
        f"max_drawdown={float(portfolio_summary['max_drawdown']) * 100:.1f}% "
        f"target_met={portfolio_summary['target_met']}"
    )
    print(
        "midyear_risk: "
        f"rule={midyear_summary['rule']} "
        f"mode={midyear_summary['mode']} "
        f"final_capital={float(midyear_summary['final_capital']):,.0f} "
        f"multiple={float(midyear_summary['multiple']):.2f} "
        f"annualized_return={float(midyear_summary['annualized_return']) * 100:.1f}% "
        f"max_drawdown={float(midyear_summary['max_drawdown']) * 100:.1f}% "
        f"target_met={midyear_summary['target_met']}"
    )
    print(
        "quarterly_risk: "
        f"rule={quarterly_summary['rule']} "
        f"mode={quarterly_summary['mode']} "
        f"final_capital={float(quarterly_summary['final_capital']):,.0f} "
        f"multiple={float(quarterly_summary['multiple']):.2f} "
        f"annualized_return={float(quarterly_summary['annualized_return']) * 100:.1f}% "
        f"max_drawdown={float(quarterly_summary['max_drawdown']) * 100:.1f}% "
        f"target_met={quarterly_summary['target_met']}"
    )
    print(
        "generalization: "
        f"stable={generalization_summary['validation']['stable']} "
        f"base_final_capital={float(generalization_summary['production_reference']['final_capital']):,.0f} "
        f"base_max_drawdown={float(generalization_summary['production_reference']['max_drawdown']) * 100:.1f}% "
        f"drift_min_annualized={float(generalization_summary['summaries']['execution_drift']['min_annualized_return']) * 100:.1f}% "
        f"drift_worst_drawdown={float(generalization_summary['summaries']['execution_drift']['worst_max_drawdown']) * 100:.1f}% "
        f"phase_matrix_pass={generalization_summary['summaries']['phase_schedule_matrix']['pass_count']}/"
        f"{generalization_summary['summaries']['phase_schedule_matrix']['count']} "
        f"phase_matrix_min_annualized={float(generalization_summary['summaries']['phase_schedule_matrix']['min_annualized_return']) * 100:.1f}% "
        f"phase_matrix_worst_drawdown={float(generalization_summary['summaries']['phase_schedule_matrix']['worst_max_drawdown']) * 100:.1f}%"
    )
    print("research_imports=" + json.dumps(research_stats, ensure_ascii=False, sort_keys=True))
    print(f"report={REPORT_JSON}")
    print(f"portfolio_report={PORTFOLIO_REPORT_JSON}")
    print(f"midyear_risk_report={MIDYEAR_RISK_REPORT_JSON}")
    print(f"quarterly_risk_report={QUARTERLY_RISK_REPORT_JSON}")
    print(f"generalization_report={GENERALIZATION_REPORT_JSON}")
    print(f"target_year={target_year}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
