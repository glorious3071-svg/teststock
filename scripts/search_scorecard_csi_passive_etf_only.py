#!/usr/bin/env python3
"""Search scorecard/CSI portfolios constrained to domestic passive ETFs only."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import statistics
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from scripts.backtest_scorecard_csi_dynamic_defense import month_end_shift, monthly_boundaries, shifted_boundary
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE, INITIAL_CAPITAL, TARGET_CAPITAL, max_drawdown
from scripts.backtest_scorecard_csi_quarterly_risk import TARGET_MDD

OUT_DIR = ROOT / "data" / "backtests"
START_YEAR = 2006
END_YEAR = 2025
EXECUTION_LAGS = [0, 1, 3, 5]
MONTH_PHASES = list(range(12))
PRICE_CACHE: dict[tuple[str, dt.date], float | None] = {}
SCORE_CACHE: dict[tuple[str, dt.date, int, int, int], float | None] = {}
PORTFOLIO_RETURN_CACHE: dict[tuple[tuple[str, ...], dt.date, dt.date], float] = {}
RANK_CACHE: dict[tuple[dt.date, str, int, int, int, int], list[str]] = {}
MONEY_ETF_WHITELIST = {
    "511880.SH": ("银华日利", "交易所货币 ETF"),
    "511990.SH": ("华宝添益", "交易所货币 ETF"),
}
OVERSEAS_CODE_PREFIXES = ("513", "520")
OVERSEAS_NAME_KEYWORDS = (
    "港股",
    "恒生",
    "纳指",
    "标普",
    "日经",
    "德国",
    "法国",
    "美国",
    "中概",
    "海外",
    "全球",
    "东南亚",
    "沙特",
)
OVERSEAS_INDEX_SUFFIXES = (".HI", ".OTH")


@dataclass(frozen=True)
class EtfMeta:
    code: str
    name: str
    index_code: str
    index_name: str
    list_date: dt.date | None
    category: str


@dataclass(frozen=True)
class EtfOnlyRule:
    name: str
    interval_months: int
    top_n: int
    momentum_months: int
    skip_recent_months: int
    min_history_months: int
    trend_months: int
    trend_lte: float
    drawdown_lte: float
    defense_top_n: int
    max_single_weight: float
    floor_pct: float = 0.0
    multiplier: float = 1.0
    max_risk_weight: float = 1.0
    min_risk_score: float = -999.0


def price_at(rows: list[tuple[dt.date, float]], boundary: dt.date, code: str | None = None) -> float | None:
    if code is not None:
        key = (code, boundary)
        if key in PRICE_CACHE:
            return PRICE_CACHE[key]
    i = bisect_right(rows, (boundary, math.inf)) - 1
    value = rows[i][1] if i >= 0 else None
    if code is not None:
        PRICE_CACHE[key] = value
    return value


def period_return(rows: list[tuple[dt.date, float]], start: dt.date, end: dt.date, code: str | None = None) -> float | None:
    start_px = price_at(rows, start, code)
    end_px = price_at(rows, end, code)
    if not start_px or not end_px or start_px <= 0:
        return None
    return end_px / start_px - 1.0


def classify_etf(code: str, name: str, index_name: str) -> str:
    text = f"{code} {name} {index_name}"
    if code in MONEY_ETF_WHITELIST or any(key in text for key in ["货币", "保证金", "现金"]):
        return "money"
    if code.startswith("511") or any(key in text for key in ["国债", "债ETF", "信用债", "公司债", "政金债", "城投债"]):
        return "bond"
    if any(key in text for key in ["黄金", "上海金", "金ETF"]):
        return "gold"
    return "equity"


def is_overseas_etf(code: str, name: str, index_name: str, index_code: str = "") -> bool:
    text = f"{code} {name} {index_name} {index_code}"
    return (
        code.startswith(OVERSEAS_CODE_PREFIXES)
        or any(key in text for key in OVERSEAS_NAME_KEYWORDS)
        or index_code.endswith(OVERSEAS_INDEX_SUFFIXES)
    )


def load_etf_universe(
    conn,
    min_rows: int,
    include_money_etf_defense: bool,
) -> tuple[dict[str, EtfMeta], dict[str, list[tuple[dt.date, float]]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date,
                   COUNT(f.trade_date), MIN(f.trade_date), MAX(f.trade_date)
            FROM passive_etf e
            JOIN fund_daily f ON f.ts_code=e.ts_code
            WHERE (e.etf_type IS NULL OR e.etf_type!='QDII')
              AND (e.is_enhanced IS NULL OR e.is_enhanced=0)
              AND e.ts_code NOT LIKE '%%.OF'
              AND e.ts_code NOT LIKE '513%%'
              AND e.ts_code NOT LIKE '520%%'
              AND COALESCE(e.extname, '') NOT REGEXP '港股|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|东南亚|沙特'
              AND COALESCE(e.index_name, '') NOT REGEXP '港股|恒生|纳指|标普|日经|德国|法国|美国|中概|海外|全球|东南亚|沙特'
              AND COALESCE(e.index_ts_code, '') NOT LIKE '%%.HI'
              AND COALESCE(e.index_ts_code, '') NOT LIKE '%%.OTH'
              AND f.close IS NOT NULL
            GROUP BY e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
            HAVING COUNT(f.trade_date) >= %s
            ORDER BY e.list_date, e.ts_code
            """,
            (min_rows,),
        )
        meta_rows = cur.fetchall()
        codes = [str(row[0]) for row in meta_rows]
        metas = {
            str(code): EtfMeta(
                code=str(code),
                name=str(name or code),
                index_code=str(index_code or ""),
                index_name=str(index_name or ""),
                list_date=list_date,
                category=classify_etf(str(code), str(name or ""), str(index_name or "")),
            )
            for code, name, index_code, index_name, list_date, *_ in meta_rows
        }
        if include_money_etf_defense:
            money_codes = list(MONEY_ETF_WHITELIST)
            placeholders = ",".join(["%s"] * len(money_codes))
            cur.execute(
                f"""
                SELECT ts_code, COUNT(*), MIN(trade_date), MAX(trade_date)
                FROM fund_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                GROUP BY ts_code
                HAVING COUNT(*) >= %s
                """,
                [*money_codes, min_rows],
            )
            for code, *_ in cur.fetchall():
                code = str(code)
                if code in metas:
                    continue
                name, index_name = MONEY_ETF_WHITELIST[code]
                metas[code] = EtfMeta(
                    code=code,
                    name=name,
                    index_code="",
                    index_name=index_name,
                    list_date=None,
                    category="money",
                )
                codes.append(code)
        series: dict[str, list[tuple[dt.date, float]]] = {code: [] for code in codes}
        for start in range(0, len(codes), 400):
            chunk = codes[start : start + 400]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, close
                FROM fund_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            for code, trade_date, close in cur.fetchall():
                series.setdefault(str(code), []).append((trade_date, float(close)))
        cur.execute(
            """
            SELECT trade_date, close
            FROM index_daily
            WHERE ts_code=%s AND close IS NOT NULL
            ORDER BY trade_date
            """,
            (CS300_CODE,),
        )
        series[CS300_CODE] = [(trade_date, float(close)) for trade_date, close in cur.fetchall()]
    return metas, series


