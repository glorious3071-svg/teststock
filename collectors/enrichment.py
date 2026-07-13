"""LLM extraction from raw news articles."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from agents.annual_direction.llm_client import LLMError, chat

CANONICAL_THEMES = [
    "农业/三农",
    "节能环保/绿色低碳",
    "科技创新/自主创新",
    "基建/城镇化",
    "消费/内需",
    "汽车/交通装备",
    "新能源/光伏储能",
    "半导体/数字经济",
    "医药/医疗健康",
    "金融/资本市场",
    "房地产/城投化债",
    "军工/国防",
    "煤炭/钢铁/资源品",
    "对外开放/出海",
    "民营经济",
    "先进制造/产业升级",
    "人工智能/大数据",
]

EVENT_TYPES = [
    "policy",
    "trade",
    "geopolitics",
    "earnings",
    "industry",
    "macro",
    "regulation",
    "other",
]


def build_extraction_prompt(title: str, body: str | None) -> list[dict[str, str]]:
    themes_list = "\n".join(f"- {t}" for t in CANONICAL_THEMES)
    body_text = (body or "")[:4000]
    user_content = f"""分析以下财经新闻，输出 JSON（不要 markdown 代码块外的文字）。

标题：{title}
正文：
{body_text}

要求：
1. sentiment: bullish / bearish / neutral（对 A 股相关行业的中短期影响）
2. themes: 从下列列表选 0-3 个最相关题材（必须完全匹配字符串）
3. industries: 相关行业名称列表（中文，0-5 个）
4. ts_codes: 相关指数代码列表（如 000300.SH，不确定则 []）
5. event_type: 从 {EVENT_TYPES} 选一个
6. magnitude: 1-3 整数（影响强度）
7. summary: 50 字以内摘要
8. reasoning: 100 字以内理由
9. confidence: 0.0-1.0

可选题材：
{themes_list}

只输出 JSON 对象。"""

    return [
        {
            "role": "system",
            "content": "你是 A 股行业分析师，擅长从新闻中提取对 CSI 行业指数有预测价值的信息。",
        },
        {"role": "user", "content": user_content},
    ]


def parse_extraction_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def normalize_themes(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    allowed = set(CANONICAL_THEMES)
    return [t for t in raw if isinstance(t, str) and t in allowed][:3]


def normalize_extraction(data: dict[str, Any]) -> dict[str, Any]:
    sentiment = data.get("sentiment")
    if sentiment not in ("bullish", "bearish", "neutral"):
        sentiment = "neutral"

    magnitude = data.get("magnitude")
    try:
        magnitude = max(1, min(3, int(magnitude)))
    except (TypeError, ValueError):
        magnitude = 1

    confidence = data.get("confidence", 0.8)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.8

    event_type = data.get("event_type") or "other"
    if event_type not in EVENT_TYPES:
        event_type = "other"

    industries = data.get("industries") or []
    if isinstance(industries, str):
        industries = [industries]
    industries = [str(x) for x in industries if x][:5]

    ts_codes = data.get("ts_codes") or []
    if isinstance(ts_codes, str):
        ts_codes = [ts_codes]
    ts_codes = [str(x) for x in ts_codes if x][:5]

    summary = str(data.get("summary") or "")[:500]
    reasoning = str(data.get("reasoning") or "")[:2000]

    return {
        "sentiment": sentiment,
        "themes": normalize_themes(data.get("themes")),
        "industries": industries,
        "ts_codes": ts_codes,
        "event_type": event_type,
        "magnitude": magnitude,
        "summary": summary,
        "reasoning": reasoning,
        "confidence": confidence,
    }


def extract_article(title: str, body: str | None, *, mock: bool = False) -> dict[str, Any]:
    if mock:
        return _mock_extract(title, body)
    messages = build_extraction_prompt(title, body)
    raw = chat(messages, temperature=0.2)
    parsed = parse_extraction_json(raw)
    if not parsed:
        raise LLMError(f"Failed to parse LLM JSON: {raw[:200]}")
    return normalize_extraction(parsed)


def _mock_extract(title: str, body: str | None) -> dict[str, Any]:
    """Keyword fallback when LLM API is unreachable (testing / offline)."""
    text = f"{title} {body or ''}"
    themes: list[str] = []
    mapping = {
        "半导体/数字经济": ("半导体", "芯片", "算力", "数字经济"),
        "新能源/光伏储能": ("新能源", "光伏", "储能", "锂电"),
        "医药/医疗健康": ("医药", "医疗", "生物"),
        "金融/资本市场": ("券商", "银行", "资本市场", "金融"),
        "煤炭/钢铁/资源品": ("煤炭", "钢铁", "石油", "燃油", "资源"),
        "军工/国防": ("军工", "国防", "军事"),
        "消费/内需": ("消费", "内需", "零售"),
        "对外开放/出海": ("出口", "出海", "外贸", "关税"),
    }
    for theme, kws in mapping.items():
        if any(k in text for k in kws):
            themes.append(theme)
    sentiment = "bearish" if any(k in text for k in ("下跌", "暴雷", "战争", "制裁", "短缺", "危机")) else "neutral"
    if any(k in text for k in ("增长", "利好", "上涨", "突破")):
        sentiment = "bullish"
    return normalize_extraction({
        "sentiment": sentiment,
        "themes": themes[:3],
        "industries": [],
        "ts_codes": [],
        "event_type": "industry" if themes else "macro",
        "magnitude": 2 if themes else 1,
        "summary": title[:50],
        "reasoning": "mock keyword extraction",
        "confidence": 0.5,
    })
