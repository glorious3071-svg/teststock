#!/usr/bin/env python3
"""Unit tests for news pipeline (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collectors.enrichment import normalize_extraction, normalize_themes, CANONICAL_THEMES
from collectors.dedup import content_hash, html_to_text


def test_dedup():
    h1 = content_hash("eastmoney", "测试标题", "正文内容", None)
    h2 = content_hash("eastmoney", "测试标题", "正文内容", None)
    assert h1 == h2, "hash should be stable"
    assert html_to_text("<b>hello</b>") == "hello"


def test_enrichment():
    data = normalize_extraction({
        "sentiment": "bullish",
        "themes": ["半导体/数字经济", "无效题材"],
        "industries": ["电子"],
        "ts_codes": [],
        "event_type": "industry",
        "magnitude": 5,
        "summary": "测试",
        "reasoning": "理由",
        "confidence": 1.5,
    })
    assert data["sentiment"] == "bullish"
    assert data["themes"] == ["半导体/数字经济"]
    assert data["magnitude"] == 3
    assert data["confidence"] == 1.0
    assert len(CANONICAL_THEMES) == 17
    assert normalize_themes(["消费/内需"]) == ["消费/内需"]


def main():
    test_dedup()
    test_enrichment()
    print("ALL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