def return_vol(rows: list[tuple[dt.date, float]], end: dt.date, months: int) -> float:
    start = month_end_shift(end, -months)
    vals = []
    prev_px = price_at(rows, start)
    if not prev_px:
        return 999.0
    for day, px in rows:
        if start < day <= end and prev_px > 0:
            vals.append(px / prev_px - 1.0)
            prev_px = px
    return statistics.pstdev(vals) if len(vals) >= 20 else 999.0


def score_etf(code: str, rows: list[tuple[dt.date, float]], snapshot: dt.date, rule: EtfOnlyRule) -> float | None:
    key = (code, snapshot, rule.momentum_months, rule.skip_recent_months, rule.min_history_months)
    if key in SCORE_CACHE:
        return SCORE_CACHE[key]
    end = month_end_shift(snapshot, -rule.skip_recent_months)
    start = month_end_shift(end, -rule.momentum_months)
    min_start = month_end_shift(snapshot, -rule.min_history_months)
    if price_at(rows, min_start, code) is None:
        SCORE_CACHE[key] = None
        return None
    mom = period_return(rows, start, end, code)
    short = period_return(rows, month_end_shift(end, -3), end, code)
    if mom is None or short is None:
        SCORE_CACHE[key] = None
        return None
    vol = return_vol(rows, end, 6)
    score = mom + 0.35 * short - 3.0 * min(vol, 0.08)
    SCORE_CACHE[key] = score
    return score


