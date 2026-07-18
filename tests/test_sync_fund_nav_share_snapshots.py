from __future__ import annotations

import unittest
from datetime import date

from scripts.sync_fund_nav_share_snapshots import parse_response_rows, quarter_ends


class FundNavShareSnapshotSyncTest(unittest.TestCase):
    def test_quarter_ends_are_calendar_quarters_within_bounds(self) -> None:
        self.assertEqual(
            quarter_ends(date(2020, 4, 1), date(2021, 1, 15)),
            [date(2020, 6, 30), date(2020, 9, 30), date(2020, 12, 31)],
        )

    def test_parser_keeps_only_allowed_announced_positive_share_rows(self) -> None:
        payload = {
            "data": {
                "fields": ["ts_code", "ann_date", "nav_date", "unit_nav", "net_asset"],
                "items": [
                    ["510300.SH", "20200420", "20200331", 4.0, 40_000_000.0],
                    ["513100.SH", "20200420", "20200331", 1.0, 10_000_000.0],
                    ["510050.SH", None, "20200331", 3.0, 30_000_000.0],
                    ["510500.SH", "20200301", "20200331", 5.0, 50_000_000.0],
                ],
            }
        }
        rows = parse_response_rows(payload, {"510300.SH", "510050.SH", "510500.SH"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "510300.SH")
        self.assertEqual(rows[0][5], 10_000_000.0)


if __name__ == "__main__":
    unittest.main()
