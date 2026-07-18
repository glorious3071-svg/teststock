#!/usr/bin/env python3
"""Import current US option-chain snapshots from public delayed quote sources.

This is execution evidence for current target generation only.  These endpoints
do not provide a 20-year historical option-chain archive, so the table is not a
substitute for executable historical option backtests.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection

SCHEMA_FILE = ROOT / "sql" / "us_option_chain_snapshot_schema.sql"
DEFAULT_SYMBOLS = ["QQQ"]
SOURCE_YAHOO = "yahoo_options"
SOURCE_CBOE = "cboe_delayed_quotes"
OCC_RE = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


def ensure_schema(conn) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for statement in [part.strip() for part in sql.split(";") if part.strip()]:
            cur.execute(statement)
    conn.commit()


def utc_date(timestamp: int | float | None) -> dt.date | None:
    if timestamp is None:
        return None
    return dt.datetime.fromtimestamp(float(timestamp), tz=dt.timezone.utc).date()


def utc_datetime(timestamp: int | float | None) -> dt.datetime | None:
    if timestamp is None:
        return None
    return dt.datetime.fromtimestamp(float(timestamp), tz=dt.timezone.utc).replace(tzinfo=None)


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def yahoo_session(timeout: float) -> tuple[requests.Session, str]:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    # Yahoo sets the cookie needed by the crumb endpoint even when the warmup
    # endpoint itself returns 404.  The crumb response is the real health check.
    session.get("https://fc.yahoo.com", timeout=timeout)
    crumb_resp = session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=timeout)
    crumb_resp.raise_for_status()
    crumb = crumb_resp.text.strip()
    if not crumb:
        raise RuntimeError("Yahoo crumb response was empty")
    return session, crumb


def fetch_options(
    session: requests.Session,
    crumb: str,
    symbol: str,
    expiration_ts: int | None,
    timeout: float,
) -> dict[str, Any]:
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    params: dict[str, Any] = {"crumb": crumb}
    if expiration_ts is not None:
        params["date"] = expiration_ts
    resp = session.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    chain = payload.get("optionChain", {})
    error = chain.get("error")
    if error:
        raise RuntimeError(f"Yahoo option-chain error for {symbol}: {error}")
    results = chain.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo option-chain returned no result for {symbol}")
    return results[0]


def choose_expirations(
    expirations: list[int],
    quote_date: dt.date,
    min_dte: int,
    max_dte: int,
    max_expirations: int,
) -> list[int]:
    selected = []
    for expiration in expirations:
        expiry_date = utc_date(expiration)
        if expiry_date is None:
            continue
        dte = (expiry_date - quote_date).days
        if dte < min_dte or dte > max_dte:
            continue
        selected.append(expiration)
    selected.sort()
    if max_expirations > 0:
        selected = selected[:max_expirations]
    return selected


def row_from_contract(symbol: str, quote_date: dt.date, expiry: dt.date, option_type: str, contract: dict[str, Any]) -> dict[str, Any]:
    bid = parse_float(contract.get("bid"))
    ask = parse_float(contract.get("ask"))
    last_price = parse_float(contract.get("lastPrice"))
    mark = (bid + ask) / 2.0 if bid is not None and ask is not None and bid > 0 and ask > 0 else last_price
    return {
        "underlying_symbol": symbol,
        "quote_date": quote_date,
        "expiration_date": expiry,
        "option_type": option_type,
        "strike": parse_float(contract.get("strike")),
        "contract_symbol": contract.get("contractSymbol"),
        "currency": contract.get("currency"),
        "contract_size": contract.get("contractSize"),
        "last_trade_time": utc_datetime(contract.get("lastTradeDate")),
        "bid": bid,
        "ask": ask,
        "mark": mark,
        "last_price": last_price,
        "implied_volatility": parse_float(contract.get("impliedVolatility")),
        "delta_value": None,
        "volume": parse_int(contract.get("volume")),
        "open_interest": parse_int(contract.get("openInterest")),
        "in_the_money": int(bool(contract.get("inTheMoney"))) if contract.get("inTheMoney") is not None else None,
        "source": SOURCE_YAHOO,
    }


def fetch_symbol_rows(
    session: requests.Session,
    crumb: str,
    symbol: str,
    quote_date: dt.date,
    min_dte: int,
    max_dte: int,
    max_expirations: int,
    timeout: float,
    sleep: float,
) -> tuple[list[dict[str, Any]], list[dt.date]]:
    first = fetch_options(session, crumb, symbol, None, timeout)
    expirations = choose_expirations(
        [int(item) for item in first.get("expirationDates", [])],
        quote_date,
        min_dte,
        max_dte,
        max_expirations,
    )
    rows: list[dict[str, Any]] = []
    expiry_dates = []
    for expiration in expirations:
        payload = fetch_options(session, crumb, symbol, expiration, timeout)
        options = payload.get("options") or []
        if not options:
            continue
        expiry = utc_date(expiration)
        if expiry is None:
            continue
        expiry_dates.append(expiry)
        option_set = options[0]
        for option_type, key in [("call", "calls"), ("put", "puts")]:
            for contract in option_set.get(key) or []:
                row = row_from_contract(symbol, quote_date, expiry, option_type, contract)
                if row["strike"] is not None and row["contract_symbol"]:
                    rows.append(row)
        time.sleep(sleep)
    return rows, expiry_dates


def parse_occ_symbol(contract_symbol: str) -> tuple[dt.date, str, float] | None:
    match = OCC_RE.match(contract_symbol)
    if not match:
        return None
    raw_date = match.group(2)
    option_type = "call" if match.group(3) == "C" else "put"
    strike = int(match.group(4)) / 1000.0
    year = 2000 + int(raw_date[:2])
    month = int(raw_date[2:4])
    day = int(raw_date[4:6])
    return dt.date(year, month, day), option_type, strike


def fetch_cboe_context(symbol: str, timeout: float) -> dict[str, Any]:
    url = f"https://www.cboe.com/delayed_quotes/{symbol.lower()}/quote_table"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    match = re.search(
        r"CTX\.contextOptionsData\s*=\s*(\{.*?\});\s*CTX\.ignoreSymbolsForSearch",
        resp.text,
        flags=re.S,
    )
    if not match:
        raise RuntimeError(f"CBOE delayed quote page did not expose contextOptionsData for {symbol}")
    return json.loads(match.group(1))


def row_from_cboe_contract(symbol: str, quote_date: dt.date, contract: dict[str, Any]) -> dict[str, Any] | None:
    contract_symbol = contract.get("option")
    parsed = parse_occ_symbol(contract_symbol or "")
    if parsed is None:
        return None
    expiry, option_type, strike = parsed
    bid = parse_float(contract.get("bid"))
    ask = parse_float(contract.get("ask"))
    last_price = parse_float(contract.get("last_trade_price"))
    theo = parse_float(contract.get("theo"))
    mark = (bid + ask) / 2.0 if bid is not None and ask is not None and bid > 0 and ask > 0 else theo or last_price
    return {
        "underlying_symbol": symbol,
        "quote_date": quote_date,
        "expiration_date": expiry,
        "option_type": option_type,
        "strike": strike,
        "contract_symbol": contract_symbol,
        "currency": "USD",
        "contract_size": None,
        "last_trade_time": parse_iso_datetime(contract.get("last_trade_time")),
        "bid": bid,
        "ask": ask,
        "mark": mark,
        "last_price": last_price,
        "implied_volatility": parse_float(contract.get("iv")),
        "delta_value": parse_float(contract.get("delta")),
        "volume": parse_int(contract.get("volume")),
        "open_interest": parse_int(contract.get("open_interest")),
        "in_the_money": None,
        "source": SOURCE_CBOE,
    }


def fetch_cboe_symbol_rows(
    symbol: str,
    quote_date: dt.date,
    min_dte: int,
    max_dte: int,
    max_expirations: int,
    timeout: float,
) -> tuple[list[dict[str, Any]], list[dt.date]]:
    payload = fetch_cboe_context(symbol, timeout)
    contracts = payload.get("data", {}).get("options") or []
    parsed_rows = []
    for contract in contracts:
        row = row_from_cboe_contract(symbol, quote_date, contract)
        if row is None:
            continue
        dte = (row["expiration_date"] - quote_date).days
        if dte < min_dte or dte > max_dte:
            continue
        parsed_rows.append(row)
    expiries = sorted({row["expiration_date"] for row in parsed_rows})
    if max_expirations > 0:
        selected = set(expiries[:max_expirations])
        parsed_rows = [row for row in parsed_rows if row["expiration_date"] in selected]
        expiries = sorted(selected)
    return parsed_rows, expiries


def upsert_rows(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO us_option_chain_snapshot
          (underlying_symbol, quote_date, expiration_date, option_type, strike,
           contract_symbol, currency, contract_size, last_trade_time, bid, ask,
           mark, last_price, implied_volatility, delta_value, volume,
           open_interest, in_the_money, source)
        VALUES
          (%(underlying_symbol)s, %(quote_date)s, %(expiration_date)s, %(option_type)s, %(strike)s,
           %(contract_symbol)s, %(currency)s, %(contract_size)s, %(last_trade_time)s, %(bid)s, %(ask)s,
           %(mark)s, %(last_price)s, %(implied_volatility)s, %(delta_value)s, %(volume)s,
           %(open_interest)s, %(in_the_money)s, %(source)s)
        ON DUPLICATE KEY UPDATE
          currency=VALUES(currency),
          contract_size=VALUES(contract_size),
          last_trade_time=VALUES(last_trade_time),
          bid=VALUES(bid),
          ask=VALUES(ask),
          mark=VALUES(mark),
          last_price=VALUES(last_price),
          implied_volatility=VALUES(implied_volatility),
          delta_value=VALUES(delta_value),
          volume=VALUES(volume),
          open_interest=VALUES(open_interest),
          in_the_money=VALUES(in_the_money),
          updated_at=CURRENT_TIMESTAMP
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def coverage(conn, symbols: list[str]) -> list[tuple[str, str, dt.date, int, dt.date | None, dt.date | None]]:
    placeholders = ",".join(["%s"] * len(symbols))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT source, underlying_symbol, quote_date, COUNT(*), MIN(expiration_date), MAX(expiration_date)
            FROM us_option_chain_snapshot
            WHERE underlying_symbol IN ({placeholders})
            GROUP BY source, underlying_symbol, quote_date
            ORDER BY underlying_symbol, quote_date DESC, source
            """,
            symbols,
        )
        return list(cur.fetchall())


