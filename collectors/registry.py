"""News collector registry."""

from __future__ import annotations

from collectors.base import BaseCollector
from collectors.cctv_daily import CctvDailyCollector
from collectors.flash_eastmoney import EastmoneyFlashCollector
from collectors.flash_sina import SinaFlashCollector
from collectors.flash_ths import ThsFlashCollector
from collectors.intl_news import IntlNewsCollector
from collectors.policy_ndrc import NdrcPolicyCollector
from collectors.research_industry import IndustryResearchCollector

COLLECTORS: dict[str, BaseCollector] = {
    "eastmoney_flash": EastmoneyFlashCollector(),
    "sina_flash": SinaFlashCollector(),
    "ths_flash": ThsFlashCollector(),
    "cctv_daily": CctvDailyCollector(),
    "research_industry": IndustryResearchCollector(),
    "ndrc_policy": NdrcPolicyCollector(),
    "intl_cls": IntlNewsCollector(),
}

TIER_COLLECTORS: dict[str, list[str]] = {
    "flash": ["eastmoney_flash", "sina_flash", "ths_flash"],
    "daily": ["cctv_daily", "research_industry", "ndrc_policy", "intl_cls"],
    "all": list(COLLECTORS.keys()),
}


def get_collector(name: str) -> BaseCollector:
    if name not in COLLECTORS:
        raise KeyError(f"Unknown collector: {name}")
    return COLLECTORS[name]


def collectors_for_tier(tier: str) -> list[BaseCollector]:
    if tier not in TIER_COLLECTORS:
        raise KeyError(f"Unknown tier: {tier}. Choose from {list(TIER_COLLECTORS)}")
    return [COLLECTORS[name] for name in TIER_COLLECTORS[tier]]
