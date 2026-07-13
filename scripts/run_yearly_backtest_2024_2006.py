#!/usr/bin/env python3
"""Year-by-year CSI + macro backtest pipeline, 2024 → 2006.

For each apply_year:
  1. Migrate CCTV news for H2 window (if available)
  2. Mock-extract pending articles in window
  3. Aggregate theme_news_signals (or verified supplement)
  4. Rank CSI indices (--save --full) when price data exists
  5. Evaluate macro scorecard + forward returns
  6. Append results to checkpoint JSON

Usage:
  python scripts/run_yearly_backtest_2024_2006.py
  python scripts/run_yearly_backtest_2024_2006.py --from 2024 --to 2006
  python scripts/run_yearly_backtest_2024_2006.py --deadline "2026-06-30 08:00"
  python scripts/run_yearly_backtest_2024_2006.py --resume
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql

from backtest.scorecard import evaluate_scorecard
from backtest.scorecard_adapter import AdapterOptions, load_scorecard_inputs
from collectors.dedup import normalize_text
from collectors.models import RawArticle
from collectors.storage import insert_articles
from csi.ranking import news_window, year_as_of
from db.connection import get_connection

VERIFIED_PATH = ROOT / "data" / "historical" / "verified_theme_news_signals.json"
OUT_PATH = ROOT / "data" / "backtests" / "yearly_backtest_2024_2006.json"
LOG_PATH = ROOT / "data" / "backtests" / "yearly_backtest_2024_2006.log"
CSI_MIN_YEAR = 2020  # index_daily .CSI starts ~2019 H2


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_verified() -> dict:
    if not VERIFIED_PATH.exists():
        return {}
    return json.loads(VERIFIED_PATH.read_text(encoding="utf-8"))


def migrate_cctv_window(conn, w_start: date, w_end: date) -> dict:
    stats = {"read": 0, "inserted": 0}
    if w_start.year < 2016:
        return {**stats, "skipped": "cctv_before_2016"}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT news_date, title, content FROM cctv_news_daily
            WHERE news_date >= %s AND news_date <= %s
            ORDER BY news_date, title
            """,
            (w_start, w_end),
        )
        rows = cur.fetchall()
    articles: list[RawArticle] = []
    for news_date, title, content in rows:
        stats["read"] += 1
        title = normalize_text(str(title or ""))
        if len(title) < 4:
            continue
        pub_time = datetime.combine(news_date, dt_time(19, 0))
        body = normalize_text(str(content or "")) or None
        articles.append(
            RawArticle(
                source="cctv",
                category="policy",
                title=title[:490],
                body_text=body,
                pub_time=pub_time,
                extra_json={"news_date": news_date.isoformat(), "migrated_from": "cctv_news_daily"},
            )
        )
    if articles:
        r = insert_articles(conn, articles)
        stats["inserted"] = r.inserted
        stats["dup"] = r.skipped_dup
    return stats


def extract_pending(conn, w_start: date, w_end: date, *, limit: int = 500) -> int:
    """Mock-extract articles in window without extraction rows."""
    until = datetime.combine(w_end + timedelta(days=1), dt_time(0, 0))
    since = datetime.combine(w_start, dt_time(0, 0))
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_news_extraction.py"),
        "--since", since.strftime("%Y-%m-%d"),
        "--until", until.strftime("%Y-%m-%d"),
        "--limit", str(limit),
        "--mock",
        "--sleep", "0.05",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    if proc.returncode not in (0, 1):
        log(f"  extract warning: {proc.stderr[-300:]}")
    return proc.returncode


