"""Salience / propagation strength weighting."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Category trust / signal quality multipliers (discussion: policy+research > flash)
CATEGORY_WEIGHT: dict[str, float] = {
    "policy": 1.50,
    "research": 1.30,
    "flash": 1.00,
    "intl": 1.05,
    "macro": 1.10,
    "industry": 1.15,
}


@dataclass(frozen=True)
class SalienceParams:
    alpha_sources: float = 0.2
    beta_duration: float = 0.1
    max_duration_days: int = 7


DEFAULT_SALIENCE = SalienceParams()


def salience_params() -> SalienceParams:
    return DEFAULT_SALIENCE


def category_weight(category: str | None) -> float:
    return CATEGORY_WEIGHT.get(category or "flash", 1.0)


def event_salience_weight(
    *,
    sign: float,
    magnitude: float,
    confidence: float,
    mention_count: int,
    unique_sources: int,
    duration_days: int,
    category: str | None = None,
    extra_mentions: int = 0,
    params: SalienceParams | None = None,
) -> float:
    """Compute signed event weight with mention/source/duration/category amplification."""
    p = params or DEFAULT_SALIENCE
    base = sign * magnitude * confidence * category_weight(category)
    total_mentions = max(mention_count, 1) + max(extra_mentions, 0)
    mention_mult = math.log1p(total_mentions)
    source_mult = 1.0 + p.alpha_sources * max(0, unique_sources - 1)
    duration_mult = 1.0 + p.beta_duration * min(max(duration_days, 1), p.max_duration_days)
    return base * mention_mult * source_mult * duration_mult


def sentiment_sign(sentiment: str | None) -> float:
    return {"bullish": 1.0, "bearish": -1.0}.get(sentiment or "", 0.0)
