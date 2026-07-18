"""Point-in-time lexical features from official PBoC policy-report outlooks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Sequence


EASING_TERMS = {
    "适度宽松": 3.0,
    "降准": 2.0,
    "降息": 2.0,
    "降低存款准备金率": 2.0,
    "下调存款准备金率": 2.0,
    "保持流动性合理充裕": 2.0,
    "保持流动性充裕": 1.5,
    "流动性合理充裕": 1.5,
    "加大逆周期调节力度": 2.0,
    "加强逆周期调节": 1.5,
    "加大金融支持": 1.0,
    "降低社会综合融资成本": 2.0,
    "推动融资成本下降": 1.5,
    "促进融资成本下降": 1.5,
    "灵活适度": 1.0,
}

TIGHTENING_TERMS = {
    "适度从紧": 3.0,
    "从紧": 2.0,
    "上调存款准备金率": 2.0,
    "提高存款准备金率": 2.0,
    "加息": 2.0,
    "防止经济过热": 2.0,
    "抑制通货膨胀": 2.0,
    "抑制通胀": 2.0,
    "控制信贷投放": 2.0,
    "把好货币供给总闸门": 2.0,
    "管好货币总闸门": 2.0,
    "稳健中性": 1.0,
    "去杠杆": 1.0,
}

RISK_TERMS = {
    "下行压力": 1.5,
    "不确定性": 1.0,
    "风险挑战": 1.0,
    "需求不足": 1.5,
    "通货紧缩": 2.0,
    "通缩风险": 2.0,
    "通胀压力": 1.0,
    "房地产风险": 2.0,
    "金融风险": 1.0,
    "外部冲击": 1.5,
}

OUTLOOK_HEADINGS = (
    "下一阶段主要政策思路",
    "下一阶段货币政策思路",
    "下一阶段货币政策主要思路",
)


@dataclass(frozen=True)
class PbocReport:
    publication_date: date
    content: str


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def extract_policy_outlook(text: str, maximum_chars: int = 16_000) -> str:
    clean = normalize_text(text)
    positions = [clean.rfind(heading) for heading in OUTLOOK_HEADINGS]
    start = max(positions, default=-1)
    if start < 0:
        start = clean.rfind("第四部分")
    if start < 0:
        start = max(0, len(clean) * 2 // 3)
    return clean[start : start + maximum_chars]


def weighted_term_density(text: str, terms: dict[str, float]) -> float:
    if not text:
        return 0.0
    weighted_count = sum(text.count(term) * weight for term, weight in terms.items())
    return weighted_count * 10_000.0 / len(text)


def score_report(report: PbocReport) -> dict[str, float]:
    outlook = extract_policy_outlook(report.content)
    easing = weighted_term_density(outlook, EASING_TERMS)
    tightening = weighted_term_density(outlook, TIGHTENING_TERMS)
    risk = weighted_term_density(outlook, RISK_TERMS)
    return {
        "pboc_outlook_easing_density": easing,
        "pboc_outlook_tightening_density": tightening,
        "pboc_outlook_net_tone": easing - tightening,
        "pboc_outlook_risk_density": risk,
        "pboc_outlook_text_length": float(len(outlook)),
    }


def report_features_as_of(
    reports: Sequence[PbocReport], snapshot: date
) -> dict[str, float | None]:
    available = [report for report in reports if report.publication_date <= snapshot]
    if not available:
        return {
            "pboc_outlook_easing_density": None,
            "pboc_outlook_tightening_density": None,
            "pboc_outlook_net_tone": None,
            "pboc_outlook_risk_density": None,
            "pboc_outlook_net_tone_change": None,
            "pboc_outlook_risk_density_change": None,
            "pboc_report_age_days": None,
        }
    current = score_report(available[-1])
    previous = score_report(available[-2]) if len(available) >= 2 else None
    current["pboc_outlook_net_tone_change"] = (
        current["pboc_outlook_net_tone"] - previous["pboc_outlook_net_tone"]
        if previous is not None
        else None
    )
    current["pboc_outlook_risk_density_change"] = (
        current["pboc_outlook_risk_density"]
        - previous["pboc_outlook_risk_density"]
        if previous is not None
        else None
    )
    current["pboc_report_age_days"] = float(
        (snapshot - available[-1].publication_date).days
    )
    return current
