#!/usr/bin/env python3
"""Import SSE historical ETF option contract events and build an archive."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
import requests
from dotenv import load_dotenv

QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
REFERER = "https://www.sse.com.cn/disclosure/optioninfo/preinfo/"
EVENT_SQL_IDS = {
    "new_listing": "SSE_ZQPZ_YSP_OPTZSXT_ADJUST_INFO_HYXG_SEARCH_L",
    "delist": "SSE_ZQPZ_YSP_OPTZSXT_ADJUST_INFO_HYZP_SEARCH_L",
    "adjustment": "SSE_ZQPZ_YSP_OPTZSXT_ADJUST_INFO_HYTZ_SEARCH_L",
}
DEFAULT_SECURITY_CODES = ["510050", "510300", "510500"]


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def apply_schema(conn: pymysql.Connection) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS cn_option_contract_event (
            event_type varchar(32) NOT NULL,
            event_date date NOT NULL,
            security_code varchar(16) NOT NULL,
            option_ts_code varchar(24) NOT NULL,
            contract_id varchar(64) DEFAULT NULL,
            contract_symbol varchar(128) DEFAULT NULL,
            underlying_name_code varchar(128) DEFAULT NULL,
            call_put varchar(16) DEFAULT NULL,
            exercise_price decimal(18,4) DEFAULT NULL,
            contract_unit decimal(18,4) DEFAULT NULL,
            exercise_date date DEFAULT NULL,
            delivery_date date DEFAULT NULL,
            expire_date date DEFAULT NULL,
            start_date date DEFAULT NULL,
            end_date date DEFAULT NULL,
            settl_price decimal(18,4) DEFAULT NULL,
            margin_unit decimal(18,4) DEFAULT NULL,
            raw_json json DEFAULT NULL,
            updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (event_type, event_date, security_code, option_ts_code),
            KEY idx_cn_option_event_contract (option_ts_code),
            KEY idx_cn_option_event_security_date (security_code, event_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='SSE option contract event records from commonQuery.do'
        """,
        """
        CREATE TABLE IF NOT EXISTS cn_option_contract_archive (
            option_ts_code varchar(24) NOT NULL,
            exchange varchar(12) NOT NULL DEFAULT 'SSE',
            opt_code varchar(32) DEFAULT NULL,
            security_code varchar(16) DEFAULT NULL,
            contract_id varchar(64) DEFAULT NULL,
            contract_symbol varchar(128) DEFAULT NULL,
            call_put varchar(2) DEFAULT NULL,
            exercise_price decimal(18,4) DEFAULT NULL,
            contract_unit decimal(18,4) DEFAULT NULL,
            list_date date DEFAULT NULL,
            maturity_date date DEFAULT NULL,
            last_event_date date DEFAULT NULL,
            source varchar(32) NOT NULL DEFAULT 'sse_common_query',
            updated_at timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (option_ts_code),
            KEY idx_cn_option_archive_underlying (opt_code, call_put, maturity_date, exercise_price)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='Historical China option contract master reconstructed from SSE events'
        """,
    ]
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    conn.commit()