def main() -> int:
    parser = argparse.ArgumentParser(description="Import current US option-chain snapshots from public delayed quote sources.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--provider", choices=[SOURCE_YAHOO, SOURCE_CBOE], default=SOURCE_CBOE)
    parser.add_argument("--quote-date", default=dt.date.today().isoformat())
    parser.add_argument("--min-dte", type=int, default=5)
    parser.add_argument("--max-dte", type=int, default=120)
    parser.add_argument("--max-expirations", type=int, default=12, help="0 means all expirations in the DTE window.")
    parser.add_argument("--sleep", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    quote_date = dt.date.fromisoformat(args.quote_date)
    session = None
    crumb = None
    if args.provider == SOURCE_YAHOO:
        session, crumb = yahoo_session(args.timeout)
    conn = get_connection()
    try:
        ensure_schema(conn)
        total = 0
        for symbol in args.symbols:
            if args.provider == SOURCE_YAHOO:
                rows, expiries = fetch_symbol_rows(
                    session,
                    crumb,
                    symbol.upper(),
                    quote_date,
                    args.min_dte,
                    args.max_dte,
                    args.max_expirations,
                    args.timeout,
                    args.sleep,
                )
            else:
                rows, expiries = fetch_cboe_symbol_rows(
                    symbol.upper(),
                    quote_date,
                    args.min_dte,
                    args.max_dte,
                    args.max_expirations,
                    args.timeout,
                )
            print(
                f"{symbol.upper()}: provider={args.provider} fetched={len(rows)} expirations={len(expiries)} "
                f"range={min(expiries) if expiries else None}..{max(expiries) if expiries else None}"
            )
            if not args.dry_run:
                total += upsert_rows(conn, rows)
        if args.dry_run:
            print("dry_run=True; no rows written")
        else:
            print(f"upserted={total}")
            for row in coverage(conn, [symbol.upper() for symbol in args.symbols]):
                print(f"coverage source={row[0]} symbol={row[1]} quote_date={row[2]} rows={row[3]} expiry_range={row[4]}..{row[5]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
