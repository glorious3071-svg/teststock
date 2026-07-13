#!/usr/bin/env python3
"""Overnight loop: incremental process → grid search → backtest until 04:00 CST.

Usage:
  python scripts/overnight_optimize.py
  python scripts/overnight_optimize.py --until 04:00
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOG = ROOT / "data" / "logs" / "overnight_optimize.log"
REPORT = ROOT / "data" / "backtests" / "overnight_final_report.json"
BEST = ROOT / "data" / "backtests" / "overnight_best_config.json"

CST = ZoneInfo("Asia/Shanghai")


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def log(msg: str) -> None:
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def db_snapshot() -> dict:
    from db.connection import get_connection
    conn = get_connection()
    cur = conn.cursor()
    snap = {}
    for key, sql in {
        "articles": "SELECT COUNT(*) FROM news_article",
        "extractions": "SELECT COUNT(*) FROM news_extraction",
        "extractions_event": "SELECT COUNT(*) FROM news_extraction WHERE event_id IS NOT NULL",
        "daily": "SELECT COUNT(*) FROM theme_news_daily",
        "daily_min": "SELECT MIN(signal_date) FROM theme_news_daily",
        "daily_max": "SELECT MAX(signal_date) FROM theme_news_daily",
    }.items():
        cur.execute(sql)
        snap[key] = cur.fetchone()[0]
    conn.close()
    return snap


def incremental_process() -> dict:
    """Process new extractions / re-aggregate if data grew."""
    from db.connection import get_connection
    from news.processing.batch import (
        ensure_all_schema,
        extract_pending_events,
        link_existing_extractions,
    )
    from news.processing.daily import aggregate_daily, rollup_weekly
    from news.processing.cluster import cluster_stream_all, cluster_articles
    from news.retrieval.prefilter import backfill_prefilter

    conn = get_connection()
    ensure_all_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM news_article a LEFT JOIN news_event_member m ON m.article_id=a.id WHERE m.article_id IS NULL"
    )
    unclustered = cur.fetchone()[0]
    if unclustered > 0:
        if unclustered > 2000:
            # Fast path: one chronological pass only
            st = cluster_stream_all(conn)
            cluster_info = {"created": st.created, "updated": st.updated, "scanned": st.scanned}
        else:
            st = cluster_articles(conn, process_date=date.today())
            cluster_info = {"created": st.created, "updated": st.updated, "scanned": st.scanned}
    else:
        cluster_info = {"skipped": True}
    link_existing_extractions(conn)
    backfill_prefilter(conn, limit=8000)
    ext_total = {"extracted": 0, "skipped": 0}
    for _ in range(20):
        b = extract_pending_events(conn, limit=1000, mock=True)
        ext_total["extracted"] += b["extracted"]
        ext_total["skipped"] += b["skipped_prefilter"]
        if b["extracted"] == 0:
            break
    cur = conn.cursor()
    cur.execute("SELECT MIN(DATE(COALESCE(pub_time,created_at))), MAX(DATE(COALESCE(pub_time,created_at))) FROM news_article")
    d0, d1 = cur.fetchone()
    daily_n = 0
    if d0 and d1:
        d = d0
        while d <= d1:
            if aggregate_daily(conn, d):
                daily_n += 1
            d += timedelta(days=1)
        w = d0
        weekly_n = 0
        while w <= d1:
            if rollup_weekly(conn, w):
                weekly_n += 1
            w += timedelta(days=7)
    else:
        weekly_n = 0
    for y in range(2020, date.today().year + 1):
        subprocess.call(
            [sys.executable, "scripts/aggregate_theme_news_signals.py", "--year", str(y)],
            cwd=ROOT,
        )
    conn.close()
    return {"cluster": cluster_info, "extract": ext_total, "daily_days": daily_n, "weekly": weekly_n}


def grid_search() -> dict:
    from db.connection import get_connection
    from csi.tuning import RankConfig, eval_config_on_years
    from scripts.rank_annual_csi import load_news, load_prices, load_signals, load_theme_map, load_valuations
    from scripts.validate_csi_rank import forward_return, spearman

    conn = get_connection()
    years = list(range(2019, date.today().year + 1))
    candidates: list[RankConfig] = []

    # Baseline reference
    candidates.append(RankConfig(news_weight=0.0, use_heat=False, use_scorecard=False))
    candidates.append(RankConfig(news_weight=0.15, use_heat=False, use_scorecard=False))

    for nw in (0.10, 0.15, 0.20, 0.25):
        for hw in (0.0, 0.10, 0.20):
            for sw in (0.0, 0.08, 0.12):
                for dedup in (False, True):
                    candidates.append(RankConfig(
                        news_weight=nw,
                        heat_weight=hw,
                        scorecard_weight=sw,
                        use_heat=hw > 0,
                        use_scorecard=sw > 0,
                        use_corr_dedup=dedup,
                        use_duration=True,
                    ))

    best = None
    results = []
    for i, cfg in enumerate(candidates):
        r = eval_config_on_years(
            conn, cfg, years,
            load_signals=load_signals,
            load_news=load_news,
            load_theme_map=load_theme_map,
            load_prices=load_prices,
            load_valuations=load_valuations,
            forward_return=forward_return,
            spearman=spearman,
        )
        results.append(r)
        if best is None or r["score"] > best["score"]:
            best = r
        if (i + 1) % 10 == 0:
            log(f"  grid {i+1}/{len(candidates)} best_score={best['score']:.4f}")

    results.sort(key=lambda x: -x["score"])
    conn.close()
    BEST.parent.mkdir(parents=True, exist_ok=True)
    BEST.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"best": best, "top5": results[:5], "tested": len(candidates)}


def build_final_report(best: dict, snap: dict) -> dict:
    from db.connection import get_connection
    from csi.tuning import RankConfig, rank_with_config
    from scripts.rank_annual_csi import load_news, load_prices, load_signals, load_theme_map, load_valuations
    from scripts.validate_csi_rank import forward_return, spearman
    import statistics

    conn = get_connection()
    cfg = RankConfig(**best["config"])
    baseline_cfg = RankConfig(news_weight=0.0, use_heat=False, use_scorecard=False)
    legacy_news = RankConfig(news_weight=0.15, use_heat=False, use_scorecard=False)
    legacy_full = RankConfig(
        news_weight=0.15, heat_weight=0.20, scorecard_weight=0.10,
        use_heat=True, use_scorecard=True, use_corr_dedup=True,
    )

    comparison = {}
    for label, c in [
        ("baseline", baseline_cfg),
        ("legacy_news", legacy_news),
        ("legacy_full", legacy_full),
        ("optimized", cfg),
    ]:
        year_stats = []
        for year in range(2019, date.today().year + 1):
            news = load_news(conn, year)
            if label != "baseline" and not news:
                continue
            rows = rank_with_config(
                conn, year, c,
                load_signals=load_signals, load_news=load_news,
                load_theme_map=load_theme_map, load_prices=load_prices,
                load_valuations=load_valuations,
            )
            if not rows:
                continue
            start = date(year, 1, 5)
            end = date(year, 12, 31) if year < date.today().year else date.today()
            pairs = []
            for row in rows:
                ret = forward_return(conn, row["ts_code"], start, end)
                if ret is not None:
                    pairs.append((row["final_score"], ret))
            if len(pairs) < 10:
                continue
            scores, rets = zip(*pairs)
            rho = spearman(list(scores), list(rets))
            sp = sorted(pairs, key=lambda p: -p[0])
            k = min(10, len(sp) // 4)
            top_avg = statistics.mean([r for _, r in sp[:k]])
            bench = forward_return(conn, "000300.SH", start, end)
            year_stats.append({
                "year": year,
                "rho": rho,
                "top_avg": top_avg,
                "spread": top_avg - statistics.mean([r for _, r in sp[-k:]]),
                "excess": top_avg - bench if bench else None,
                "top5": [rows[i]["ts_code"] for i in range(min(5, len(rows)))],
            })
        comparison[label] = year_stats

    conn.close()
    report = {
        "generated_at": datetime.now(CST).isoformat(),
        "db_snapshot": snap,
        "best_config": best,
        "comparison": comparison,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(_json_safe(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_until(s: str) -> datetime:
    today = datetime.now(CST).date()
    h, m = map(int, s.split(":"))
    target = datetime.combine(today, datetime.min.time().replace(hour=h, minute=m), tzinfo=CST)
    if target <= datetime.now(CST):
        target += timedelta(days=1)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--until", default="04:00", help="Stop time HH:MM CST")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between cycles")
    args = parser.parse_args()

    deadline = parse_until(args.until)
    log(f"Overnight optimize until {deadline.strftime('%Y-%m-%d %H:%M %Z')}")
    last_snap = db_snapshot()
    best_result = None
    first_cycle = True

    while datetime.now(CST) < deadline:
        snap = db_snapshot()
        changed = snap != last_snap or first_cycle
        if changed:
            log(f"Data changed: {snap}")
            log("Incremental process...")
            proc = incremental_process()
            log(f"  process: {proc}")
            last_snap = snap
            first_cycle = False

        log("Grid search...")
        gs = grid_search()
        best_result = gs["best"]
        log(
            f"  best score={best_result['score']:.4f} "
            f"rho={best_result.get('mean_rho')} spread={best_result.get('mean_spread')} "
            f"excess={best_result.get('mean_excess')} ytd_excess={best_result.get('mean_ytd_excess')}"
        )

        remaining = (deadline - datetime.now(CST)).total_seconds()
        if remaining <= 0:
            break
        sleep_s = min(args.interval, max(30, remaining - 10))
        log(f"Sleep {int(sleep_s)}s (remaining {int(remaining/60)} min)")
        time.sleep(sleep_s)

    log("Building final report...")
    report = build_final_report(best_result or json.loads(BEST.read_text()), db_snapshot())
    log(f"Final report → {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
