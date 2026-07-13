#!/usr/bin/env python3
"""Unit tests for news processing (cluster, salience, fingerprint)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from news.processing.fingerprint import event_fingerprint, title_similarity
from news.processing.salience import event_salience_weight, sentiment_sign


def test_fingerprint_cross_source():
    fp1 = event_fingerprint("国务院发布新能源产业支持政策")
    fp2 = event_fingerprint("国务院发布新能源产业支持政策")
    assert fp1 == fp2
    sim = title_similarity(
        "国务院发布新能源产业支持政策",
        "国务院发布新能源产业支持政策（更新）",
    )
    assert sim >= 0.72


def test_salience_monotonic():
    base = event_salience_weight(
        sign=1.0, magnitude=2, confidence=0.9,
        mention_count=1, unique_sources=1, duration_days=1,
    )
    more_mentions = event_salience_weight(
        sign=1.0, magnitude=2, confidence=0.9,
        mention_count=5, unique_sources=1, duration_days=1,
    )
    more_sources = event_salience_weight(
        sign=1.0, magnitude=2, confidence=0.9,
        mention_count=1, unique_sources=3, duration_days=1,
    )
    assert more_mentions > base, "repeated mentions should strengthen signal"
    assert more_sources > base, "multi-source should strengthen signal"
    assert sentiment_sign("bullish") == 1.0
    assert sentiment_sign("bearish") == -1.0


def test_salience_log_dampening():
    w10 = event_salience_weight(
        sign=1.0, magnitude=1, confidence=1,
        mention_count=10, unique_sources=1, duration_days=1,
    )
    w100 = event_salience_weight(
        sign=1.0, magnitude=1, confidence=1,
        mention_count=100, unique_sources=1, duration_days=1,
    )
    assert w100 > w10
    assert w100 < w10 * 10, "log dampening prevents linear blow-up"


def test_salience_category():
    flash = event_salience_weight(
        sign=1.0, magnitude=2, confidence=0.9,
        mention_count=2, unique_sources=1, duration_days=1, category="flash",
    )
    policy = event_salience_weight(
        sign=1.0, magnitude=2, confidence=0.9,
        mention_count=2, unique_sources=1, duration_days=1, category="policy",
    )
    assert policy > flash


def main():
    test_fingerprint_cross_source()
    test_salience_monotonic()
    test_salience_log_dampening()
    test_salience_category()
    print("ALL NEWS PROCESSING UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
