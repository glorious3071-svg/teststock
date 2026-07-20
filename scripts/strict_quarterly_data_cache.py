"""Local market-data cache for strict quarterly ETF research scripts."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

from backtest.domestic_defensive_etf import load_defensive_etf_universe
from backtest.domestic_equity_etf import load_equity_etf_return_universe
from db.connection import get_connection
from scripts.backtest_calendar_neutral_csi_tipp import load_selector_price_series
from scripts.backtest_scorecard_csi_dynamic_defense import load_price_series

CACHE_VERSION = 1


def _resolve_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            payload = pickle.load(fh)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("cache_version") != CACHE_VERSION:
        return None
    return payload


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)


def load_strict_quarterly_market_data(
    root: Path,
    cache_dir: Path,
    *,
    include_selector_index_series: bool = False,
    refresh: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Load reusable strict-quarterly market data, optionally from local cache.

    This caches only the raw historical price/universe data pulled from MySQL.
    Strategy features and ETF decisions are still recomputed point-in-time by
    the caller.
    """

    cache_name = (
        "market_data_with_selector_index_series.pkl"
        if include_selector_index_series
        else "market_data_replay.pkl"
    )
    cache_path = _resolve_path(root, cache_dir) / cache_name
    if use_cache and not refresh:
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached["data"]

    conn = get_connection()
    try:
        index_series = load_price_series(conn)
        if include_selector_index_series:
            load_selector_price_series(conn, index_series)
        defensive_metas, defensive_series = load_defensive_etf_universe(conn)
        equity_metas, equity_series = load_equity_etf_return_universe(conn)
    finally:
        conn.close()

    data = {
        "index_series": index_series,
        "defensive_metas": defensive_metas,
        "defensive_series": defensive_series,
        "equity_metas": equity_metas,
        "equity_series": equity_series,
    }
    if use_cache:
        _write_cache(
            cache_path,
            {
                "cache_version": CACHE_VERSION,
                "include_selector_index_series": include_selector_index_series,
                "data": data,
            },
        )
    return data
