#!/usr/bin/env python3
"""Unit tests for news retrieval / prefilter."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news.retrieval.prefilter import match_keywords, should_extract_llm
from news.retrieval.keywords import THEME_KEYWORD_SEED
from collectors.enrichment import CANONICAL_THEMES


def test_keyword_match():
    pf = match_keywords("光伏储能板块大涨", "宁德时代锂电")
    assert "新能源/光伏储能" in pf.themes
    assert pf.score > 0


def test_force_policy():
    pf = match_keywords("无关标题", None)
    assert should_extract_llm("policy", pf) is True
    assert should_extract_llm("flash", pf) is False


def test_all_themes_have_keywords():
    for t in CANONICAL_THEMES:
        assert t in THEME_KEYWORD_SEED
        assert len(THEME_KEYWORD_SEED[t]) >= 1


def main():
    test_keyword_match()
    test_force_policy()
    test_all_themes_have_keywords()
    print("ALL RETRIEVAL UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
