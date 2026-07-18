import datetime as dt
import unittest

from scripts.sync_etf_share_size_snapshots import (
    parse_response_rows,
    period_ends,
    quarter_ends,
)


class EtfShareSizeSyncTest(unittest.TestCase):
    def test_quarter_ends(self):
        self.assertEqual(
            quarter_ends(dt.date(2020, 2, 1), dt.date(2020, 7, 1)),
            [dt.date(2020, 3, 31), dt.date(2020, 6, 30)],
        )

    def test_monthly_period_ends(self):
        self.assertEqual(
            period_ends(dt.date(2020, 2, 1), dt.date(2020, 4, 1)),
            [dt.date(2020, 2, 29), dt.date(2020, 3, 31)],
        )

    def test_parser_filters_unapproved_and_sets_next_day_availability(self):
        payload = {
            "data": {
                "fields": [
                    "trade_date", "ts_code", "total_share", "total_size",
                    "nav", "close", "exchange",
                ],
                "items": [
                    ["20250331", "510300.SH", 100.0, 425.0, 4.25, 4.24, "SSE"],
                    ["20250331", "513500.SH", 50.0, 60.0, 1.2, 1.2, "SSE"],
                    ["20250331", "510050.SH", 0.0, 0.0, None, None, "SSE"],
                ],
            }
        }
        rows = parse_response_rows(payload, {"510300.SH"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "510300.SH")
        self.assertEqual(rows[0][1], dt.date(2025, 3, 31))
        self.assertEqual(rows[0][2], dt.date(2025, 4, 1))
        self.assertEqual(rows[0][3], 100.0)


if __name__ == "__main__":
    unittest.main()
