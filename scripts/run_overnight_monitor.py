#!/usr/bin/env python3
"""Overnight news pipeline monitor — runs until Beijing 04:00."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "logs" / "overnight-monitor.log"
BEIJING = timezone(timedelta(hours=8))
END_HOUR = 4  # 04:00 Beijing


def bj_now() -> datetime:
    return datetime.now(BEIJING)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{bj_now():%Y-%m-%d %H:%M:%S}] {msg}\n"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="")


def run(cmd: list[str], timeout: int = 300) -> int:
    log(f"RUN {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        tail = (r.stdout or "")[-800:] + (r.stderr or "")[-400:]
        log(f"EXIT {r.returncode} {tail[-300:]}")
        return r.returncode
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {' '.join(cmd)}")
        return 124


def until_end() -> datetime:
    now = bj_now()
    end = now.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)
    if now >= end:
        end += timedelta(days=1)
    return end


def main() -> int:
    end = until_end()
    log(f"Monitor start, end at {end:%Y-%m-%d %H:%M} Beijing")
    cycle = 0
    while bj_now() < end:
        cycle += 1
        log(f"=== cycle {cycle} ===")
        run([sys.executable, "scripts/run_daily_news.py", "--tier", "flash"], timeout=120)
        if cycle == 1 or cycle % 4 == 0:
            run([sys.executable, "scripts/run_daily_news.py", "--tier", "daily"], timeout=600)
        if cycle % 2 == 0:
            run([sys.executable, "scripts/run_news_extraction.py", "--limit", "5"], timeout=600)
        run([sys.executable, "scripts/verify_news_pipeline.py"], timeout=600)
        sleep_sec = min(1800, max(60, (end - bj_now()).total_seconds()))
        if sleep_sec <= 60:
            break
        log(f"sleep {int(sleep_sec)}s")
        time.sleep(min(sleep_sec, 1800))
    log("=== final verification ===")
    rc = run([sys.executable, "scripts/verify_news_pipeline.py"], timeout=900)
    log(f"Monitor finished rc={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
