from __future__ import annotations

import unittest
from datetime import date

from scripts.sync_chinabond_yield_curve import parse_curve_table, yearly_chunks


class SyncChinaBondYieldCurveTest(unittest.TestCase):
    def test_yearly_chunks_stay_below_one_year(self) -> None:
        chunks = yearly_chunks(date(2006, 3, 1), date(2008, 3, 1))
        self.assertEqual(chunks[0], (date(2006, 3, 1), date(2007, 2, 28)))
        self.assertEqual(chunks[-1][-1], date(2008, 3, 1))
        self.assertTrue(all((end - start).days <= 364 for start, end in chunks))

    def test_parse_curve_table_keeps_supported_curves_and_tenors(self) -> None:
        html = """
        <table><tr><th>empty</th></tr></table>
        <table>
          <tr><th>曲线名称</th><th>日期</th><th>1年</th><th>3年</th><th>5年</th><th>10年</th></tr>
          <tr><td>中债国债收益率曲线</td><td>2006-03-01</td><td>1.70</td><td>2.00</td><td>2.40</td><td>2.90</td></tr>
          <tr><td>不支持曲线</td><td>2006-03-01</td><td>9</td><td>9</td><td>9</td><td>9</td></tr>
        </table>
        """
        rows = parse_curve_table(html)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["symbol"], "CN_GOV_1Y")
        self.assertEqual(rows[-1]["symbol"], "CN_GOV_10Y")
        self.assertEqual(rows[-1]["close"], 2.9)
        self.assertEqual(rows[-1]["source"], "chinabond_yield")


if __name__ == "__main__":
    unittest.main()
