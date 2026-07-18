#!/usr/bin/env python3
"""Aggregate point-in-time stock valuation fields to historical index snapshots."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS index_constituent_fundamental (
    index_code VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    stock_trade_date DATE NOT NULL,
    constituent_count INT NOT NULL,
    total_weight DOUBLE NOT NULL,
    valuation_coverage_weight DOUBLE NOT NULL,
    dividend_coverage_weight DOUBLE NOT NULL,
    earnings_yield DOUBLE NULL,
    book_yield DOUBLE NULL,
    roe_proxy DOUBLE NULL,
    dividend_yield DOUBLE NULL,
    positive_earnings_weight DOUBLE NULL,
    weight_hhi DOUBLE NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (index_code, trade_date),
    KEY idx_icf_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


UPSERT = """
INSERT INTO index_constituent_fundamental (
    index_code, trade_date, stock_trade_date, constituent_count, total_weight,
    valuation_coverage_weight, dividend_coverage_weight, earnings_yield,
    book_yield, roe_proxy, dividend_yield, positive_earnings_weight, weight_hhi
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    stock_trade_date=VALUES(stock_trade_date),
    constituent_count=VALUES(constituent_count),
    total_weight=VALUES(total_weight),
    valuation_coverage_weight=VALUES(valuation_coverage_weight),
    dividend_coverage_weight=VALUES(dividend_coverage_weight),
    earnings_yield=VALUES(earnings_yield),
    book_yield=VALUES(book_yield),
    roe_proxy=VALUES(roe_proxy),
    dividend_yield=VALUES(dividend_yield),
    positive_earnings_weight=VALUES(positive_earnings_weight),
    weight_hhi=VALUES(weight_hhi)
"""


def weighted_mean(rows, value_index: int, valid, transform) -> tuple[float | None, float]:
    pairs = [
        (float(row[1]), transform(float(row[value_index])))
        for row in rows
        if row[value_index] is not None and valid(float(row[value_index]))
    ]
    coverage = sum(weight for weight, _value in pairs)
    return (
        sum(weight * value for weight, value in pairs) / coverage
        if coverage > 0
        else None,
        coverage,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="append")
    parser.add_argument("--minimum-coverage", type=float, default=0.80)
    args = parser.parse_args()

    conn = get_connection()
    written = rejected = 0
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            conditions = ""
            params: list[str] = []
            if args.index:
                conditions = " WHERE index_code IN (" + ",".join(["%s"] * len(args.index)) + ")"
                params.extend(args.index)
            cur.execute(
                "SELECT DISTINCT index_code, trade_date FROM index_constituent"
                + conditions
                + " ORDER BY trade_date, index_code",
                params,
            )
            snapshots = list(cur.fetchall())
            for index_code, snapshot in snapshots:
                cur.execute(
                    "SELECT MAX(trade_date) FROM stock_daily_basic WHERE trade_date<=%s",
                    (snapshot,),
                )
                stock_date = cur.fetchone()[0]
                if stock_date is None:
                    rejected += 1
                    continue
                cur.execute(
                    """
                    SELECT c.con_code, c.weight / 100.0, c.weight,
                           s.pe_ttm, s.pb, s.dv_ttm
                    FROM index_constituent c
                    LEFT JOIN stock_daily_basic s
                      ON s.ts_code=c.con_code AND s.trade_date=%s
                    WHERE c.index_code=%s AND c.trade_date=%s AND c.weight IS NOT NULL
                    """,
                    (stock_date, index_code, snapshot),
                )
                rows = list(cur.fetchall())
                total_weight = sum(float(row[2]) for row in rows) / 100.0
                if not rows or total_weight < 0.95:
                    rejected += 1
                    continue
                normalized = [
                    (row[0], float(row[1]) / total_weight, *row[2:]) for row in rows
                ]
                earnings_yield, earnings_coverage = weighted_mean(
                    normalized,
                    3,
                    lambda value: 0.0 < value < 500.0,
                    lambda value: 1.0 / value,
                )
                book_yield, book_coverage = weighted_mean(
                    normalized,
                    4,
                    lambda value: 0.0 < value < 100.0,
                    lambda value: 1.0 / value,
                )
                dividend_yield, dividend_coverage = weighted_mean(
                    normalized,
                    5,
                    lambda value: 0.0 <= value < 100.0,
                    lambda value: value / 100.0,
                )
                valuation_coverage = min(earnings_coverage, book_coverage)
                if valuation_coverage < args.minimum_coverage:
                    rejected += 1
                    continue
                roe_proxy = (
                    earnings_yield / book_yield
                    if earnings_yield is not None and book_yield not in (None, 0.0)
                    else None
                )
                positive_earnings_weight = sum(
                    float(row[1])
                    for row in normalized
                    if row[3] is not None and 0.0 < float(row[3]) < 500.0
                )
                hhi = sum(float(row[1]) ** 2 for row in normalized)
                cur.execute(
                    UPSERT,
                    (
                        index_code,
                        snapshot,
                        stock_date,
                        len(rows),
                        total_weight,
                        valuation_coverage,
                        dividend_coverage,
                        earnings_yield,
                        book_yield,
                        roe_proxy,
                        dividend_yield,
                        positive_earnings_weight,
                        hhi,
                    ),
                )
                written += 1
        conn.commit()
    finally:
        conn.close()
    print(f"Done: written={written} rejected={rejected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