def choose_portfolio(
    metas: dict[str, EtfMeta],
    series: dict[str, list[tuple[dt.date, float]]],
    snapshot: dt.date,
    rule: EtfOnlyRule,
    defensive: bool,
    allow_cash_defense: bool = False,
) -> list[str]:
    if defensive and allow_cash_defense:
        return []
    rank_key = (
        snapshot,
        "def" if defensive else "risk",
        rule.top_n,
        rule.defense_top_n,
        rule.momentum_months,
        rule.min_history_months,
        int(rule.min_risk_score * 1000),
    )
    if rank_key in RANK_CACHE:
        ranked = RANK_CACHE[rank_key]
        return ranked[: (rule.defense_top_n if defensive else rule.top_n)]
    categories = {"money", "bond", "gold"} if defensive else {"equity"}
    scored = []
    for code, meta in metas.items():
        if meta.category not in categories:
            continue
        score = score_etf(code, series[code], snapshot, rule)
        if score is not None:
            scored.append((score, code))
    if defensive and not scored:
        # Before bond/gold ETFs existed, use the least volatile domestic equity ETF as a forced ETF-only fallback.
        fallback = []
        for code, meta in metas.items():
            if meta.category != "equity":
                continue
            if price_at(series[code], month_end_shift(snapshot, -rule.min_history_months), code) is None:
                continue
            fallback.append((return_vol(series[code], snapshot, 12), code))
        ranked = [code for _vol, code in sorted(fallback)]
        RANK_CACHE[rank_key] = ranked
        return ranked[: rule.defense_top_n]
    count = rule.defense_top_n if defensive else rule.top_n
    ranked = [code for _score, code in sorted(scored, reverse=True)]
    if not defensive and allow_cash_defense and scored:
        best_score = max(score for score, _code in scored)
        if best_score <= rule.min_risk_score:
            ranked = []
    RANK_CACHE[rank_key] = ranked
    return ranked[:count]


def portfolio_return(codes: list[str], series: dict[str, list[tuple[dt.date, float]]], start: dt.date, end: dt.date) -> float:
    if not codes:
        return 0.0
    key = (tuple(sorted(codes)), start, end)
    if key in PORTFOLIO_RETURN_CACHE:
        return PORTFOLIO_RETURN_CACHE[key]
    returns = [period_return(series[code], start, end, code) for code in codes]
    valid = [value for value in returns if value is not None]
    if not valid:
        PORTFOLIO_RETURN_CACHE[key] = 0.0
        return 0.0
    value = sum(valid) / len(valid)
    PORTFOLIO_RETURN_CACHE[key] = value
    return value


def build_rules(quick: bool, cash_focused: bool = False) -> list[EtfOnlyRule]:
    intervals = [1, 3, 12]
    top_ns = [1, 3, 5]
    momentum_months = [3, 6, 12]
    trend_values = [-0.03, -0.06, -0.10]
    drawdowns = [-0.04, -0.06, -0.08, -1.0]
    floors = [
        (0.88, 3.0),
        (0.90, 4.0),
        (0.92, 6.0),
    ]
    min_scores = [-999.0]
    if quick:
        intervals = [1, 3, 12]
        top_ns = [1, 3]
        momentum_months = [3, 6, 12]
        trend_values = [0.0, -0.03, -0.06]
        drawdowns = [-0.04, -0.06, -1.0]
        floors = [
            (0.88, 3.0),
            (0.90, 4.0),
            (0.92, 6.0),
            (0.95, 3.0),
            (0.97, 2.0),
            (0.99, 1.0),
        ]
        min_scores = [-999.0]
    if cash_focused:
        intervals = [1]
        top_ns = [1, 3]
        momentum_months = [1, 3, 6]
        trend_values = [-1.0, -0.03]
        drawdowns = [-1.0, -0.06]
        floors = [
            (0.92, 3.0),
            (0.95, 2.0),
        ]
        min_scores = [-0.05, 0.0, 0.05]
    rules = []
    for interval in intervals:
        for top_n in top_ns:
            for mom in momentum_months:
                for trend in trend_values:
                    for dd in drawdowns:
                        for min_score in min_scores:
                            suffix = "" if min_score <= -900 else f"_sg{int(min_score*100):+03d}".replace("+", "p").replace("-", "n")
                            name = (
                                f"etfonly_i{interval}_top{top_n}_m{mom}_tr{int(abs(trend)*100):02d}"
                                f"_dd{int(abs(dd)*100):02d}{suffix}"
                            )
                            rules.append(
                                EtfOnlyRule(
                                    name=name,
                                    interval_months=interval,
                                    top_n=top_n,
                                    momentum_months=mom,
                                    skip_recent_months=0,
                                    min_history_months=max(6, mom),
                                    trend_months=6,
                                    trend_lte=trend,
                                    drawdown_lte=dd,
                                    defense_top_n=1,
                                    max_single_weight=1.0,
                                    min_risk_score=min_score,
                                )
                            )
                            for floor, multiplier in floors:
                                rules.append(
                                    EtfOnlyRule(
                                        name=(
                                            f"etftipp_i{interval}_top{top_n}_m{mom}_tr{int(abs(trend)*100):02d}"
                                            f"_dd{int(abs(dd)*100):02d}{suffix}_f{int(floor*100)}_k{int(multiplier*10)}"
                                        ),
                                        interval_months=interval,
                                        top_n=top_n,
                                        momentum_months=mom,
                                        skip_recent_months=0,
                                        min_history_months=max(6, mom),
                                        trend_months=6,
                                        trend_lte=trend,
                                        drawdown_lte=dd,
                                        defense_top_n=1,
                                        max_single_weight=1.0,
                                        floor_pct=floor,
                                        multiplier=multiplier,
                                        max_risk_weight=1.0,
                                        min_risk_score=min_score,
                                    )
                                )
    return rules


