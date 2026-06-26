"""Sector-level quantitative signals for annual direction.

Computes per-sector metrics from SW L1 index data:
  - momentum_1y: 1-year return of SW L1 index ending at as_of_date
  - pb_now: current PB (latest on or before as_of_date)
  - pb_pct_5y: PB percentile vs 5-year lookback (0=cheapest, 1=most expensive)
  - pb_zscore: PB z-score vs 5-year lookback

Data sources (all local DB):
  - index_daily (ts_code LIKE '8010xx.SI')  → momentum
  - index_dailybasic                          → PB valuation
  - data/sw_industry_classify.csv            → L1 sector names
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SW_CSV = Path(__file__).resolve().parents[2] / "data" / "sw_industry_classify.csv"
PB_HISTORY_YEARS = 5


@dataclass
class SectorSignal:
    ts_code: str
    name: str
    momentum_1y: float | None = None
    pb_now: float | None = None
    pb_pct_5y: float | None = None
    pb_zscore: float | None = None
    signal_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code,
            "name": self.name,
            "momentum_1y_pct": round(self.momentum_1y * 100, 2) if self.momentum_1y is not None else None,
            "pb_now": round(self.pb_now, 2) if self.pb_now is not None else None,
            "pb_pct_5y": round(self.pb_pct_5y * 100, 1) if self.pb_pct_5y is not None else None,
            "pb_zscore": round(self.pb_zscore, 2) if self.pb_zscore is not None else None,
            "signal": self.signal_label,
        }


def _load_sw_l1() -> list[tuple[str, str]]:
    """Return [(ts_code, name)] for SW L1 sectors."""
    if not SW_CSV.exists():
        return []
    return [
        (r["index_code"], r["industry_name"])
        for r in csv.DictReader(open(SW_CSV))
        if r.get("level") == "L1"
    ]


def _close_on_or_before(cur, ts_code: str, target: str) -> float | None:
    cur.execute(
        "SELECT close FROM index_daily WHERE ts_code = %s AND trade_date <= %s "
        "ORDER BY trade_date DESC LIMIT 1",
        (ts_code, target),
    )
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _pb_on_or_before(cur, ts_code: str, target: str) -> float | None:
    cur.execute(
        "SELECT pb FROM index_dailybasic WHERE ts_code = %s AND trade_date <= %s "
        "ORDER BY trade_date DESC LIMIT 1",
        (ts_code, target),
    )
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _pb_history(cur, ts_code: str, start: str, end: str) -> list[float]:
    cur.execute(
        "SELECT pb FROM index_dailybasic WHERE ts_code = %s "
        "AND trade_date BETWEEN %s AND %s AND pb IS NOT NULL",
        (ts_code, start, end),
    )
    return [float(r[0]) for r in cur.fetchall()]


def _classify_signal(momentum_1y: float | None, pb_pct: float | None) -> str:
    """Combine momentum and valuation into a readable signal."""
    if momentum_1y is None and pb_pct is None:
        return "数据不足"
    labels: list[str] = []
    if momentum_1y is not None:
        if momentum_1y >= 0.20:
            labels.append("强势")
        elif momentum_1y >= 0.05:
            labels.append("温和上涨")
        elif momentum_1y <= -0.20:
            labels.append("大跌后")
        elif momentum_1y <= -0.05:
            labels.append("回调中")
        else:
            labels.append("横盘")
    if pb_pct is not None:
        if pb_pct <= 0.25:
            labels.append("估值偏低")
        elif pb_pct <= 0.50:
            labels.append("估值合理")
        elif pb_pct <= 0.75:
            labels.append("估值偏高")
        else:
            labels.append("估值高位")
    return " | ".join(labels) if labels else "中性"


def sector_signals_as_of(conn, as_of_date: str) -> list[SectorSignal]:
    """Compute SW L1 sector signals as of a given date.

    as_of_date: ISO date string like '2021-12-31'
    Returns list of SectorSignal, sorted by momentum_1y desc.
    """
    as_of_dt = as_of_date
    year = int(as_of_date[:4])
    one_yr_prior = f"{year - 1}{as_of_date[4:]}"
    hist_start = f"{year - PB_HISTORY_YEARS}{as_of_date[4:]}"

    l1_sectors = _load_sw_l1()
    signals: list[SectorSignal] = []

    with conn.cursor() as cur:
        for ts_code, name in l1_sectors:
            close_now = _close_on_or_before(cur, ts_code, as_of_dt)
            close_1y = _close_on_or_before(cur, ts_code, one_yr_prior)
            momentum_1y = (close_now / close_1y - 1) if close_now and close_1y else None

            pb_now = _pb_on_or_before(cur, ts_code, as_of_dt)
            pb_hist = _pb_history(cur, ts_code, hist_start, as_of_dt)

            pb_pct = None
            pb_zscore = None
            if pb_now is not None and pb_hist:
                pb_min = min(pb_hist)
                pb_max = max(pb_hist)
                if pb_max > pb_min:
                    pb_pct = (pb_now - pb_min) / (pb_max - pb_min)
                n = len(pb_hist)
                if n >= 4:
                    mu = sum(pb_hist) / n
                    std = (sum((x - mu) ** 2 for x in pb_hist) / n) ** 0.5
                    if std > 0:
                        pb_zscore = (pb_now - mu) / std

            sig = SectorSignal(
                ts_code=ts_code,
                name=name,
                momentum_1y=momentum_1y,
                pb_now=pb_now,
                pb_pct_5y=pb_pct,
                pb_zscore=pb_zscore,
                signal_label=_classify_signal(momentum_1y, pb_pct),
            )
            signals.append(sig)

    signals.sort(key=lambda s: s.momentum_1y if s.momentum_1y is not None else float("-inf"), reverse=True)
    return signals