def count_theme_signals(conn, apply_year: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM theme_news_signals WHERE apply_year=%s", (apply_year,))
        return int(cur.fetchone()[0])


def apply_verified_supplement(conn, apply_year: int, verified: dict) -> dict:
    entry = verified.get(str(apply_year))
    if not entry:
        return {"applied": 0, "reason": "no_verified_entry"}
    w_start, w_end = news_window(apply_year)
    as_of = year_as_of(apply_year)
    themes = entry.get("themes") or {}
    applied = 0
    with conn.cursor() as cur:
        for theme, meta in themes.items():
            net = float(meta.get("net_score", 0))
            bull = max(net, 0)
            bear = max(-net, 0)
            cur.execute(
                """
                INSERT INTO theme_news_signals
                    (apply_year, as_of_date, window_start, window_end, theme,
                     net_score, bull_score, bear_score, article_count,
                     event_count, mention_count, source_diversity,
                     avg_magnitude, avg_confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,0,0,2.0,0.85)
                ON DUPLICATE KEY UPDATE
                    net_score=IF(article_count=0, VALUES(net_score), net_score),
                    bull_score=IF(article_count=0, VALUES(bull_score), bull_score),
                    bear_score=IF(article_count=0, VALUES(bear_score), bear_score),
                    avg_confidence=IF(article_count=0, VALUES(avg_confidence), avg_confidence),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (apply_year, as_of, w_start, w_end, theme, net, bull, bear),
            )
            applied += 1
    conn.commit()
    return {
        "applied": applied,
        "evidence": entry.get("evidence", []),
        "source": "verified_historical",
    }


def aggregate_news(conn, apply_year: int) -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "aggregate_theme_news_signals.py"),
        "--year", str(apply_year),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
    n = count_theme_signals(conn, apply_year)
    return {"exit": proc.returncode, "themes": n, "stdout_tail": (proc.stdout or "")[-400:]}


def rank_csi(conn, apply_year: int) -> dict:
    if apply_year < CSI_MIN_YEAR:
        return {"skipped": True, "reason": f"CSI prices from {CSI_MIN_YEAR}"}
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "rank_annual_csi.py"),
        "--year", str(apply_year),
        "--top", "30",
        "--save",
        "--full",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM csi_annual_recommendation WHERE apply_year=%s",
            (apply_year,),
        )
        n = int(cur.fetchone()[0])
    return {"exit": proc.returncode, "recommendations": n, "stdout_tail": (proc.stdout or "")[-400:]}


def forward_return(conn, ts_code: str, start: date, end: date) -> float | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT trade_date, close FROM index_daily
            WHERE ts_code=%s AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date
            """,
            (ts_code, start, end),
        )
        rows = [(d, float(c)) for d, c in cur.fetchall() if c]
    if len(rows) < 2:
        return None
    s, e = rows[0][1], rows[-1][1]
    return (e - s) / s if s > 0 else None


def validate_csi_year(conn, apply_year: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts_code, final_score FROM csi_annual_recommendation
            WHERE apply_year=%s AND ts_code LIKE '%%.CSI'
            ORDER BY rank_position LIMIT 50
            """,
            (apply_year,),
        )
        ranked = cur.fetchall()
    if len(ranked) < 10:
        return None
    start = date(apply_year, 1, 5)
    end = date(apply_year, 12, 31)
    pairs = []
    for ts, score in ranked:
        ret = forward_return(conn, ts, start, end)
        if ret is not None:
            pairs.append((float(score), ret))
    if len(pairs) < 10:
        return {"n": len(pairs), "insufficient": True}
    pairs.sort(key=lambda p: -p[0])
    k = min(10, len(pairs) // 4)
    top_avg = sum(r for _, r in pairs[:k]) / k
    bot_avg = sum(r for _, r in pairs[-k:]) / k
    bench = forward_return(conn, "000300.SH", start, end)
    return {
        "n": len(pairs),
        "top_k": k,
        "top_avg_pct": round(top_avg * 100, 2),
        "bot_avg_pct": round(bot_avg * 100, 2),
        "spread_pct": round((top_avg - bot_avg) * 100, 2),
        "bench_pct": round(bench * 100, 2) if bench else None,
        "excess_top_pct": round((top_avg - bench) * 100, 2) if bench else None,
    }


def macro_scorecard_year(conn, apply_year: int) -> dict:
    snap = date(apply_year - 1, 12, 31)
    try:
        inp = load_scorecard_inputs(snap, options=AdapterOptions(), conn=conn)
        r = evaluate_scorecard(apply_year, inp)
        start = date(apply_year, 1, 5)
        end = date(apply_year, 12, 31)
        cs_ret = forward_return(conn, "000300.SH", start, end)
        top_rules = [(it.name, it.score) for it in sorted(r.items, key=lambda x: -abs(x.score))[:5]]
        return {
            "score": r.total_score,
            "band": r.band,
            "target_equity_pct": r.target_equity_pct,
            "cs300_return_pct": round(cs_ret * 100, 2) if cs_ret else None,
            "top_rules": top_rules,
        }
    except Exception as exc:
        return {"error": str(exc)}


def process_year(conn, apply_year: int, verified: dict, *, skip_news: bool = False) -> dict:
    w_start, w_end = news_window(apply_year)
    log(f"=== apply_year={apply_year} window={w_start}..{w_end} ===")
    result: dict = {"apply_year": apply_year, "window": [str(w_start), str(w_end)]}

    if not skip_news:
        result["cctv_migrate"] = migrate_cctv_window(conn, w_start, w_end)
        log(f"  cctv: {result['cctv_migrate']}")

        log("  extracting pending (mock)...")
        extract_pending(conn, w_start, w_end, limit=800)

        result["aggregate"] = aggregate_news(conn, apply_year)
        log(f"  aggregate themes={result['aggregate']['themes']}")

        if result["aggregate"]["themes"] < 3:
            result["verified_supplement"] = apply_verified_supplement(conn, apply_year, verified)
            log(f"  verified supplement: {result['verified_supplement']}")
            result["aggregate"]["themes_after_supplement"] = count_theme_signals(conn, apply_year)
        else:
            result["verified_supplement"] = {"applied": 0, "reason": "aggregate_sufficient"}
    else:
        result["news_skipped"] = True

    result["csi_rank"] = rank_csi(conn, apply_year)
    log(f"  csi rank: {result['csi_rank']}")

    result["csi_validation"] = validate_csi_year(conn, apply_year)
    result["macro_scorecard"] = macro_scorecard_year(conn, apply_year)
    log(f"  macro score={result['macro_scorecard'].get('score')} cs300={result['macro_scorecard'].get('cs300_return_pct')}%")

    if result.get("csi_validation"):
        cv = result["csi_validation"]
        log(f"  csi validation spread={cv.get('spread_pct')}% excess={cv.get('excess_top_pct')}%")

    return result


def load_checkpoint() -> dict:
    if OUT_PATH.exists():
        return json.loads(OUT_PATH.read_text(encoding="utf-8"))
    return {"years": {}, "runs": [], "started_at": datetime.now().isoformat()}


def save_checkpoint(data: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat()
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize(data: dict) -> str:
    years = sorted(data.get("years", {}).keys(), reverse=True)
    lines = ["\n=== SUMMARY ==="]
    lines.append(f"{'Year':<6} {'Themes':>7} {'CSI#':>5} {'Spread%':>8} {'Macro':>6} {'CS300%':>8}")
    lines.append("-" * 50)
    spreads, excesses = [], []
    for y in years:
        r = data["years"][y]
        themes = r.get("aggregate", {}).get("themes_after_supplement") or r.get("aggregate", {}).get("themes", 0)
        csi_n = r.get("csi_rank", {}).get("recommendations", 0)
        cv = r.get("csi_validation") or {}
        spread = cv.get("spread_pct")
        excess = cv.get("excess_top_pct")
        macro = r.get("macro_scorecard", {}).get("score")
        cs = r.get("macro_scorecard", {}).get("cs300_return_pct")
        if spread is not None:
            spreads.append(spread)
        if excess is not None:
            excesses.append(excess)
        lines.append(
            f"{y:<6} {themes:>7} {csi_n:>5} "
            f"{spread if spread is not None else 'N/A':>8} "
            f"{macro if macro is not None else 'N/A':>6} "
            f"{cs if cs is not None else 'N/A':>8}"
        )
    if spreads:
        lines.append(f"\nMean CSI top-bottom spread: {sum(spreads)/len(spreads):.1f}% (n={len(spreads)})")
    if excesses:
        lines.append(f"Mean CSI top excess vs HS300: {sum(excesses)/len(excesses):.1f}% (n={len(excesses)})")
    return "\n".join(lines)


def try_real_llm_batch(conn, limit: int = 50) -> int:
    """Try real LLM extraction on oldest pending H2-window articles."""
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_news_extraction.py"),
        "--since", "2006-07-01",
        "--until", "2026-01-01",
        "--limit", str(limit),
        "--sleep", "1.0",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=7200)
    tail = (proc.stdout or "")[-200:]
    log(f"  real LLM batch: exit={proc.returncode} {tail}")
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="y_from", type=int, default=2024)
    parser.add_argument("--to", dest="y_to", type=int, default=2006)
    parser.add_argument("--deadline", default="2026-06-30 08:00")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep-between", type=float, default=30.0, help="Seconds between full passes")
    args = parser.parse_args()

    deadline = datetime.strptime(args.deadline, "%Y-%m-%d %H:%M")
    years = list(range(args.y_from, args.y_to - 1, -1))
    verified = load_verified()
    checkpoint = load_checkpoint()
    conn = get_connection(apply_schema=True)

    log(f"START years={years[0]}→{years[-1]} deadline={deadline}")

    pass_no = 0
    years_done = set(checkpoint.get("years", {}).keys())

    # Pass 1: full pipeline for each year (2024 → 2006)
    pass_no = 1
    log(f"--- Pass {pass_no}: full pipeline ---")
    for apply_year in years:
        key = str(apply_year)
        if args.resume and key in years_done:
            log(f"  skip {apply_year} (resume)")
            continue
        try:
            result = process_year(conn, apply_year, verified)
            checkpoint.setdefault("years", {})[key] = result
            checkpoint.setdefault("runs", []).append({
                "pass": pass_no, "apply_year": apply_year, "at": datetime.now().isoformat(),
            })
            save_checkpoint(checkpoint)
        except Exception:
            log(f"  ERROR {apply_year}: {traceback.format_exc()}")

    summary = summarize(checkpoint)
    log(summary)

    # Until deadline: retry real LLM extraction + re-aggregate CSI years
    pass_no = 2
    while datetime.now() < deadline:
        remaining = (deadline - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        log(f"--- Pass {pass_no}: real LLM retry (remaining {remaining/3600:.1f}h) ---")
        rc = try_real_llm_batch(conn, limit=30)
        if rc == 0:
            for apply_year in [y for y in years if y >= CSI_MIN_YEAR]:
                aggregate_news(conn, apply_year)
                rank_csi(conn, apply_year)
                key = str(apply_year)
                if key in checkpoint.get("years", {}):
                    checkpoint["years"][key]["csi_validation"] = validate_csi_year(conn, apply_year)
            save_checkpoint(checkpoint)
        time.sleep(min(args.sleep_between, remaining))
        pass_no += 1

    # Final backtest modes for CSI years
    if CSI_MIN_YEAR <= args.y_from:
        log("Running backtest_csi_modes...")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "backtest_csi_modes.py"),
             "--from", str(max(args.y_to, CSI_MIN_YEAR)), "--to", str(args.y_from)],
            cwd=ROOT, capture_output=True, text=True, timeout=1800,
        )
        checkpoint["csi_modes_output"] = proc.stdout
        save_checkpoint(checkpoint)
        log((proc.stdout or "")[-1500:])

    conn.close()
    log(f"DONE. Results: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