def parse_date(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text in {"-", "None", "nan"}:
        return None
    text = text.replace("-", "")
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return None


def to_float(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "-":
        return None
    return float(text)


def call_put_code(raw: Any) -> str | None:
    text = str(raw or "")
    if "认购" in text or text.upper() == "C":
        return "C"
    if "认沽" in text or text.upper() == "P":
        return "P"
    return None


def opt_code_from_security(security_code: str) -> str:
    return f"OP{security_code}.SH"


def fetch_event(
    session: requests.Session,
    event_type: str,
    event_date: date,
    security_code: str,
    timeout: float,
    retries: int,
) -> list[dict[str, Any]]:
    params = {
        "isPagination": "false",
        "sqlId": EVENT_SQL_IDS[event_type],
        "adjustDate": event_date.strftime("%Y%m%d"),
        "securityCode": security_code,
        "jsonCallBack": "jsonpCallback",
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = session.get(QUERY_URL, params=params, headers={"Referer": REFERER}, timeout=timeout)
            response.raise_for_status()
            break
        except (requests.RequestException, TimeoutError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(min(2.0 * (attempt + 1), 10.0))
    else:
        raise RuntimeError(f"failed to fetch {event_type} {event_date} {security_code}: {last_error}")
    text = response.text.strip()
    if text.startswith("jsonpCallback(") and text.endswith(")"):
        text = text[len("jsonpCallback(") : -1]
    payload = json.loads(text)
    return payload.get("result") or []


def upsert_events(conn: pymysql.Connection, event_type: str, event_date: date, security_code: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    event_sql = """
        INSERT INTO cn_option_contract_event (
            event_type, event_date, security_code, option_ts_code, contract_id,
            contract_symbol, underlying_name_code, call_put, exercise_price,
            contract_unit, exercise_date, delivery_date, expire_date, start_date,
            end_date, settl_price, margin_unit, raw_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON))
        ON DUPLICATE KEY UPDATE
            contract_id=VALUES(contract_id),
            contract_symbol=VALUES(contract_symbol),
            underlying_name_code=VALUES(underlying_name_code),
            call_put=VALUES(call_put),
            exercise_price=VALUES(exercise_price),
            contract_unit=VALUES(contract_unit),
            exercise_date=VALUES(exercise_date),
            delivery_date=VALUES(delivery_date),
            expire_date=VALUES(expire_date),
            start_date=VALUES(start_date),
            end_date=VALUES(end_date),
            settl_price=VALUES(settl_price),
            margin_unit=VALUES(margin_unit),
            raw_json=VALUES(raw_json)
    """
    archive_sql = """
        INSERT INTO cn_option_contract_archive (
            option_ts_code, exchange, opt_code, security_code, contract_id,
            contract_symbol, call_put, exercise_price, contract_unit, list_date,
            maturity_date, last_event_date, source
        )
        VALUES (%s, 'SSE', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'sse_common_query')
        ON DUPLICATE KEY UPDATE
            opt_code=COALESCE(VALUES(opt_code), opt_code),
            security_code=COALESCE(VALUES(security_code), security_code),
            contract_id=COALESCE(VALUES(contract_id), contract_id),
            contract_symbol=COALESCE(VALUES(contract_symbol), contract_symbol),
            call_put=COALESCE(VALUES(call_put), call_put),
            exercise_price=COALESCE(VALUES(exercise_price), exercise_price),
            contract_unit=COALESCE(VALUES(contract_unit), contract_unit),
            list_date=COALESCE(LEAST(COALESCE(list_date, VALUES(list_date)), VALUES(list_date)), list_date, VALUES(list_date)),
            maturity_date=COALESCE(VALUES(maturity_date), maturity_date),
            last_event_date=GREATEST(COALESCE(last_event_date, VALUES(last_event_date)), VALUES(last_event_date))
    """
    event_rows = []
    archive_rows = []
    for row in rows:
        option_ts_code = str(row.get("SECURITY_ID") or "").strip()
        if not option_ts_code:
            continue
        if "." not in option_ts_code:
            option_ts_code = f"{option_ts_code}.SH"
        contract_id = row.get("CONTRACT_ID")
        contract_symbol = row.get("CONTRACT_SYMBOL")
        call_put = call_put_code(row.get("CALL_OR_PUT"))
        exercise_price = to_float(row.get("EXERCISE_PRICE"))
        contract_unit = to_float(row.get("CONTRACT_UNIT"))
        expire_date = parse_date(row.get("EXPIRE_DATE"))
        start_date = parse_date(row.get("START_DATE")) or event_date.isoformat()
        event_rows.append(
            (
                event_type,
                event_date.isoformat(),
                security_code,
                option_ts_code,
                contract_id,
                contract_symbol,
                row.get("SECURITYNAMEBYID"),
                call_put,
                exercise_price,
                contract_unit,
                parse_date(row.get("EXERCISE_DATE")),
                parse_date(row.get("DELIVERY_DATE")),
                expire_date,
                start_date,
                parse_date(row.get("END_DATE")),
                to_float(row.get("SETTL_PRICE") or row.get("ADJUST_SETTLE_PRICE")),
                to_float(row.get("MARGIN_UNIT")),
                json.dumps(row, ensure_ascii=False),
            )
        )
        archive_rows.append(
            (
                option_ts_code,
                opt_code_from_security(security_code),
                security_code,
                contract_id,
                contract_symbol,
                call_put,
                exercise_price,
                contract_unit,
                start_date,
                expire_date,
                event_date.isoformat(),
            )
        )
    with conn.cursor() as cur:
        if event_rows:
            cur.executemany(event_sql, event_rows)
        if archive_rows:
            cur.executemany(archive_sql, archive_rows)
    conn.commit()
    return len(event_rows)


def trade_dates(conn: pymysql.Connection, start: date, end: date) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT trade_date
            FROM index_daily
            WHERE ts_code='000300.SH' AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date
            """,
            (start, end),
        )
        return [row[0] for row in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Import SSE option contract events by date range.")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--security-codes", nargs="+", default=DEFAULT_SECURITY_CODES)
    parser.add_argument("--event-types", nargs="+", default=list(EVENT_SQL_IDS))
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        apply_schema(conn)
        dates = trade_dates(conn, date.fromisoformat(args.start), date.fromisoformat(args.end))
        total = 0
        errors: list[dict[str, str]] = []
        session = requests.Session()
        for trade_date in dates:
            daily_total = 0
            for security_code in args.security_codes:
                for event_type in args.event_types:
                    try:
                        rows = fetch_event(session, event_type, trade_date, security_code, args.timeout, args.retries)
                    except Exception as exc:
                        if not args.continue_on_error:
                            raise
                        errors.append(
                            {
                                "trade_date": trade_date.isoformat(),
                                "security_code": security_code,
                                "event_type": event_type,
                                "error": str(exc),
                            }
                        )
                        print(f"{trade_date}: error {security_code} {event_type}: {exc}")
                        rows = []
                    written = upsert_events(conn, event_type, trade_date, security_code, rows)
                    daily_total += written
                    time.sleep(args.sleep)
            if daily_total:
                print(f"{trade_date}: events={daily_total}")
            total += daily_total
        print(f"Imported SSE option events: dates={len(dates)} events={total} errors={len(errors)}")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*), MIN(list_date), MAX(maturity_date)
                FROM cn_option_contract_archive
                """
            )
            print(f"cn_option_contract_archive: {cur.fetchone()}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