def run_case(
    metas: dict[str, EtfMeta],
    series: dict[str, list[tuple[dt.date, float]]],
    trade_dates: list[dt.date],
    rule: EtfOnlyRule,
    phase: int,
    lag: int,
    allow_cash_defense: bool = False,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    peak = capital
    curve = [capital]
    current_codes: list[str] = []
    current_defense_codes: list[str] = []
    rows = []
    periods = monthly_boundaries(START_YEAR, END_YEAR, phase)
    for idx, (start_snapshot, end_snapshot) in enumerate(periods):
        start_exec = shifted_boundary(trade_dates, start_snapshot, lag)
        end_exec = shifted_boundary(trade_dates, end_snapshot, lag)
        drawdown = capital / peak - 1.0
        trend = period_return(series[CS300_CODE], month_end_shift(start_snapshot, -rule.trend_months), start_snapshot) or 0.0
        defensive = trend <= rule.trend_lte or drawdown <= rule.drawdown_lte
        if idx % rule.interval_months == 0 or not current_codes:
            current_codes = choose_portfolio(metas, series, start_snapshot, rule, defensive, allow_cash_defense)
            current_defense_codes = choose_portfolio(metas, series, start_snapshot, rule, True, allow_cash_defense)
        if rule.floor_pct > 0:
            floor = peak * rule.floor_pct
            cushion = max(0.0, capital - floor)
            risk_weight = min(rule.max_risk_weight, max(0.0, rule.multiplier * cushion / max(capital, 1.0)))
            if defensive:
                risk_weight = min(risk_weight, 0.25)
            risky_ret = portfolio_return(current_codes, series, start_exec, end_exec)
            defense_ret = portfolio_return(current_defense_codes, series, start_exec, end_exec)
            ret = risk_weight * risky_ret + (1.0 - risk_weight) * defense_ret
        else:
            ret = portfolio_return(current_codes, series, start_exec, end_exec)
        capital *= 1.0 + ret
        if capital <= 0:
            capital = 1.0
        peak = max(peak, capital)
        curve.append(capital)
        rows.append(
            {
                "start_snapshot": start_snapshot.isoformat(),
                "end_snapshot": end_snapshot.isoformat(),
                "start_exec": start_exec.isoformat(),
                "end_exec": end_exec.isoformat(),
                "capital": capital,
                "period_return": ret,
                "drawdown": capital / peak - 1.0,
                "defensive": defensive,
                "holdings": ",".join(current_codes) if current_codes else "CASH",
            }
        )
    mdd = max_drawdown(curve)
    return {
        "rule": rule.name,
        "phase_month_offset": phase,
        "execution_lag_days": lag,
        "final_capital": capital,
        "final_capital_wan": capital / 10_000.0,
        "annualized_return": (capital / INITIAL_CAPITAL) ** (1.0 / (END_YEAR - START_YEAR + 1)) - 1.0,
        "max_drawdown": mdd,
        "target_met": capital >= TARGET_CAPITAL and mdd >= TARGET_MDD,
        "rows": rows,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(cases),
        "pass_count": sum(1 for item in cases if item["target_met"]),
        "min_final_capital_wan": min(item["final_capital_wan"] for item in cases),
        "median_final_capital_wan": statistics.median(item["final_capital_wan"] for item in cases),
        "worst_max_drawdown": min(item["max_drawdown"] for item in cases),
        "median_max_drawdown": statistics.median(item["max_drawdown"] for item in cases),
        "min_annualized_return": min(item["annualized_return"] for item in cases),
    }


def drop_monthly_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped = []
    for item in results:
        cases = []
        for case in item["cases"]:
            cases.append({key: value for key, value in case.items() if key != "rows"})
        stripped.append({"rule": item["rule"], "cases": cases, "summary": item["summary"]})
    return stripped


def main() -> int:
    parser = argparse.ArgumentParser(description="Search domestic passive ETF-only scorecard/CSI portfolios.")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--cash-focused", action="store_true", help="Use a smaller rule grid focused on cash-defense risk controls.")
    parser.add_argument("--summary-only", action="store_true", help="Do not write monthly row details to the JSON report.")
    parser.add_argument("--min-rows", type=int, default=120)
    parser.add_argument("--output-prefix")
    parser.add_argument(
        "--include-money-etf-defense",
        action="store_true",
        help="Include explicitly whitelisted exchange-traded money ETFs as defensive ETF holdings.",
    )
    parser.add_argument(
        "--allow-cash-defense",
        action="store_true",
        help="Allow uninvested cash with 0 return as the defensive state; ETF holdings remain domestic passive ETFs only.",
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        metas, series = load_etf_universe(conn, args.min_rows, args.include_money_etf_defense)
    finally:
        conn.close()
    trade_dates = [day for day, _px in series[CS300_CODE]]
    rules = build_rules(args.quick, args.cash_focused)
    results = []
    for rule in rules:
        cases = [
            run_case(metas, series, trade_dates, rule, phase, lag)
            if not args.allow_cash_defense
            else run_case(metas, series, trade_dates, rule, phase, lag, allow_cash_defense=True)
            for phase in MONTH_PHASES
            for lag in EXECUTION_LAGS
        ]
        results.append({"rule": asdict(rule), "cases": cases, "summary": summarize(cases)})
    results.sort(
        key=lambda item: (
            item["summary"]["pass_count"],
            item["summary"]["worst_max_drawdown"],
            item["summary"]["min_final_capital_wan"],
        ),
        reverse=True,
    )
    prefix = Path(args.output_prefix) if args.output_prefix else OUT_DIR / "scorecard_csi_passive_etf_only"
    if not prefix.is_absolute():
        prefix = ROOT / prefix
    json_path = Path(f"{prefix}_report.json")
    csv_path = Path(f"{prefix}_search.csv")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "objective": (
            "Domestic ETF-only search; excludes overseas assets, options, futures, crypto, "
            "and non-ETF instruments. Money ETF defense is included only when explicitly flagged."
            " Cash defense means no position, not a non-ETF investment, and is included only when explicitly flagged."
        ),
        "initial_capital": INITIAL_CAPITAL,
        "target_capital": TARGET_CAPITAL,
        "target_mdd": TARGET_MDD,
        "universe": {
            "etf_count": len(metas),
            "equity_count": sum(1 for meta in metas.values() if meta.category == "equity"),
            "bond_count": sum(1 for meta in metas.values() if meta.category == "bond"),
            "gold_count": sum(1 for meta in metas.values() if meta.category == "gold"),
            "money_count": sum(1 for meta in metas.values() if meta.category == "money"),
            "include_money_etf_defense": args.include_money_etf_defense,
            "allow_cash_defense": args.allow_cash_defense,
        },
        "results": drop_monthly_rows(results) if args.summary_only else results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["name", "interval_months", "top_n", "momentum_months", "trend_lte", "drawdown_lte", *list(results[0]["summary"].keys())]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in results:
            row = {**item["rule"], **item["summary"]}
            writer.writerow({"name": row.pop("name"), **{field: row.get(field) for field in fields if field != "name"}})
    for item in results[:20]:
        s = item["summary"]
        print(
            f"{item['rule']['name']:<42} pass={s['pass_count']:>2}/{s['count']} "
            f"min={s['min_final_capital_wan']:8.1f}w worst_mdd={s['worst_max_drawdown']*100:6.1f}% "
            f"median={s['median_final_capital_wan']:8.1f}w"
        )
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")
    best = results[0]["summary"]
    return 0 if best["pass_count"] == best["count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
