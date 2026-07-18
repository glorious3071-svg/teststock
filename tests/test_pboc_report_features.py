from __future__ import annotations

import unittest
from datetime import date

from backtest.pboc_report_features import (
    PbocReport,
    extract_policy_outlook,
    report_features_as_of,
)


class PbocReportFeaturesTest(unittest.TestCase):
    def test_extracts_forward_policy_section(self) -> None:
        text = "历史回顾降准降息。第四部分其他。下一阶段主要政策思路保持流动性合理充裕。"
        outlook = extract_policy_outlook(text)
        self.assertNotIn("历史回顾", outlook)
        self.assertIn("保持流动性合理充裕", outlook)

    def test_report_is_not_visible_before_publication(self) -> None:
        reports = [
            PbocReport(date(2020, 5, 10), "下一阶段主要政策思路适度宽松降准。"),
        ]
        before = report_features_as_of(reports, date(2020, 5, 9))
        self.assertIsNone(before["pboc_outlook_net_tone"])
        after = report_features_as_of(reports, date(2020, 5, 10))
        self.assertGreater(float(after["pboc_outlook_net_tone"]), 0.0)

    def test_change_uses_previous_published_report_only(self) -> None:
        reports = [
            PbocReport(date(2020, 2, 1), "下一阶段主要政策思路适度从紧。"),
            PbocReport(date(2020, 5, 1), "下一阶段主要政策思路适度宽松。"),
            PbocReport(date(2020, 8, 1), "下一阶段主要政策思路适度从紧。"),
        ]
        features = report_features_as_of(reports, date(2020, 5, 31))
        self.assertGreater(float(features["pboc_outlook_net_tone_change"]), 0.0)
        self.assertEqual(features["pboc_report_age_days"], 30.0)


if __name__ == "__main__":
    unittest.main()
