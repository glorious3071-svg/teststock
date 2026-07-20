"""Point-in-time mapping from selected A-share indices to listed passive ETFs."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from backtest.domestic_defensive_etf import classify_defensive_etf
from backtest.passive_etf_online_selector import (
    learned_online_feature_weights,
    load_observations,
)
from backtest.passive_etf_boosted_selector import (
    BOOSTED_ETF_POLICIES,
    policy_by_name as boosted_policy_by_name,
    select_boosted_etfs,
)
from backtest.passive_etf_supervised_selector import (
    ENRICHED_V2_DATASET,
    FUNDAMENTAL_V3_DATASET,
    CONSTITUENT_V4_DATASET,
    SHARE_V5_DATASET,
    SUPERVISED_ETF_POLICIES,
    current_candidate_observations,
    load_candidate_observations,
    policy_by_name as supervised_policy_by_name,
    select_static_stable_combo,
    select_static_stable_combo_top3,
    select_weighted_stable_combo_top3,
    select_weighted_stable_combo_top1,
    select_weighted_stable_combo_v2_top1,
    select_weighted_stable_combo_v3_top1,
    select_weighted_stable_combo_v4_top1,
    select_weighted_stable_combo_v5_top1,
    select_weighted_stable_combo_v6_top1,
    select_weighted_stable_combo_v7_top1,
    select_weighted_stable_combo_v9_top1,
    select_weighted_stable_combo_v10_top1,
    select_weighted_structural_mainline_top3,
    select_weighted_structural_mainline_top5,
    select_weighted_structural_liquidity_flow_top5,
    select_weighted_structural_momentum_breadth_top3,
    select_weighted_structural_resilience_top5,
    structural_healthcare_leadership_active,
    structural_digital_blowoff_rotation_active,
    structural_finance_defensive_rotation_active,
    structural_finance_catchup_active,
    structural_finance_substyle_for_text,
    structural_resource_bank_catchup_style_for_text,
    structural_local_mainline_pullback_reentry_active,
    structural_local_mainline_pullback_reentry_candidate,
    structural_local_mainline_pullback_reentry_subthemes,
    structural_new_energy_pullback_restart_active,
    structural_subtheme_group_for_text,
    structural_theme_group_for_text,
    weighted_stable_combo_v2_scores,
    weighted_stable_combo_v3_scores,
    weighted_stable_combo_v4_scores,
    weighted_stable_combo_v5_scores,
    weighted_stable_combo_v6_scores,
    weighted_stable_combo_v7_scores,
    weighted_stable_combo_v9_scores,
    weighted_stable_combo_v10_scores,
    weighted_structural_mainline_scores,
    weighted_structural_conditional_rotation_scores,
    weighted_structural_cooling_rotation_scores,
    weighted_structural_liquidity_group_breadth_scores,
    weighted_structural_liquidity_flow_scores,
    weighted_structural_late_cycle_defensive_rotation_scores,
    weighted_structural_finance_defensive_rotation_scores,
    weighted_structural_finance_catchup_scores,
    weighted_structural_finance_bank_catchup_scores,
    weighted_structural_finance_resource_catchup_scores,
    weighted_structural_resource_bank_catchup_scores,
    weighted_structural_late_cycle_small_growth_recovery_scores,
    weighted_structural_late_cycle_tech_pullback_continuation_scores,
    weighted_structural_local_mainline_pullback_reentry_scores,
    weighted_structural_new_energy_pullback_restart_scores,
    weighted_structural_late_cycle_policy_catalyst_scores,
    weighted_structural_momentum_breadth_scores,
    weighted_structural_multistate_rotation_scores,
    weighted_structural_reflation_rotation_scores,
    weighted_structural_value_reflation_mainline_scores,
    weighted_structural_resilience_scores,
    select_supervised_etfs,
)


OVERSEAS_CODE_PREFIXES = ("513", "517", "520")
OVERSEAS_KEYWORDS = (
    "港股",
    "沪港深",
    "恒生",
    "纳指",
    "标普",
    "日经",
    "德国",
    "法国",
    "美国",
    "中概",
    "海外",
    "全球",
    "东南亚",
    "沙特",
)
EARLY_BROAD_PROXY_CODES = (
    "510050.SH",  # SSE 50 ETF
    "510180.SH",  # SSE 180 ETF
    "159901.SZ",  # SZSE 100 ETF
    "159902.SZ",  # SME ETF
    "510880.SH",  # Dividend ETF
)
MAX_ETF_PRICE_STALENESS_DAYS = 14


def has_recent_etf_price(
    series: dict[str, list[tuple[date, float]]],
    code: str,
    snapshot: date,
    max_staleness_days: int = MAX_ETF_PRICE_STALENESS_DAYS,
) -> bool:
    """Return whether an ETF has a genuinely current point-in-time quote.

    A non-empty lifetime series is insufficient: historical import gaps used
    to make an ETF look like a zero-return, zero-volatility asset because the
    last old price was carried forward.  Month-end selection therefore
    requires a recent observable quote.
    """

    values = series.get(code) or []
    index = bisect_right(values, (snapshot, float("inf")))
    return bool(
        index
        and 0 <= (snapshot - values[index - 1][0]).days <= max_staleness_days
    )


@dataclass(frozen=True)
class EquityEtfMeta:
    code: str
    name: str
    index_code: str
    index_name: str
    list_date: date
    first_trade_date: date


@dataclass(frozen=True)
class DirectEtfSelectorPolicy:
    name: str
    top_n: int
    momentum_12m_weight: float
    momentum_6m_weight: float
    momentum_3m_weight: float
    low_volatility_weight: float
    score_power: float = 1.0
    low_beta_6m_weight: float = 0.0
    distance_high_12m_weight: float = 0.0
    low_volatility_1m_weight: float = 0.0
    deduplicate_tracking_index: bool = False
    direct_blend_weight: float = 1.0
    strong_trend_blend_weight: float | None = None
    strong_trend_return_3m_threshold: float = 0.08
    strong_trend_return_6m_threshold: float = 0.0
    strong_trend_ma_6m_distance_threshold: float = 0.0
    strong_trend_basket_drawdown_6m_threshold: float = -0.05


DIRECT_ETF_POLICIES = (
    DirectEtfSelectorPolicy("direct_momentum_top1", 1, 0.20, 0.45, 0.25, 0.10, 1.0),
    DirectEtfSelectorPolicy("direct_momentum_top3", 3, 0.20, 0.45, 0.25, 0.10, 2.0),
    DirectEtfSelectorPolicy("direct_momentum_top5", 5, 0.20, 0.45, 0.25, 0.10, 2.0),
    DirectEtfSelectorPolicy("direct_trend_top3", 3, 0.10, 0.35, 0.40, 0.15, 2.0),
    DirectEtfSelectorPolicy("direct_reversal_top3", 3, 0.30, -0.15, -0.45, 0.10, 2.0),
    DirectEtfSelectorPolicy("direct_reversal_top5", 5, 0.30, -0.15, -0.45, 0.10, 2.0),
    DirectEtfSelectorPolicy(
        "direct_stable_beta_top1", 1, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.35, 0.35, 0.30, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_beta_top3", 3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.35, 0.35, 0.30, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_beta_top5", 5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.35, 0.35, 0.30, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_momentum_top3", 3, 0.20, 0.0, 0.0, 0.0, 2.0,
        0.30, 0.25, 0.25, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_momentum_top5", 5, 0.20, 0.0, 0.0, 0.0, 2.0,
        0.30, 0.25, 0.25, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_regime_ic_top3", 3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_regime_ic_top5", 5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_online_ic_top3", 3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_online_ic_top5", 5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            policy.name, policy.top_n, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True,
        )
        for policy in SUPERVISED_ETF_POLICIES
    ),
    *(
        DirectEtfSelectorPolicy(
            policy.name, policy.top_n, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True,
        )
        for policy in BOOSTED_ETF_POLICIES
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_boost_rank_all_price_h120_q20_dd2_e16_top3_w{int(weight*100)}",
            3, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.125, 0.25, 0.50)
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_combo_beta_autocorr_vol3_top1",
        1,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        True,
    ),
    DirectEtfSelectorPolicy(
        "direct_stable_combo_beta_distance_autocorr_vol3_top3",
        3,
        0.0,
        0.0,
        0.0,
        0.0,
        2.0,
        0.0,
        0.0,
        0.0,
        True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_static_top1_w{int(weight*100)}",
            1,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            True,
            weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75)
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_static_top3_w{int(weight*100)}",
            3,
            0.0,
            0.0,
            0.0,
            0.0,
            2.0,
            0.0,
            0.0,
            0.0,
            True,
            weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_top3",
        3, 0.0, 0.0, 0.0, 0.0, 4.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_top3_w{int(weight*100)}",
            3, 0.0, 0.0, 0.0, 0.0, 4.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v2_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v2_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v3_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v3_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v4_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v4_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325, 0.35, 0.40)
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v5_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_weighted_stable_combo_v6_top1",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v6_top1_regime_w49_s71",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.71,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v7_flow010_top1_regime_w49_s75",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.75,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v7_flow050_top1_regime_w49_s75",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.75,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_roe050_top1_regime_w49_s92",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_repair_top5_s05_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_repair_top5_s10_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_repair_top5_s15_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_repair_top5_s20_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_rotcond20_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_rotcond20_shockres50_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_mombreadth_repair_top3_s05_rotcond20_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_mombreadth_repair_top3_s10_rotcond35_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_mombreadth_repair_top3_s20_rotcond50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_mombreadth_repair_top3_s20_rotcond50_shockres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_mombreadth_repair_top3_s20_rotcond50_shockres50_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_mombreadth_repair_top3_s20_rotcond50_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_groupbreadth_repair_top5_s20_rotcond50_shockres100_earlyres50_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_reflation_repair_top3_s20_rotcond50_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_reflation_repair_top3_s10_rotcond35_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_reflation_repair_top3_s15_rotcond50_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_conditional_repair_top3_s10_rotcond35_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_conditional_repair_top3_s15_rotcond50_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_conditional_repair_top3_s10_rotcond35_hcres30_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_conditional_repair_top3_s10_rotcond35_hcres50_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_conditional_repair_top3_s10_rotcond35_hcres100_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s10_rotcond35_hcres100_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s10_rotcond35_hcres100_structblend70_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s10_rotcond35_hcres100_structblend85_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_cooling_repair_top3_s00_rotcond100_structblend85_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_cooling_repair_top3_s00_rotcond50_structblend85_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_cooling_repair_top3_s00_rotcond50_structblend85_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s10_rotcond35_hcres100_structblend85_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_repair_top3_s10_rotcond35_hcres100_structblend85_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_repair_top3_s20_rotcond50_hcres100_structblend85_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_repair_top3_s20_rotcond50_hcres100_structblend85_purestruct_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_repair_top3_s20_rotcond50_hcres100_structblend85_purestructcond_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_repair_top3_s20_rotcond50_hcres100_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_sgrowth_repair_top3_s20_rotcond50_hcres100_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_valres100_valblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_valres70_valblend70_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_fcres100_fcblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_fcres70_fcblend70_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_fcbankres100_fcbankblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_fcbankres100_fcbankblend50_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_finres100_finresblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_rbres100_rbblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_lmres100_lmblend85_drotres100_drotblend85_rbres100_rbblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_findefres100_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top5_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100_drotblend85_structblend85_purestructcond_coldstart_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_policycat_repair_top3_s20_rotcond50_hcres100_structblend85_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_policycat_repair_top3_s20_rotcond50_hcres100_structblend85_purestructcond_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_repair_top3_s10_rotcond35_hcres100_structblend90_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_latecycle_repair_top3_s10_rotcond35_hcres100_structblend100_exhaustfallback_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s10_rotcond35_hcres100_structblend100_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s15_rotcond50_hcres100_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s20_rotcond50_hcres100_shockres100_earlyres50_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_resilience_repair_top5_s20_shockcond50_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_rotcond35_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_rotcond50_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_rotcond15_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_rotcond10_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_earlyrotcond50_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_earlyrotcond30_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_earlyrotcond20_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_earlyrotcond10_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_earlyrotcond100_regime_w49_s92",
        10, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_cond20_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_cond35_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_widecond20_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_widecond35_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v10_b050_v500_roe100_top1_regime_w49_s92",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_weighted_stable_v10_b050_v500_roe075_top1_regime_w49_s92",
        1, 0.0, 0.0, 0.0, 0.0, 1.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "direct_structural_mainline_top3",
        3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "direct_structural_mainline_top5",
        5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_structural_mainline_top3_regime_w49_s92",
        3, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_structural_mainline_top5_regime_w49_s92",
        5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True, 0.49, 0.92,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_structural_mainline_top5_w10",
        5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True, 0.10,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_structural_mainline_top5_w20",
        5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True, 0.20,
    ),
    DirectEtfSelectorPolicy(
        "blend_index_structural_mainline_top5_cond_w10",
        5, 0.0, 0.0, 0.0, 0.0, 2.0,
        0.0, 0.0, 0.0, True, 0.10,
    ),
    *(
        DirectEtfSelectorPolicy(
            f"blend_index_weighted_stable_v5_top1_w{int(weight*100)}",
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, weight,
        )
        for weight in (
            0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.275, 0.30, 0.325,
            0.35, 0.40, 0.45, 0.46, 0.47, 0.48, 0.49, 0.50,
            0.60, 0.70, 0.80, 0.90,
        )
    ),
    *(
        DirectEtfSelectorPolicy(
            (
                f"blend_index_weighted_stable_v5_top1_regime"
                f"_w{int(base_weight*100)}_s{int(strong_weight*100)}"
            ),
            1, 0.0, 0.0, 0.0, 0.0, 1.0,
            0.0, 0.0, 0.0, True, base_weight, strong_weight,
        )
        for base_weight, strong_weight in (
            (0.25, 0.60), (0.25, 0.70), (0.25, 0.80),
            (0.35, 0.60), (0.35, 0.70), (0.35, 0.80),
            (0.40, 0.60), (0.40, 0.70), (0.40, 0.80),
            (0.49, 0.60), (0.49, 0.65), (0.49, 0.70),
            (0.49, 0.71), (0.49, 0.72), (0.49, 0.75), (0.49, 0.76),
            (0.49, 0.77), (0.49, 0.78), (0.49, 0.79), (0.49, 0.80),
            (0.49, 0.81), (0.49, 0.82), (0.49, 0.83), (0.49, 0.84),
            (0.49, 0.85), (0.49, 0.86), (0.49, 0.87), (0.49, 0.88),
            (0.49, 0.89), (0.49, 0.90),
            (0.49, 0.92), (0.49, 0.94), (0.49, 0.96), (0.49, 0.98),
            (0.49, 1.00),
        )
    ),
)

_DIRECT_SELECTION_CACHE: dict[
    tuple[int, int, int, str, date], dict[str, float]
] = {}
_DIRECT_DIAGNOSTICS_CACHE: dict[
    tuple[int, int, str, date], dict[str, float]
] = {}
_INDEX_MAPPING_CACHE: dict[tuple[object, ...], dict[str, float]] = {}
_STRUCTURAL_THEME_GROUPS_CACHE: dict[int, dict[str, str]] = {}
_STRUCTURAL_SUBTHEME_GROUPS_CACHE: dict[int, dict[str, str]] = {}
_STRUCTURAL_FINANCE_SUBSTYLE_CACHE: dict[int, dict[str, str]] = {}
_STRUCTURAL_RESOURCE_BANK_STYLE_CACHE: dict[int, dict[str, str]] = {}
_STRUCTURAL_PRICE_COLD_START_CACHE: dict[
    tuple[int, int, date, frozenset[str]], dict[str, float]
] = {}

SELECTED_ETF_DIAGNOSTIC_FIELDS = {
    "momentum_1m": "selected_etf_momentum_1m",
    "momentum_3m": "selected_etf_momentum_3m",
    "momentum_6m": "selected_etf_momentum_6m",
    "volatility_1m": "selected_etf_volatility_1m",
    "volatility_3m": "selected_etf_volatility_3m",
    "volatility_6m": "selected_etf_volatility_6m",
    "downside_volatility_3m": "selected_etf_downside_volatility_3m",
    "market_beta_6m": "selected_etf_market_beta_6m",
    "max_drawdown_6m": "selected_etf_max_drawdown_6m",
    "momentum_12m": "selected_etf_momentum_12m",
    "momentum_12m_skip1m": "selected_etf_momentum_12m_skip1m",
    "relative_strength_3m": "selected_etf_relative_strength_3m",
    "relative_strength_6m": "selected_etf_relative_strength_6m",
    "drawdown_3m": "selected_etf_drawdown_3m",
    "drawdown_6m": "selected_etf_drawdown_6m",
    "ulcer_index_6m": "selected_etf_ulcer_index_6m",
    "amount_acceleration_1m_6m": "selected_etf_amount_acceleration_1m_6m",
    "amount_crowding_percentile_3y": "selected_etf_amount_crowding_percentile_3y",
    "historical_cvar_5pct_3m": "selected_etf_historical_cvar_5pct_3m",
    "maximum_daily_loss_3m": "selected_etf_maximum_daily_loss_3m",
    "negative_day_ratio_3m": "selected_etf_negative_day_ratio_3m",
    "days_since_high_6m": "selected_etf_days_since_high_6m",
    "volatility_acceleration_1m_3m": "selected_etf_volatility_acceleration_1m_3m",
}


def selected_score_diagnostics(
    scores: dict[str, float],
    observations: list[dict[str, Any]],
    eligible: set[str],
    snapshot: date,
) -> dict[str, float]:
    """Describe the actual top-scored ETF using only snapshot-known fields."""

    ordered = sorted(
        (
            (str(code), float(score))
            for code, score in scores.items()
            if str(code) in eligible and math.isfinite(float(score))
        ),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )
    output = {
        "selector_score_top1": ordered[0][1] if ordered else 0.0,
        "selector_score_margin": (
            ordered[0][1] - ordered[1][1] if len(ordered) >= 2 else 0.0
        ),
        "selector_score_dispersion": (
            statistics.pstdev(score for _code, score in ordered)
            if len(ordered) >= 2
            else 0.0
        ),
        "selector_score_candidate_count": float(len(ordered)),
    }
    if not ordered:
        return output
    selected_code = ordered[0][0]
    selected_row = next(
        (
            row
            for row in observations
            if str(row.get("ts_code")) == selected_code
            and date.fromisoformat(str(row["snapshot"])) == snapshot
        ),
        None,
    )
    if selected_row is None:
        return output
    for source, target in SELECTED_ETF_DIAGNOSTIC_FIELDS.items():
        raw = selected_row.get(source)
        if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
            output[target] = float(raw)
    return output


def direct_selector_diagnostics(
    metas_by_index: dict[str, list[EquityEtfMeta]],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    policy: DirectEtfSelectorPolicy,
) -> dict[str, float]:
    """Point-in-time score concentration for the static v2/v3 selectors."""

    cache_key = (id(metas_by_index), id(series), policy.name, snapshot)
    cached = _DIRECT_DIAGNOSTICS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    if (
        "weighted_stable_v9_structural_repair" in policy.name
        or "weighted_stable_v9_structural_flow_repair" in policy.name
        or "weighted_stable_v9_structural_mombreadth_repair" in policy.name
        or "weighted_stable_v9_structural_groupbreadth_repair" in policy.name
        or "weighted_stable_v9_structural_reflation_repair" in policy.name
        or "weighted_stable_v9_structural_conditional_repair" in policy.name
        or "weighted_stable_v9_structural_multistate_repair" in policy.name
        or "weighted_stable_v9_structural_latecycle_repair" in policy.name
        or "weighted_stable_v9_structural_policycat_repair" in policy.name
        or "weighted_stable_v9_structural_cooling_repair" in policy.name
        or "weighted_stable_v9_structural_resilience_repair" in policy.name
    ):
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v9_scores(observations, snapshot)
    elif "weighted_stable_v10_b050_v500" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        roe_weight = 0.75 if "roe075" in policy.name else 1.0
        scores = weighted_stable_combo_v10_scores(
            observations, snapshot, roe_weight
        )
    elif "weighted_stable_v9_roe050" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v9_scores(observations, snapshot)
    elif "structural_mainline" in policy.name:
        observations = load_candidate_observations(SHARE_V5_DATASET)
        scores = weighted_structural_mainline_scores(observations, snapshot)
    elif "weighted_stable_v7_flow" in policy.name:
        observations = load_candidate_observations(SHARE_V5_DATASET)
        flow_weight = 0.10 if "flow010" in policy.name else 0.50
        scores = weighted_stable_combo_v7_scores(
            observations, snapshot, flow_weight
        )
    elif "weighted_stable_v6_top1" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v6_scores(observations, snapshot)
    elif "weighted_stable_v5_top1" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v5_scores(observations, snapshot)
    elif "weighted_stable_v4_top1" in policy.name:
        observations = load_candidate_observations(CONSTITUENT_V4_DATASET)
        scores = weighted_stable_combo_v4_scores(observations, snapshot)
    elif "weighted_stable_v3_top1" in policy.name:
        observations = load_candidate_observations(FUNDAMENTAL_V3_DATASET)
        scores = weighted_stable_combo_v3_scores(observations, snapshot)
    elif "weighted_stable_v2_top1" in policy.name:
        observations = load_candidate_observations(ENRICHED_V2_DATASET)
        scores = weighted_stable_combo_v2_scores(observations, snapshot)
    else:
        return {}
    eligible = {
        meta.code
        for values in metas_by_index.values()
        for meta in values
        if meta.list_date <= snapshot
        and meta.first_trade_date <= snapshot
        and has_recent_etf_price(series, meta.code, snapshot)
    }
    output = selected_score_diagnostics(scores, observations, eligible, snapshot)
    _DIRECT_DIAGNOSTICS_CACHE[cache_key] = dict(output)
    return output


def _percentile(values: dict[str, float], *, higher_is_better: bool = True) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = max(len(ordered) - 1, 1)
    ranks = {code: index / denominator for index, (code, _value) in enumerate(ordered)}
    return ranks if higher_is_better else {code: 1.0 - rank for code, rank in ranks.items()}


def _finite_row_float(row: dict[str, object], key: str, default: float) -> float:
    raw = row.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def structural_theme_groups_from_metas(
    metas_by_index: dict[str, list[EquityEtfMeta]],
) -> dict[str, str]:
    cache_key = id(metas_by_index)
    cached = _STRUCTURAL_THEME_GROUPS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    output = {
        meta.code: structural_theme_group_for_text(f"{meta.name} {meta.index_name}")
        for values in metas_by_index.values()
        for meta in values
    }
    _STRUCTURAL_THEME_GROUPS_CACHE[cache_key] = dict(output)
    return output


def structural_subtheme_groups_from_metas(
    metas_by_index: dict[str, list[EquityEtfMeta]],
) -> dict[str, str]:
    cache_key = id(metas_by_index)
    cached = _STRUCTURAL_SUBTHEME_GROUPS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    output = {
        meta.code: structural_subtheme_group_for_text(f"{meta.name} {meta.index_name}")
        for values in metas_by_index.values()
        for meta in values
    }
    _STRUCTURAL_SUBTHEME_GROUPS_CACHE[cache_key] = dict(output)
    return output


def structural_finance_substyles_from_metas(
    metas_by_index: dict[str, list[EquityEtfMeta]],
) -> dict[str, str]:
    cache_key = id(metas_by_index)
    cached = _STRUCTURAL_FINANCE_SUBSTYLE_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    output = {
        meta.code: structural_finance_substyle_for_text(
            f"{meta.name} {meta.index_name}"
        )
        for values in metas_by_index.values()
        for meta in values
    }
    _STRUCTURAL_FINANCE_SUBSTYLE_CACHE[cache_key] = dict(output)
    return output


def structural_resource_bank_catchup_styles_from_metas(
    metas_by_index: dict[str, list[EquityEtfMeta]],
) -> dict[str, str]:
    cache_key = id(metas_by_index)
    cached = _STRUCTURAL_RESOURCE_BANK_STYLE_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    output = {
        meta.code: structural_resource_bank_catchup_style_for_text(
            f"{meta.name} {meta.index_name}"
        )
        for values in metas_by_index.values()
        for meta in values
    }
    _STRUCTURAL_RESOURCE_BANK_STYLE_CACHE[cache_key] = dict(output)
    return output


def crowded_growth_exhaustion_fallback_active(
    observations: list[dict[str, object]],
    snapshot: date,
    groups_by_code: dict[str, str],
    market_state: dict[str, object] | None,
) -> bool:
    """Point-in-time trigger for broad-growth exhaustion with narrow local strength."""

    if not crowded_growth_exhaustion_market_setup_active(market_state):
        return False
    for row in observations:
        if date.fromisoformat(str(row["snapshot"])) != snapshot:
            continue
        code = str(row["ts_code"])
        if groups_by_code.get(code) != "broad_growth":
            continue
        if (
            _finite_row_float(row, "momentum_3m", -1.0) >= 0.15
            and _finite_row_float(row, "amount_crowding_percentile_3y", 0.0) >= 0.85
            and _finite_row_float(row, "maximum_daily_loss_3m", 0.0) <= -0.075
        ):
            return True
    return False


def crowded_growth_exhaustion_market_setup_active(
    market_state: dict[str, object] | None,
) -> bool:
    if not market_state:
        return False
    required = (
        "cs300_return_3m",
        "basket_return_3m_max",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
        "selector_score_margin",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    strong_crisis = bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )
    if strong_crisis:
        return False
    return (
        float(market_state["cs300_return_3m"]) < 0.0
        and float(market_state["basket_return_3m_max"]) >= 0.10
        and 0.20 <= float(market_state["breadth_return_3m_positive"]) <= 0.60
        and float(market_state["basket_drawdown_6m"]) > -0.16
        and float(market_state["selector_score_margin"]) >= 0.03
    )


def _series_return_to_snapshot(
    rows: list[tuple[date, float]],
    snapshot: date,
    observations: int,
) -> float | None:
    end = bisect_right(rows, (snapshot, math.inf))
    if end <= observations:
        return None
    if (snapshot - rows[end - 1][0]).days > MAX_ETF_PRICE_STALENESS_DAYS:
        return None
    start = rows[end - observations - 1][1]
    finish = rows[end - 1][1]
    return finish / start - 1.0 if start > 0 else None


def _series_volatility_to_snapshot(
    rows: list[tuple[date, float]],
    snapshot: date,
    observations: int,
) -> float | None:
    end = bisect_right(rows, (snapshot, math.inf))
    if end <= observations:
        return None
    window = rows[end - observations - 1 : end]
    daily_returns = [
        current[1] / previous[1] - 1.0
        for previous, current in zip(window[:-1], window[1:])
        if previous[1] > 0
    ]
    if len(daily_returns) < 20:
        return None
    return statistics.pstdev(daily_returns) * math.sqrt(252.0)


def _series_drawdown_to_snapshot(
    rows: list[tuple[date, float]],
    snapshot: date,
    observations: int,
) -> float | None:
    end = bisect_right(rows, (snapshot, math.inf))
    if end < observations:
        return None
    prices = [value for _day, value in rows[end - observations : end]]
    high = max(prices) if prices else 0.0
    return prices[-1] / high - 1.0 if high > 0 else None


def price_value_exhaustion_fallback_weights(
    metas_by_index: dict[str, list[EquityEtfMeta]],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    *,
    top_n: int = 3,
) -> dict[str, float]:
    """Select low-volatility domestic value proxies from ETF prices only."""

    groups_by_code = structural_theme_groups_from_metas(metas_by_index)
    metas = {meta.code: meta for values in metas_by_index.values() for meta in values}
    metrics: dict[str, dict[str, float]] = {}
    for code, meta in metas.items():
        if groups_by_code.get(code) not in {"broad_value", "finance", "industrial"}:
            continue
        if meta.list_date > snapshot or meta.first_trade_date > snapshot:
            continue
        if not has_recent_etf_price(series, code, snapshot):
            continue
        category = classify_defensive_etf(code, meta.name, meta.index_name)
        if category in {"bond", "gold"}:
            continue
        rows = series.get(code) or []
        m1 = _series_return_to_snapshot(rows, snapshot, 21)
        m3 = _series_return_to_snapshot(rows, snapshot, 63)
        m6 = _series_return_to_snapshot(rows, snapshot, 126)
        vol = _series_volatility_to_snapshot(rows, snapshot, 63)
        drawdown = _series_drawdown_to_snapshot(rows, snapshot, 63)
        if None in (m1, m3, m6, vol, drawdown):
            continue
        metrics[code] = {
            "m1": float(m1),
            "m3": float(m3),
            "m6": float(m6),
            "vol": float(vol),
            "drawdown": float(drawdown),
        }
    if not metrics:
        return {}
    ranks = {
        "m1": _percentile({code: item["m1"] for code, item in metrics.items()}, higher_is_better=False),
        "m3": _percentile({code: item["m3"] for code, item in metrics.items()}, higher_is_better=False),
        "m6": _percentile({code: item["m6"] for code, item in metrics.items()}),
        "vol": _percentile({code: item["vol"] for code, item in metrics.items()}, higher_is_better=False),
        "drawdown": _percentile({code: item["drawdown"] for code, item in metrics.items()}),
    }
    scores = {}
    for code in metrics:
        group_bonus = {
            "broad_value": 0.20,
            "finance": 0.08,
            "industrial": 0.04,
        }.get(groups_by_code.get(code), 0.0)
        scores[code] = (
            0.22 * ranks["m1"][code]
            + 0.18 * ranks["m3"][code]
            + 0.10 * ranks["m6"][code]
            + 0.28 * ranks["vol"][code]
            + 0.22 * ranks["drawdown"][code]
            + group_bonus
        )
    selected = sorted(
        scores,
        key=lambda code: (round(scores[code], 12), code),
        reverse=True,
    )[:top_n]
    powered = {code: max(scores[code], 0.01) ** 2.0 for code in selected}
    total = sum(powered.values())
    return {code: value / total for code, value in powered.items()} if total > 0 else {}


def structural_price_cold_start_scores(
    metas_by_index: dict[str, list[EquityEtfMeta]],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    *,
    excluded_codes: set[str] | None = None,
    extra_allowed_subthemes: set[str] | None = None,
    allow_nonpositive_1m_subthemes: set[str] | None = None,
) -> dict[str, float]:
    """Price-only structural candidates for ETFs missing SHARE_V5 feature rows."""

    excluded_codes = excluded_codes or set()
    extra_allowed_subthemes = extra_allowed_subthemes or set()
    allow_nonpositive_1m_subthemes = allow_nonpositive_1m_subthemes or set()
    cache_key = (
        id(metas_by_index),
        id(series),
        snapshot,
        frozenset(excluded_codes),
        frozenset(extra_allowed_subthemes),
        frozenset(allow_nonpositive_1m_subthemes),
    )
    cached = _STRUCTURAL_PRICE_COLD_START_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    metas = {meta.code: meta for values in metas_by_index.values() for meta in values}
    groups_by_code = structural_theme_groups_from_metas(metas_by_index)
    subthemes_by_code = structural_subtheme_groups_from_metas(metas_by_index)
    allowed_groups = {
        "growth",
        "technology",
        "consumer",
        "industrial",
        "healthcare",
        "broad_growth",
    }
    preferred_subthemes = {
        "digital_hot",
        "semiconductor",
        "new_energy",
        "high_end_equipment",
        "healthcare",
    }
    metrics: dict[str, dict[str, float]] = {}
    for code, meta in metas.items():
        if code in excluded_codes:
            continue
        if meta.list_date > snapshot or meta.first_trade_date > snapshot:
            continue
        if classify_defensive_etf(code, meta.name, meta.index_name) is not None:
            continue
        if not has_recent_etf_price(series, code, snapshot):
            continue
        group = groups_by_code.get(code, "")
        subtheme = subthemes_by_code.get(code, "")
        if (
            group not in allowed_groups
            and subtheme not in preferred_subthemes
            and subtheme not in extra_allowed_subthemes
        ):
            continue
        rows = series.get(code) or []
        momentum_1m = _series_return_to_snapshot(rows, snapshot, 21)
        momentum_3m = _series_return_to_snapshot(rows, snapshot, 63)
        momentum_6m = _series_return_to_snapshot(rows, snapshot, 126)
        if momentum_1m is None:
            continue
        if momentum_1m <= 0.0 and not (
            subtheme in allow_nonpositive_1m_subthemes
            and (
                (momentum_3m is not None and momentum_3m > 0.0)
                or (momentum_6m is not None and momentum_6m > 0.0)
            )
        ):
            continue
        drawdown_3m = _series_drawdown_to_snapshot(rows, snapshot, 63)
        drawdown_6m = _series_drawdown_to_snapshot(rows, snapshot, 126)
        volatility_3m = _series_volatility_to_snapshot(rows, snapshot, 63)
        if drawdown_3m is not None and drawdown_3m < -0.18:
            continue
        if volatility_3m is not None and volatility_3m > 0.55:
            continue
        if (
            momentum_6m is not None
            and drawdown_6m is not None
            and volatility_3m is not None
            and momentum_6m > 0.40
            and drawdown_6m > -0.06
            and volatility_3m > 0.25
        ):
            continue
        if _cold_start_overheated_theme_blocked(
            subtheme,
            momentum_1m,
            momentum_3m,
            momentum_6m,
            drawdown_3m,
            drawdown_6m,
            volatility_3m,
        ):
            continue
        metrics[code] = {
            "momentum_1m": float(momentum_1m),
            "momentum_3m": float(momentum_3m) if momentum_3m is not None else 0.0,
            "momentum_6m": float(momentum_6m) if momentum_6m is not None else 0.0,
            "drawdown": float(
                drawdown_6m if drawdown_6m is not None else drawdown_3m or 0.0
            ),
            "volatility": float(volatility_3m) if volatility_3m is not None else 0.30,
            "subtheme_bonus": (
                0.08
                if subtheme in preferred_subthemes
                or subtheme in extra_allowed_subthemes
                else 0.0
            ),
        }
    if not metrics:
        _STRUCTURAL_PRICE_COLD_START_CACHE[cache_key] = {}
        return {}
    ranks = {
        "momentum_1m": _percentile(
            {code: item["momentum_1m"] for code, item in metrics.items()}
        ),
        "momentum_3m": _percentile(
            {code: item["momentum_3m"] for code, item in metrics.items()}
        ),
        "momentum_6m": _percentile(
            {code: item["momentum_6m"] for code, item in metrics.items()}
        ),
        "drawdown": _percentile(
            {code: item["drawdown"] for code, item in metrics.items()}
        ),
        "volatility": _percentile(
            {code: item["volatility"] for code, item in metrics.items()},
            higher_is_better=False,
        ),
    }
    output = {
        code: (
            0.42 * ranks["momentum_1m"][code]
            + 0.22 * ranks["momentum_3m"][code]
            + 0.12 * ranks["momentum_6m"][code]
            + 0.12 * ranks["drawdown"][code]
            + 0.12 * ranks["volatility"][code]
            + metrics[code]["subtheme_bonus"]
        )
        for code in metrics
    }
    _STRUCTURAL_PRICE_COLD_START_CACHE[cache_key] = dict(output)
    return output


def _cold_start_overheated_theme_blocked(
    subtheme: str,
    momentum_1m: float | None,
    momentum_3m: float | None,
    momentum_6m: float | None,
    drawdown_3m: float | None,
    drawdown_6m: float | None,
    volatility_3m: float | None,
) -> bool:
    if subtheme not in {"digital_hot", "semiconductor"}:
        return False

    shallow_drawdown_3m = drawdown_3m is None or drawdown_3m > -0.015
    shallow_drawdown_6m = drawdown_6m is None or drawdown_6m > -0.04
    high_volatility = volatility_3m is not None and volatility_3m > 0.24
    short_history = momentum_6m is None

    high_short_momentum = (
        momentum_1m is not None
        and momentum_1m >= 0.12
        and momentum_3m is not None
        and momentum_3m >= 0.24
        and shallow_drawdown_3m
        and high_volatility
    )
    short_history_extension = (
        short_history
        and momentum_1m is not None
        and momentum_1m >= 0.10
        and (momentum_3m is None or momentum_3m >= 0.12)
        and shallow_drawdown_3m
        and (volatility_3m is None or volatility_3m > 0.20)
    )
    medium_term_extension = (
        momentum_3m is not None
        and momentum_3m >= 0.28
        and (momentum_6m is None or momentum_6m >= 0.28)
        and shallow_drawdown_6m
        and high_volatility
    )
    return high_short_momentum or short_history_extension or medium_term_extension


def v9_structural_repair_weights(
    snapshot: date,
    repair_share: float,
    *,
    repair_score: str = "mainline",
    repair_top_n: int = 5,
    groups_by_code: dict[str, str] | None = None,
    subthemes_by_code: dict[str, str] | None = None,
    finance_substyles_by_code: dict[str, str] | None = None,
    resource_bank_styles_by_code: dict[str, str] | None = None,
    pure_structural: bool = False,
    cold_start_scores: dict[str, float] | None = None,
) -> dict[str, float]:
    """Mostly keep the V9 direct sleeve, with a small filtered mainline repair."""

    base_scores = weighted_stable_combo_v9_scores(
        load_candidate_observations(CONSTITUENT_V4_DATASET),
        snapshot,
    )
    if not base_scores:
        return {}
    base_code = max(base_scores, key=lambda code: (round(base_scores[code], 12), code))
    share_rows = current_candidate_observations(SHARE_V5_DATASET, snapshot)
    if repair_score == "liquidity_flow":
        structural_scores = weighted_structural_liquidity_flow_scores(
            share_rows,
            snapshot,
        )
    elif repair_score == "momentum_breadth":
        structural_scores = weighted_structural_momentum_breadth_scores(
            share_rows,
            snapshot,
        )
    elif repair_score == "liquidity_group_breadth":
        structural_scores = weighted_structural_liquidity_group_breadth_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
        )
    elif repair_score == "reflation_rotation":
        structural_scores = weighted_structural_reflation_rotation_scores(
            share_rows,
            snapshot,
        )
    elif repair_score == "value_reflation_mainline":
        structural_scores = weighted_structural_value_reflation_mainline_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "conditional_rotation":
        structural_scores = weighted_structural_conditional_rotation_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
        )
    elif repair_score == "multistate_rotation":
        structural_scores = weighted_structural_multistate_rotation_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "late_cycle_defensive_rotation":
        structural_scores = weighted_structural_late_cycle_defensive_rotation_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "finance_defensive_rotation":
        structural_scores = weighted_structural_finance_defensive_rotation_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "finance_catchup":
        structural_scores = weighted_structural_finance_catchup_scores(
            share_rows,
            snapshot,
            subthemes_by_code or {},
        )
    elif repair_score == "finance_bank_catchup":
        structural_scores = weighted_structural_finance_bank_catchup_scores(
            share_rows,
            snapshot,
            subthemes_by_code or {},
            finance_substyles_by_code or {},
        )
    elif repair_score == "finance_resource_catchup":
        structural_scores = weighted_structural_finance_resource_catchup_scores(
            share_rows,
            snapshot,
            subthemes_by_code or {},
            finance_substyles_by_code or {},
        )
    elif repair_score == "resource_bank_catchup":
        structural_scores = weighted_structural_resource_bank_catchup_scores(
            share_rows,
            snapshot,
            resource_bank_styles_by_code or {},
        )
    elif repair_score == "late_cycle_small_growth_recovery":
        structural_scores = weighted_structural_late_cycle_small_growth_recovery_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "late_cycle_tech_pullback_continuation":
        structural_scores = weighted_structural_late_cycle_tech_pullback_continuation_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "new_energy_pullback_restart":
        structural_scores = weighted_structural_new_energy_pullback_restart_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "local_mainline_pullback_reentry":
        structural_scores = weighted_structural_local_mainline_pullback_reentry_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "late_cycle_policy_catalyst_rotation":
        structural_scores = weighted_structural_late_cycle_policy_catalyst_scores(
            share_rows,
            snapshot,
            groups_by_code or {},
            subthemes_by_code or {},
        )
    elif repair_score == "cooling_rotation":
        structural_scores = weighted_structural_cooling_rotation_scores(
            share_rows,
            snapshot,
            subthemes_by_code or {},
        )
    elif repair_score == "resilience":
        structural_scores = weighted_structural_resilience_scores(
            share_rows,
            snapshot,
        )
    else:
        structural_scores = weighted_structural_mainline_scores(
            share_rows,
            snapshot,
        )
    if cold_start_scores:
        if repair_score == "resource_bank_catchup":
            cold_start_scores = {
                code: score
                for code, score in cold_start_scores.items()
                if (resource_bank_styles_by_code or {}).get(code)
                in {"resources", "bank"}
            }
        structural_scores = {**structural_scores, **cold_start_scores}
    new_energy_restart_active = (
        repair_score == "new_energy_pullback_restart"
        and structural_new_energy_pullback_restart_active(
            share_rows,
            snapshot,
            subthemes_by_code or {},
        )
    )
    local_mainline_reentry_active = (
        repair_score
        in {
            "late_cycle_tech_pullback_continuation",
            "local_mainline_pullback_reentry",
        }
        and structural_local_mainline_pullback_reentry_active(
            share_rows,
            snapshot,
            subthemes_by_code or {},
        )
    )
    use_resilience_filter = repair_score in {
        "resilience",
        "cooling_rotation",
        "value_reflation_mainline",
        "finance_catchup",
        "finance_bank_catchup",
        "finance_resource_catchup",
        "resource_bank_catchup",
        "new_energy_pullback_restart",
        "local_mainline_pullback_reentry",
    } or (
        repair_score in {
            "conditional_rotation",
            "multistate_rotation",
            "late_cycle_defensive_rotation",
            "finance_defensive_rotation",
            "late_cycle_small_growth_recovery",
            "late_cycle_tech_pullback_continuation",
            "late_cycle_policy_catalyst_rotation",
        }
        and structural_healthcare_leadership_active(
            share_rows,
            snapshot,
            groups_by_code or {},
        )
    ) or new_energy_restart_active or local_mainline_reentry_active
    eligible = {}
    for row in share_rows:
        code = str(row["ts_code"])
        if code not in structural_scores:
            continue
        if use_resilience_filter:
            drawdown_6m_floor = (
                -0.30
                if (
                    local_mainline_reentry_active
                    and structural_local_mainline_pullback_reentry_candidate(row)
                )
                else -0.20
            )
            blocked = (
                _finite_row_float(row, "drawdown_6m", -1.0) < drawdown_6m_floor
                or _finite_row_float(row, "maximum_daily_loss_3m", -1.0) < -0.12
                or _finite_row_float(row, "historical_cvar_5pct_3m", -1.0) < -0.08
                or _finite_row_float(row, "amount_crowding_percentile_3y", 0.0) > 0.995
            )
        else:
            blocked = (
                _finite_row_float(row, "drawdown_6m", -1.0) < -0.20
                or _finite_row_float(row, "maximum_daily_loss_3m", -1.0) < -0.07
                or _finite_row_float(row, "historical_cvar_5pct_3m", -1.0) < -0.035
                or _finite_row_float(row, "amount_crowding_percentile_3y", 0.0) > 0.90
            )
        if blocked:
            continue
        eligible[code] = structural_scores[code]
    if cold_start_scores:
        for code, score in cold_start_scores.items():
            if code not in eligible:
                eligible[code] = score
    repair_codes = sorted(
        eligible,
        key=lambda code: (round(eligible[code], 12), code),
        reverse=True,
    )[:repair_top_n]
    if not repair_codes or repair_share <= 0:
        return {base_code: 1.0}
    repair_weights = {
        code: max(eligible[code], 0.01) ** 2.0 for code in repair_codes
    }
    total_repair = sum(repair_weights.values())
    if pure_structural and total_repair > 0:
        return {
            code: weight / total_repair
            for code, weight in repair_weights.items()
        }
    output = {base_code: 1.0 - repair_share}
    for code, weight in repair_weights.items():
        output[code] = output.get(code, 0.0) + repair_share * weight / total_repair
    total = sum(output.values())
    return {code: weight / total for code, weight in output.items()}


def structural_opportunity_active(market_state: dict[str, object] | None) -> bool:
    if not market_state:
        return False
    required = (
        "cs300_return_3m",
        "basket_return_3m_dispersion",
        "basket_return_3m_max",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    strong_crisis = bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )
    return (
        float(market_state["cs300_return_3m"]) < 0.05
        and float(market_state["basket_return_3m_dispersion"]) >= 0.08
        and float(market_state["basket_return_3m_max"]) >= 0.08
        and float(market_state["breadth_return_3m_positive"]) >= 0.50
        and float(market_state["basket_drawdown_6m"]) > -0.20
        and not strong_crisis
    )


def wide_structural_opportunity_active(market_state: dict[str, object] | None) -> bool:
    if not market_state:
        return False
    required = (
        "cs300_return_3m",
        "basket_return_3m_dispersion",
        "basket_return_3m_max",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    strong_crisis = bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )
    return (
        float(market_state["cs300_return_3m"]) < 0.16
        and float(market_state["basket_return_3m_dispersion"]) >= 0.03
        and float(market_state["basket_return_3m_max"]) >= 0.06
        and float(market_state["breadth_return_3m_positive"]) >= 0.50
        and float(market_state["basket_drawdown_6m"]) > -0.12
        and not strong_crisis
    )


def broad_participation_rotation_active(market_state: dict[str, object] | None) -> bool:
    if not market_state:
        return False
    required = (
        "cs300_return_3m",
        "basket_return_3m_max",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
        "selected_etf_momentum_3m",
        "selector_score_margin",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    strong_crisis = bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )
    return (
        float(market_state["cs300_return_3m"]) < 0.12
        and float(market_state["basket_return_3m_max"]) >= 0.10
        and float(market_state["breadth_return_3m_positive"]) >= 0.80
        and float(market_state["basket_drawdown_6m"]) > -0.05
        and float(market_state["selected_etf_momentum_3m"]) >= 0.02
        and float(market_state["selector_score_margin"]) >= 0.01
        and not strong_crisis
    )


def rotation_structural_opportunity_active(market_state: dict[str, object] | None) -> bool:
    return wide_structural_opportunity_active(market_state) or broad_participation_rotation_active(market_state)


def pure_structural_rotation_active(market_state: dict[str, object] | None) -> bool:
    """Point-in-time guard for replacing the repair sleeve with pure structure."""

    if not market_state:
        return False
    required = (
        "basket_return_3m_max",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
        "basket_vol_3m",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    strong_crisis = bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )
    return (
        float(market_state["basket_return_3m_max"]) >= 0.08
        and float(market_state["breadth_return_3m_positive"]) >= 0.50
        and float(market_state["basket_drawdown_6m"]) > -0.08
        and float(market_state["basket_vol_3m"]) <= 0.24
        and not strong_crisis
    )


def shock_resilience_opportunity_active(market_state: dict[str, object] | None) -> bool:
    if not market_state:
        return False
    required = (
        "cs300_return_3m",
        "basket_return_3m_dispersion",
        "basket_return_3m_max",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    strong_crisis = bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )
    return (
        float(market_state["cs300_return_3m"]) < 0.08
        and float(market_state["basket_return_3m_dispersion"]) >= 0.04
        and float(market_state["basket_return_3m_max"]) >= 0.12
        and float(market_state["breadth_return_3m_positive"]) >= 0.80
        and -0.12 < float(market_state["basket_drawdown_6m"]) <= -0.05
        and not strong_crisis
    )


def early_structural_opportunity_active(market_state: dict[str, object] | None) -> bool:
    if not market_state:
        return False
    required = (
        "cs300_return_6m",
        "basket_return_6m_dispersion",
        "basket_return_6m_max",
        "basket_excess_return_6m",
        "breadth_return_3m_positive",
        "basket_drawdown_6m",
    )
    if any(market_state.get(name) is None for name in required):
        return False
    strong_crisis = bool(
        market_state.get("crisis_continuation_flag")
        or market_state.get("domestic_liquidity_stress_flag")
        or market_state.get("credit_contraction_tightening_flag")
    )
    return (
        float(market_state["cs300_return_6m"]) < 0.02
        and float(market_state["basket_return_6m_dispersion"]) >= 0.10
        and float(market_state["basket_return_6m_max"]) >= 0.25
        and float(market_state["basket_excess_return_6m"]) >= 0.05
        and float(market_state["breadth_return_3m_positive"]) >= 0.80
        and float(market_state["basket_drawdown_6m"]) > -0.12
        and not strong_crisis
    )


def structural_repair_share_from_policy(
    policy_name: str,
    market_state: dict[str, object] | None = None,
) -> float:
    marker = None
    for candidate in (
        "_structural_resilience_repair_top",
        "_structural_mombreadth_repair_top",
        "_structural_groupbreadth_repair_top",
        "_structural_reflation_repair_top",
        "_structural_conditional_repair_top",
        "_structural_multistate_repair_top",
        "_structural_latecycle_techpullback_repair_top",
        "_structural_latecycle_sgrowth_repair_top",
        "_structural_latecycle_repair_top",
        "_structural_policycat_repair_top",
        "_structural_cooling_repair_top",
        "_structural_flow_repair_top",
        "_structural_repair_top",
    ):
        if candidate in policy_name:
            marker = candidate
            break
    if marker is None:
        return 0.05
    top_suffix = policy_name.split(marker, 1)[1]
    if "_s" not in top_suffix:
        return 0.05
    suffix = top_suffix.split("_s", 1)[1].split("_", 1)[0]
    try:
        base_share = max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        base_share = 0.05
    if "_shockcond" in policy_name:
        if not shock_resilience_opportunity_active(market_state):
            return base_share
        cond_suffix = policy_name.split("_shockcond", 1)[1].split("_", 1)[0]
    elif "_earlyrotcond" in policy_name:
        if not broad_participation_rotation_active(market_state):
            return base_share
        cond_suffix = policy_name.split("_earlyrotcond", 1)[1].split("_", 1)[0]
    elif "_rotcond" in policy_name:
        if not rotation_structural_opportunity_active(market_state):
            return base_share
        cond_suffix = policy_name.split("_rotcond", 1)[1].split("_", 1)[0]
    elif "_widecond" in policy_name:
        if not wide_structural_opportunity_active(market_state):
            return base_share
        cond_suffix = policy_name.split("_widecond", 1)[1].split("_", 1)[0]
    elif "_cond" in policy_name:
        if not structural_opportunity_active(market_state):
            return base_share
        cond_suffix = policy_name.split("_cond", 1)[1].split("_", 1)[0]
    else:
        return base_share
    try:
        return max(base_share, min(float(int(cond_suffix)) / 100.0, 1.0))
    except ValueError:
        return base_share


def structural_repair_top_n_from_policy(policy_name: str) -> int:
    marker = "_repair_top"
    if marker not in policy_name:
        return 5
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(1, int(suffix))
    except ValueError:
        return 5


def shock_resilience_share_from_policy(policy_name: str) -> float | None:
    marker = "_shockres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def early_resilience_share_from_policy(policy_name: str) -> float | None:
    marker = "_earlyres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def healthcare_resilience_share_from_policy(policy_name: str) -> float | None:
    marker = "_hcres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def healthcare_direct_blend_share_from_policy(policy_name: str) -> float | None:
    marker = "_hcblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def local_mainline_repair_share_from_policy(policy_name: str) -> float | None:
    marker = "_lmres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def digital_rotation_repair_share_from_policy(policy_name: str) -> float | None:
    marker = "_drotres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_defensive_repair_share_from_policy(policy_name: str) -> float | None:
    marker = "_findefres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def value_reflation_repair_share_from_policy(policy_name: str) -> float | None:
    marker = "_valres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_catchup_repair_share_from_policy(policy_name: str) -> float | None:
    marker = "_fcres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_bank_catchup_repair_share_from_policy(policy_name: str) -> float | None:
    marker = "_fcbankres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_resource_catchup_repair_share_from_policy(
    policy_name: str,
) -> float | None:
    marker = "_finres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def resource_bank_catchup_repair_share_from_policy(policy_name: str) -> float | None:
    marker = "_rbres"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_catchup_market_confirmation_active(
    market_state: dict[str, object] | None,
) -> bool:
    """Confirm finance catch-up after broad strength but before crisis damage."""

    state = market_state or {}

    def number(name: str) -> float | None:
        raw = state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    pboc_tone = number("pboc_outlook_net_tone")
    m1_m2_scissors_change = number("domestic_m1_m2_scissors_change_3m")
    cs300_return_3m = number("cs300_return_3m")
    basket_return_1m = number("basket_return_1m")
    breadth_return_1m_positive = number("breadth_return_1m_positive")
    basket_drawdown_6m = number("basket_drawdown_6m")
    basket_vol_3m = number("basket_vol_3m")
    return bool(
        pboc_tone is not None
        and pboc_tone >= 20.0
        and m1_m2_scissors_change is not None
        and m1_m2_scissors_change >= 0.0
        and cs300_return_3m is not None
        and 0.08 <= cs300_return_3m <= 0.25
        and basket_return_1m is not None
        and basket_return_1m >= 0.08
        and breadth_return_1m_positive is not None
        and breadth_return_1m_positive >= 0.80
        and basket_drawdown_6m is not None
        and basket_drawdown_6m > -0.08
        and basket_vol_3m is not None
        and basket_vol_3m <= 0.35
    )


def finance_breadth_rotation_market_confirmation_active(
    market_state: dict[str, object] | None,
) -> bool:
    """Confirm low-volatility finance/value rotation without policy support."""

    state = market_state or {}

    def number(name: str) -> float | None:
        raw = state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    cs300_return_3m = number("cs300_return_3m")
    cs300_return_6m = number("cs300_return_6m")
    basket_return_1m = number("basket_return_1m")
    basket_return_3m_max = number("basket_return_3m_max")
    breadth_return_1m_positive = number("breadth_return_1m_positive")
    breadth_return_3m_positive = number("breadth_return_3m_positive")
    basket_drawdown_6m = number("basket_drawdown_6m")
    basket_vol_3m = number("basket_vol_3m")
    return bool(
        cs300_return_3m is not None
        and 0.04 <= cs300_return_3m <= 0.12
        and cs300_return_6m is not None
        and 0.08 <= cs300_return_6m <= 0.24
        and basket_return_1m is not None
        and basket_return_1m >= 0.02
        and basket_return_3m_max is not None
        and basket_return_3m_max >= 0.10
        and breadth_return_1m_positive is not None
        and breadth_return_1m_positive >= 0.85
        and breadth_return_3m_positive is not None
        and breadth_return_3m_positive >= 0.85
        and basket_drawdown_6m is not None
        and basket_drawdown_6m > -0.04
        and basket_vol_3m is not None
        and basket_vol_3m <= 0.16
        and not state.get("crisis_continuation_flag")
        and not state.get("domestic_liquidity_stress_flag")
        and not state.get("credit_contraction_tightening_flag")
    )


def value_reflation_market_confirmation_active(
    market_state: dict[str, object] | None,
) -> bool:
    """Confirm value/reflation repair only in constructive, non-crisis setups."""

    state = market_state or {}

    def number(name: str) -> float | None:
        raw = state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    pboc_tone = number("pboc_outlook_net_tone")
    m1_m2_scissors_change = number("domestic_m1_m2_scissors_change_3m")
    cs300_return_3m = number("cs300_return_3m")
    basket_return_1m = number("basket_return_1m")
    breadth_return_1m_positive = number("breadth_return_1m_positive")
    basket_drawdown_6m = number("basket_drawdown_6m")
    basket_vol_3m = number("basket_vol_3m")
    return bool(
        pboc_tone is not None
        and pboc_tone >= 10.0
        and m1_m2_scissors_change is not None
        and m1_m2_scissors_change >= 0.0
        and cs300_return_3m is not None
        and cs300_return_3m >= -0.05
        and cs300_return_3m <= 0.05
        and basket_return_1m is not None
        and basket_return_1m >= 0.04
        and breadth_return_1m_positive is not None
        and breadth_return_1m_positive >= 0.70
        and basket_drawdown_6m is not None
        and basket_drawdown_6m > -0.10
        and basket_vol_3m is not None
        and basket_vol_3m <= 0.35
    )


def finance_defensive_market_confirmation_active(
    market_state: dict[str, object] | None,
) -> bool:
    """Confirm finance defensives only when broad-market leadership is muted."""

    state = market_state or {}

    def number(name: str) -> float | None:
        raw = state.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None

    pboc_tone = number("pboc_outlook_net_tone")
    m1_m2_scissors_change = number("domestic_m1_m2_scissors_change_3m")
    cs300_return_3m = number("cs300_return_3m")
    basket_return_1m = number("basket_return_1m")
    breadth_return_1m_positive = number("breadth_return_1m_positive")
    basket_drawdown_6m = number("basket_drawdown_6m")
    basket_vol_3m = number("basket_vol_3m")
    muted_broad_market = bool(
        cs300_return_3m is not None
        and cs300_return_3m <= 0.05
        and (
            basket_return_1m is None
            or breadth_return_1m_positive is None
            or basket_return_1m <= 0.0
            or breadth_return_1m_positive <= 0.50
        )
    )
    return bool(
        pboc_tone is not None
        and pboc_tone >= 10.0
        and m1_m2_scissors_change is not None
        and m1_m2_scissors_change >= 0.0
        and muted_broad_market
        and basket_drawdown_6m is not None
        and basket_drawdown_6m > -0.10
        and basket_vol_3m is not None
        and basket_vol_3m <= 0.35
    )


def local_mainline_direct_blend_share_from_policy(
    policy_name: str,
) -> float | None:
    marker = "_lmblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def digital_rotation_direct_blend_share_from_policy(policy_name: str) -> float | None:
    marker = "_drotblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def value_reflation_direct_blend_share_from_policy(policy_name: str) -> float | None:
    marker = "_valblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_catchup_direct_blend_share_from_policy(policy_name: str) -> float | None:
    marker = "_fcblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_bank_catchup_direct_blend_share_from_policy(
    policy_name: str,
) -> float | None:
    marker = "_fcbankblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def finance_resource_catchup_direct_blend_share_from_policy(
    policy_name: str,
) -> float | None:
    marker = "_finresblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def structural_direct_blend_share_from_policy(policy_name: str) -> float | None:
    marker = "_structblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def resource_bank_catchup_direct_blend_share_from_policy(
    policy_name: str,
) -> float | None:
    marker = "_rbblend"
    if marker not in policy_name:
        return None
    suffix = policy_name.split(marker, 1)[1].split("_", 1)[0]
    try:
        return max(0.0, min(float(int(suffix)) / 100.0, 1.0))
    except ValueError:
        return None


def direct_blend_share(
    policy: DirectEtfSelectorPolicy,
    market_state: dict[str, object],
    *,
    snapshot: date | None = None,
    groups_by_code: dict[str, str] | None = None,
    subthemes_by_code: dict[str, str] | None = None,
) -> float:
    """Choose a point-in-time blend share once per three-month holding window."""

    structural_share = structural_direct_blend_share_from_policy(policy.name)
    local_mainline_blend_share = local_mainline_direct_blend_share_from_policy(
        policy.name
    )
    if (
        local_mainline_blend_share is not None
        and snapshot is not None
        and subthemes_by_code is not None
        and structural_local_mainline_pullback_reentry_active(
            load_candidate_observations(SHARE_V5_DATASET),
            snapshot,
            subthemes_by_code,
        )
    ):
        return max(float(policy.direct_blend_weight), local_mainline_blend_share)
    digital_rotation_blend_share = digital_rotation_direct_blend_share_from_policy(
        policy.name
    )
    if (
        digital_rotation_blend_share is not None
        and snapshot is not None
        and subthemes_by_code is not None
        and structural_digital_blowoff_rotation_active(
            load_candidate_observations(SHARE_V5_DATASET),
            snapshot,
            subthemes_by_code,
        )
    ):
        return max(float(policy.direct_blend_weight), digital_rotation_blend_share)
    healthcare_blend_share = healthcare_direct_blend_share_from_policy(policy.name)
    if (
        healthcare_blend_share is not None
        and snapshot is not None
        and groups_by_code is not None
        and structural_healthcare_leadership_active(
            load_candidate_observations(SHARE_V5_DATASET),
            snapshot,
            groups_by_code,
        )
    ):
        return max(float(policy.direct_blend_weight), healthcare_blend_share)
    finance_bank_catchup_blend_share = (
        finance_bank_catchup_direct_blend_share_from_policy(policy.name)
    )
    if (
        finance_bank_catchup_blend_share is not None
        and snapshot is not None
        and subthemes_by_code is not None
        and finance_catchup_market_confirmation_active(market_state)
        and structural_finance_catchup_active(
            load_candidate_observations(SHARE_V5_DATASET),
            snapshot,
            subthemes_by_code,
        )
    ):
        return max(float(policy.direct_blend_weight), finance_bank_catchup_blend_share)
    finance_resource_catchup_blend_share = (
        finance_resource_catchup_direct_blend_share_from_policy(policy.name)
    )
    if (
        finance_resource_catchup_blend_share is not None
        and snapshot is not None
        and subthemes_by_code is not None
        and finance_catchup_market_confirmation_active(market_state)
        and structural_finance_catchup_active(
            load_candidate_observations(SHARE_V5_DATASET),
            snapshot,
            subthemes_by_code,
        )
    ):
        return max(
            float(policy.direct_blend_weight),
            finance_resource_catchup_blend_share,
        )
    resource_bank_catchup_blend_share = (
        resource_bank_catchup_direct_blend_share_from_policy(policy.name)
    )
    if (
        resource_bank_catchup_blend_share is not None
        and snapshot is not None
        and subthemes_by_code is not None
        and (
            finance_catchup_market_confirmation_active(market_state)
            or finance_breadth_rotation_market_confirmation_active(market_state)
        )
        and structural_finance_catchup_active(
            load_candidate_observations(SHARE_V5_DATASET),
            snapshot,
            subthemes_by_code,
        )
    ):
        return max(float(policy.direct_blend_weight), resource_bank_catchup_blend_share)
    finance_catchup_blend_share = finance_catchup_direct_blend_share_from_policy(
        policy.name
    )
    if (
        finance_catchup_blend_share is not None
        and snapshot is not None
        and subthemes_by_code is not None
        and finance_catchup_market_confirmation_active(market_state)
        and structural_finance_catchup_active(
            load_candidate_observations(SHARE_V5_DATASET),
            snapshot,
            subthemes_by_code,
        )
    ):
        return max(float(policy.direct_blend_weight), finance_catchup_blend_share)
    value_reflation_blend_share = value_reflation_direct_blend_share_from_policy(
        policy.name
    )
    if (
        value_reflation_blend_share is not None
        and value_reflation_market_confirmation_active(market_state)
    ):
        return max(float(policy.direct_blend_weight), value_reflation_blend_share)
    if (
        structural_share is not None
        and "_exhaustfallback" in policy.name
        and crowded_growth_exhaustion_market_setup_active(market_state)
    ):
        return max(float(policy.direct_blend_weight), structural_share)
    if structural_share is not None and rotation_structural_opportunity_active(market_state):
        return max(float(policy.direct_blend_weight), structural_share)

    if policy.name.startswith("blend_index_structural_mainline_") and "_cond_" in policy.name:
        return (
            float(policy.direct_blend_weight)
            if structural_opportunity_active(market_state)
            else 0.0
        )

    strong_weight = policy.strong_trend_blend_weight
    if strong_weight is None:
        return float(policy.direct_blend_weight)
    required = (
        "cs300_return_3m",
        "cs300_return_6m",
        "cs300_ma_6m_distance",
        "basket_drawdown_6m",
    )
    if any(market_state.get(name) is None for name in required):
        return float(policy.direct_blend_weight)
    strong_trend = (
        float(market_state["cs300_return_3m"])
        >= policy.strong_trend_return_3m_threshold
        and float(market_state["cs300_return_6m"])
        >= policy.strong_trend_return_6m_threshold
        and float(market_state["cs300_ma_6m_distance"])
        >= policy.strong_trend_ma_6m_distance_threshold
        and float(market_state["basket_drawdown_6m"])
        >= policy.strong_trend_basket_drawdown_6m_threshold
    )
    return float(strong_weight if strong_trend else policy.direct_blend_weight)


def _max_drawdown(values: list[float]) -> float:
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def select_direct_equity_etfs(
    metas_by_index: dict[str, list[EquityEtfMeta]],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    policy: DirectEtfSelectorPolicy,
    benchmark_series: list[tuple[date, float]] | None = None,
    market_state: dict[str, object] | None = None,
) -> dict[str, float]:
    repair_share = structural_repair_share_from_policy(policy.name, market_state)
    cache_key = (
        id(metas_by_index),
        id(series),
        id(benchmark_series),
        policy.name,
        snapshot,
        repair_share,
    )
    cached = _DIRECT_SELECTION_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    metas = {
        meta.code: meta
        for values in metas_by_index.values()
        for meta in values
        if has_recent_etf_price(series, meta.code, snapshot)
    }
    if policy.name == "direct_stable_combo_beta_autocorr_vol3_top1" or policy.name.startswith(
        "blend_index_static_top1_"
    ):
        weights = select_static_stable_combo(load_candidate_observations(), snapshot)
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_stable_combo_beta_distance_autocorr_vol3_top3" or policy.name.startswith(
        "blend_index_static_top3_"
    ):
        weights = select_static_stable_combo_top3(
            load_candidate_observations(), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("direct_supervised_"):
        weights = select_supervised_etfs(
            load_candidate_observations(),
            snapshot,
            supervised_policy_by_name(policy.name),
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_top3" or policy.name.startswith(
        "blend_index_weighted_stable_top3_"
    ):
        weights = select_weighted_stable_combo_top3(
            load_candidate_observations(), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_top1" or policy.name.startswith(
        "blend_index_weighted_stable_top1_"
    ):
        weights = select_weighted_stable_combo_top1(
            load_candidate_observations(), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v2_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v2_top1_"
    ):
        weights = select_weighted_stable_combo_v2_top1(
            load_candidate_observations(ENRICHED_V2_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v3_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v3_top1_"
    ):
        weights = select_weighted_stable_combo_v3_top1(
            load_candidate_observations(FUNDAMENTAL_V3_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v4_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v4_top1_"
    ):
        weights = select_weighted_stable_combo_v4_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("blend_index_weighted_stable_v9_structural_repair") or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_flow_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_mombreadth_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_groupbreadth_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_reflation_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_conditional_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_multistate_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_latecycle_sgrowth_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_latecycle_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_policycat_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_cooling_repair"
    ) or policy.name.startswith(
        "blend_index_weighted_stable_v9_structural_resilience_repair"
    ):
        if "structural_flow_repair" in policy.name:
            repair_score = "liquidity_flow"
        elif "structural_groupbreadth_repair" in policy.name:
            repair_score = "liquidity_group_breadth"
        elif "structural_reflation_repair" in policy.name:
            repair_score = "reflation_rotation"
        elif "structural_conditional_repair" in policy.name:
            repair_score = "conditional_rotation"
        elif "structural_multistate_repair" in policy.name:
            repair_score = "multistate_rotation"
        elif "structural_latecycle_techpullback_repair" in policy.name:
            repair_score = "late_cycle_tech_pullback_continuation"
        elif "structural_latecycle_sgrowth_repair" in policy.name:
            repair_score = "late_cycle_small_growth_recovery"
        elif "structural_latecycle_repair" in policy.name:
            repair_score = "late_cycle_defensive_rotation"
        elif "structural_policycat_repair" in policy.name:
            repair_score = "late_cycle_policy_catalyst_rotation"
        elif "structural_cooling_repair" in policy.name:
            repair_score = "cooling_rotation"
        elif "structural_mombreadth_repair" in policy.name:
            repair_score = "momentum_breadth"
        elif "structural_resilience_repair" in policy.name:
            repair_score = "resilience"
        else:
            repair_score = "mainline"
        shock_resilience_share = shock_resilience_share_from_policy(policy.name)
        if (
            shock_resilience_share is not None
            and shock_resilience_opportunity_active(market_state)
        ):
            repair_score = "resilience"
            repair_share = max(repair_share, shock_resilience_share)
        early_resilience_share = early_resilience_share_from_policy(policy.name)
        if (
            early_resilience_share is not None
            and early_structural_opportunity_active(market_state)
        ):
            repair_score = "resilience"
            repair_share = max(repair_share, early_resilience_share)
        groups_by_code = structural_theme_groups_from_metas(metas_by_index)
        subthemes_by_code = structural_subtheme_groups_from_metas(metas_by_index)
        finance_substyles_by_code = structural_finance_substyles_from_metas(
            metas_by_index
        )
        resource_bank_styles_by_code = (
            structural_resource_bank_catchup_styles_from_metas(metas_by_index)
        )
        share_rows = load_candidate_observations(SHARE_V5_DATASET)
        healthcare_resilience_share = healthcare_resilience_share_from_policy(policy.name)
        if (
            repair_score in {
                "conditional_rotation",
                "multistate_rotation",
                "late_cycle_defensive_rotation",
                "finance_defensive_rotation",
                "late_cycle_small_growth_recovery",
                "late_cycle_tech_pullback_continuation",
                "late_cycle_policy_catalyst_rotation",
            }
            and healthcare_resilience_share is not None
        ):
            if structural_healthcare_leadership_active(
                share_rows,
                snapshot,
                groups_by_code,
            ):
                repair_share = max(repair_share, healthcare_resilience_share)
        new_energy_restart_active = (
            repair_score == "new_energy_pullback_restart"
            and structural_new_energy_pullback_restart_active(
                share_rows,
                snapshot,
                subthemes_by_code,
            )
        )
        local_mainline_repair_share = local_mainline_repair_share_from_policy(
            policy.name
        )
        local_mainline_active = structural_local_mainline_pullback_reentry_active(
            share_rows,
            snapshot,
            subthemes_by_code,
        )
        if (
            repair_score
            in {"late_cycle_tech_pullback_continuation", "resilience"}
            and local_mainline_repair_share is not None
            and local_mainline_active
        ):
            repair_score = "local_mainline_pullback_reentry"
            repair_share = max(repair_share, local_mainline_repair_share)
        digital_rotation_repair_share = digital_rotation_repair_share_from_policy(
            policy.name
        )
        digital_rotation_active = structural_digital_blowoff_rotation_active(
            share_rows,
            snapshot,
            subthemes_by_code,
        )
        if (
            repair_score == "late_cycle_tech_pullback_continuation"
            and digital_rotation_repair_share is not None
            and digital_rotation_active
        ):
            repair_share = max(repair_share, digital_rotation_repair_share)
        value_reflation_active = value_reflation_market_confirmation_active(
            market_state
        )
        value_reflation_repair_share = value_reflation_repair_share_from_policy(
            policy.name
        )
        if (
            repair_score == "late_cycle_tech_pullback_continuation"
            and value_reflation_repair_share is not None
            and value_reflation_active
        ):
            repair_score = "value_reflation_mainline"
            repair_share = max(repair_share, value_reflation_repair_share)
        finance_catchup_active = (
            finance_catchup_market_confirmation_active(market_state)
            and structural_finance_catchup_active(
                share_rows,
                snapshot,
                subthemes_by_code,
            )
        )
        finance_catchup_repair_share = finance_catchup_repair_share_from_policy(
            policy.name
        )
        if (
            repair_score == "late_cycle_tech_pullback_continuation"
            and finance_catchup_repair_share is not None
            and finance_catchup_active
        ):
            repair_score = "finance_catchup"
            repair_share = max(repair_share, finance_catchup_repair_share)
        finance_bank_catchup_repair_share = (
            finance_bank_catchup_repair_share_from_policy(policy.name)
        )
        if (
            repair_score == "late_cycle_tech_pullback_continuation"
            and finance_bank_catchup_repair_share is not None
            and finance_catchup_active
        ):
            repair_score = "finance_bank_catchup"
            repair_share = max(repair_share, finance_bank_catchup_repair_share)
        finance_resource_catchup_repair_share = (
            finance_resource_catchup_repair_share_from_policy(policy.name)
        )
        if (
            repair_score == "late_cycle_tech_pullback_continuation"
            and finance_resource_catchup_repair_share is not None
            and finance_catchup_active
        ):
            repair_score = "finance_resource_catchup"
            repair_share = max(repair_share, finance_resource_catchup_repair_share)
        resource_bank_catchup_active = (
            finance_catchup_market_confirmation_active(market_state)
            and structural_finance_catchup_active(
                share_rows,
                snapshot,
                subthemes_by_code,
            )
        )
        resource_bank_catchup_repair_share = (
            resource_bank_catchup_repair_share_from_policy(policy.name)
        )
        if (
            repair_score == "late_cycle_tech_pullback_continuation"
            and resource_bank_catchup_repair_share is not None
            and resource_bank_catchup_active
        ):
            repair_score = "resource_bank_catchup"
            repair_share = max(repair_share, resource_bank_catchup_repair_share)
        finance_defensive_active = structural_finance_defensive_rotation_active(
            share_rows,
            snapshot,
            subthemes_by_code,
        ) and finance_defensive_market_confirmation_active(market_state)
        finance_defensive_repair_share = finance_defensive_repair_share_from_policy(
            policy.name
        )
        if (
            repair_score == "late_cycle_tech_pullback_continuation"
            and finance_defensive_repair_share is not None
            and finance_defensive_active
        ):
            repair_score = "finance_defensive_rotation"
            repair_share = max(repair_share, finance_defensive_repair_share)
        repair_top_n = structural_repair_top_n_from_policy(policy.name)
        if repair_score == "resilience":
            repair_top_n = max(repair_top_n, 5)
        if (
            "_exhaustfallback" in policy.name
            and crowded_growth_exhaustion_fallback_active(
                share_rows,
                snapshot,
                groups_by_code,
                market_state,
            )
            and not (
                "_purestructcond" in policy.name
                and pure_structural_rotation_active(market_state)
            )
        ):
            weights = price_value_exhaustion_fallback_weights(
                metas_by_index,
                series,
                snapshot,
                top_n=repair_top_n,
            )
        else:
            cold_start_scores = {}
            if "_coldstart" in policy.name:
                local_mainline_subthemes = (
                    structural_local_mainline_pullback_reentry_subthemes(
                        share_rows,
                        snapshot,
                        subthemes_by_code,
                    )
                    if repair_score == "local_mainline_pullback_reentry"
                    else set()
                )
                extra_allowed_subthemes = set()
                if digital_rotation_active:
                    extra_allowed_subthemes.update({"communication", "utilities"})
                if (
                    repair_score == "finance_defensive_rotation"
                    and finance_defensive_active
                ):
                    extra_allowed_subthemes.update(
                        {"finance", "resources", "consumer", "utilities"}
                    )
                if (
                    repair_score == "value_reflation_mainline"
                    and value_reflation_active
                ):
                    extra_allowed_subthemes.update(
                        {"finance", "resources"}
                    )
                if repair_score == "finance_catchup" and finance_catchup_active:
                    extra_allowed_subthemes.add("finance")
                if repair_score == "finance_bank_catchup" and finance_catchup_active:
                    extra_allowed_subthemes.add("finance")
                if (
                    repair_score == "finance_resource_catchup"
                    and finance_catchup_active
                ):
                    extra_allowed_subthemes.update({"finance", "resources"})
                if (
                    repair_score == "resource_bank_catchup"
                    and resource_bank_catchup_active
                ):
                    extra_allowed_subthemes.update({"finance", "resources"})
                if (
                    repair_score == "new_energy_pullback_restart"
                    and new_energy_restart_active
                ):
                    extra_allowed_subthemes.add("new_energy")
                if repair_score == "local_mainline_pullback_reentry":
                    extra_allowed_subthemes.update(local_mainline_subthemes)
                cold_start_scores = structural_price_cold_start_scores(
                    metas_by_index,
                    series,
                    snapshot,
                    excluded_codes={str(row["ts_code"]) for row in share_rows},
                    extra_allowed_subthemes=extra_allowed_subthemes,
                    allow_nonpositive_1m_subthemes=(
                        {"finance", "resources"}
                        if repair_score == "value_reflation_mainline"
                        else {"finance"}
                        if repair_score in {"finance_catchup", "finance_bank_catchup"}
                        else {"finance", "resources"}
                        if repair_score == "finance_resource_catchup"
                        else {"finance", "resources"}
                        if repair_score == "resource_bank_catchup"
                        else {"new_energy"}
                        if repair_score == "new_energy_pullback_restart"
                        else local_mainline_subthemes
                        if repair_score == "local_mainline_pullback_reentry"
                        else set()
                    ),
                )
                if (
                    repair_score == "new_energy_pullback_restart"
                    and new_energy_restart_active
                ):
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code) == "new_energy"
                    }
                elif repair_score == "local_mainline_pullback_reentry":
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code) in local_mainline_subthemes
                    }
                elif digital_rotation_active:
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code) in {"communication", "utilities"}
                    }
                elif (
                    repair_score == "finance_defensive_rotation"
                    and finance_defensive_active
                ):
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code)
                        in {"finance", "resources", "consumer", "utilities"}
                    }
                elif (
                    repair_score == "value_reflation_mainline"
                    and value_reflation_active
                ):
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code)
                        in {"finance", "resources"}
                    }
                elif repair_score == "finance_catchup" and finance_catchup_active:
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code) == "finance"
                    }
                elif (
                    repair_score == "finance_bank_catchup"
                    and finance_catchup_active
                ):
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code) == "finance"
                        and finance_substyles_by_code.get(code) == "bank_dividend"
                    }
                elif (
                    repair_score == "finance_resource_catchup"
                    and finance_catchup_active
                ):
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if subthemes_by_code.get(code) == "resources"
                        or (
                            subthemes_by_code.get(code) == "finance"
                            and finance_substyles_by_code.get(code)
                            == "bank_dividend"
                        )
                    }
                elif (
                    repair_score == "resource_bank_catchup"
                    and resource_bank_catchup_active
                ):
                    cold_start_scores = {
                        code: score
                        for code, score in cold_start_scores.items()
                        if resource_bank_styles_by_code.get(code)
                        in {"resources", "bank"}
                    }
            weights = v9_structural_repair_weights(
                snapshot,
                repair_share,
                repair_score=repair_score,
                repair_top_n=repair_top_n,
                groups_by_code=groups_by_code,
                subthemes_by_code=subthemes_by_code,
                finance_substyles_by_code=finance_substyles_by_code,
                resource_bank_styles_by_code=resource_bank_styles_by_code,
                pure_structural=(
                    "_purestruct" in policy.name
                    and (
                        "_purestructcond" not in policy.name
                        or pure_structural_rotation_active(market_state)
                    )
                ),
                cold_start_scores=cold_start_scores,
            )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("blend_index_weighted_stable_v10_b050_v500"):
        roe_weight = 0.75 if "roe075" in policy.name else 1.0
        weights = select_weighted_stable_combo_v10_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET),
            snapshot,
            roe_weight,
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("blend_index_weighted_stable_v9_roe050"):
        weights = select_weighted_stable_combo_v9_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("direct_structural_mainline_") or policy.name.startswith(
        "blend_index_structural_mainline_"
    ):
        selector = (
            select_weighted_structural_liquidity_flow_top5
            if "_flow_" in policy.name
            else select_weighted_structural_momentum_breadth_top3
            if "_mombreadth_" in policy.name
            else select_weighted_structural_resilience_top5
            if "_resilience_" in policy.name
            else
            select_weighted_structural_mainline_top5
            if "_top5" in policy.name
            else select_weighted_structural_mainline_top3
        )
        weights = selector(load_candidate_observations(SHARE_V5_DATASET), snapshot)
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v5_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v5_top1_"
    ):
        weights = select_weighted_stable_combo_v5_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("blend_index_weighted_stable_v7_flow"):
        flow_weight = 0.10 if "flow010" in policy.name else 0.50
        weights = select_weighted_stable_combo_v7_top1(
            load_candidate_observations(SHARE_V5_DATASET), snapshot, flow_weight
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name == "direct_weighted_stable_combo_v6_top1" or policy.name.startswith(
        "blend_index_weighted_stable_v6_top1_"
    ):
        weights = select_weighted_stable_combo_v6_top1(
            load_candidate_observations(CONSTITUENT_V4_DATASET), snapshot
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("direct_boost_rank_") or policy.name.startswith(
        "blend_index_boost_rank_"
    ):
        weights = select_boosted_etfs(
            load_candidate_observations(), snapshot, boosted_policy_by_name(policy.name)
        )
        weights = {
            code: weight
            for code, weight in weights.items()
            if code in metas
            and metas[code].list_date <= snapshot
            and metas[code].first_trade_date <= snapshot
            and bool(series.get(code))
        }
        total = sum(weights.values())
        weights = (
            {code: weight / total for code, weight in weights.items()}
            if total > 0
            else {}
        )
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    benchmark_returns: dict[date, float] = {}
    benchmark_end = 0
    if benchmark_series:
        benchmark_end = bisect_right(benchmark_series, (snapshot, math.inf))
        for previous, current in zip(
            benchmark_series[max(0, benchmark_end - 127) : benchmark_end - 1],
            benchmark_series[max(0, benchmark_end - 126) : benchmark_end],
        ):
            if previous[1] > 0:
                benchmark_returns[current[0]] = current[1] / previous[1] - 1.0
    metrics: dict[str, dict[str, float]] = {}
    for code, meta in metas.items():
        if meta.list_date > snapshot or meta.first_trade_date > snapshot:
            continue
        rows = series.get(code, [])
        end = bisect_right(rows, (snapshot, math.inf))
        if end < 253:
            continue
        prices = [value for _day, value in rows[:end]]
        if min(prices[-253:]) <= 0:
            continue
        daily_returns_6m = [
            prices[index] / prices[index - 1] - 1.0
            for index in range(end - 126, end)
        ]
        daily_returns = daily_returns_6m[-63:]
        recent_rows = rows[end - 127 : end]
        asset_by_day = {
            current[0]: current[1] / previous[1] - 1.0
            for previous, current in zip(recent_rows[:-1], recent_rows[1:])
            if previous[1] > 0
        }
        common_days = sorted(set(asset_by_day) & set(benchmark_returns))
        beta = 1.0
        market_correlation = 0.0
        market_return_6m = 0.0
        market_return_3m = 0.0
        if len(common_days) >= 60:
            asset_values = [asset_by_day[day] for day in common_days]
            benchmark_values = [benchmark_returns[day] for day in common_days]
            asset_mean = statistics.mean(asset_values)
            benchmark_mean = statistics.mean(benchmark_values)
            variance = statistics.pvariance(benchmark_values)
            if variance > 0:
                beta = statistics.mean(
                    (asset - asset_mean) * (market - benchmark_mean)
                    for asset, market in zip(asset_values, benchmark_values)
                ) / variance
            asset_std = statistics.pstdev(asset_values)
            market_std = statistics.pstdev(benchmark_values)
            if asset_std > 0 and market_std > 0:
                market_correlation = statistics.mean(
                    (asset - asset_mean) * (market - benchmark_mean)
                    for asset, market in zip(asset_values, benchmark_values)
                ) / asset_std / market_std
            market_return_6m = math.prod(1.0 + value for value in benchmark_values) - 1.0
            market_return_3m = math.prod(1.0 + value for value in benchmark_values[-63:]) - 1.0
        metrics[code] = {
            "m1": prices[-1] / prices[-22] - 1.0,
            "m12": prices[-1] / prices[-253] - 1.0,
            "m12_skip1": prices[-22] / prices[-253] - 1.0,
            "m6": prices[-1] / prices[-127] - 1.0,
            "m3": prices[-1] / prices[-64] - 1.0,
            "low_vol": statistics.pstdev(daily_returns) * math.sqrt(252.0),
            "low_vol_1m": statistics.pstdev(daily_returns[-21:]) * math.sqrt(252.0),
            "low_beta_6m": beta,
            "distance_high_12m": prices[-1] / max(prices[-252:]) - 1.0,
            "drawdown_3m": prices[-1] / max(prices[-63:]) - 1.0,
            "drawdown_6m": prices[-1] / max(prices[-126:]) - 1.0,
            "max_drawdown_6m": _max_drawdown(prices[-126:]),
            "positive_day_ratio_3m": sum(value > 0 for value in daily_returns) / 63.0,
            "market_correlation_6m": market_correlation,
            "relative_strength_3m": prices[-1] / prices[-64] - 1.0 - market_return_3m,
            "residual_momentum_6m": prices[-1] / prices[-127] - 1.0 - beta * market_return_6m,
        }
        metrics[code].update(
            {
                "momentum_1m": metrics[code]["m1"],
                "momentum_3m": metrics[code]["m3"],
                "momentum_12m": metrics[code]["m12"],
                "momentum_12m_skip1m": metrics[code]["m12_skip1"],
                "volatility_1m": metrics[code]["low_vol_1m"],
                "volatility_3m": metrics[code]["low_vol"],
            }
        )
    if policy.deduplicate_tracking_index:
        kept: dict[str, str] = {}
        for code in sorted(metrics):
            meta = metas[code]
            current = kept.get(meta.index_code)
            if current is None or (
                meta.list_date,
                meta.first_trade_date,
                code,
            ) < (
                metas[current].list_date,
                metas[current].first_trade_date,
                current,
            ):
                kept[meta.index_code] = code
        metrics = {code: metrics[code] for code in kept.values()}
    if not metrics:
        _DIRECT_SELECTION_CACHE[cache_key] = {}
        return {}
    benchmark_values = [
        value
        for _day, value in (benchmark_series or [])[max(0, benchmark_end - 127) : benchmark_end]
    ]
    benchmark_return_6m = (
        benchmark_values[-1] / benchmark_values[-127] - 1.0
        if len(benchmark_values) >= 127
        else 0.0
    )
    current_regime = (
        "bull" if benchmark_return_6m >= 0.10 else "bear" if benchmark_return_6m <= -0.10 else "neutral"
    )
    if policy.name.startswith("direct_online_ic_"):
        learned = learned_online_feature_weights(
            load_observations(),
            snapshot,
            current_regime,
        )
        learned = {
            feature: item
            for feature, item in learned.items()
            if all(feature in values for values in metrics.values())
        }
        if not learned:
            _DIRECT_SELECTION_CACHE[cache_key] = {}
            return {}
        online_ranks = {
            feature: _percentile(
                {code: item[feature] for code, item in metrics.items()}
            )
            for feature in learned
        }
        total_weight = sum(item["weight"] for item in learned.values())
        scores = {}
        for code in metrics:
            score = 0.0
            for feature, item in learned.items():
                rank = online_ranks[feature][code]
                score += item["weight"] * (
                    rank if item["orientation"] > 0 else 1.0 - rank
                )
            scores[code] = score / total_weight
        selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[: policy.top_n]
        powered = {code: max(scores[code], 1e-9) ** policy.score_power for code in selected}
        total = sum(powered.values())
        weights = {code: value / total for code, value in powered.items()}
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    if policy.name.startswith("direct_regime_ic_"):
        regime = current_regime
        regime_fields = {
            "bull": (
                ("max_drawdown_6m", 0.20, True),
                ("drawdown_6m", 0.15, True),
                ("drawdown_3m", 0.10, True),
                ("residual_momentum_6m", 0.15, True),
                ("low_vol", 0.10, False),
                ("low_beta_6m", 0.10, False),
                ("distance_high_12m", 0.10, True),
                ("positive_day_ratio_3m", 0.10, True),
            ),
            "neutral": (
                ("low_beta_6m", 0.25, False),
                ("m12_skip1", 0.20, True),
                ("low_vol_1m", 0.15, False),
                ("m12", 0.15, True),
                ("low_vol", 0.10, False),
                ("drawdown_6m", 0.10, False),
                ("distance_high_12m", 0.05, True),
            ),
            "bear": (
                ("low_vol", 0.20, False),
                ("positive_day_ratio_3m", 0.15, False),
                ("market_correlation_6m", 0.15, True),
                ("relative_strength_3m", 0.10, False),
                ("m1", 0.10, False),
                ("low_vol_1m", 0.10, False),
                ("residual_momentum_6m", 0.10, False),
                ("distance_high_12m", 0.10, True),
            ),
        }[regime]
        regime_ranks = {
            field: _percentile(
                {code: item[field] for code, item in metrics.items()},
                higher_is_better=higher,
            )
            for field, _weight, higher in regime_fields
        }
        scores = {
            code: sum(
                weight * regime_ranks[field][code]
                for field, weight, _higher in regime_fields
            )
            for code in metrics
        }
        selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[: policy.top_n]
        powered = {code: max(scores[code], 1e-9) ** policy.score_power for code in selected}
        total = sum(powered.values())
        weights = {code: value / total for code, value in powered.items()}
        _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
        return weights
    ranks = {
        "m12": _percentile({code: item["m12"] for code, item in metrics.items()}),
        "m6": _percentile({code: item["m6"] for code, item in metrics.items()}),
        "m3": _percentile({code: item["m3"] for code, item in metrics.items()}),
        "low_vol": _percentile(
            {code: item["low_vol"] for code, item in metrics.items()}, higher_is_better=False
        ),
        "low_vol_1m": _percentile(
            {code: item["low_vol_1m"] for code, item in metrics.items()}, higher_is_better=False
        ),
        "low_beta_6m": _percentile(
            {code: item["low_beta_6m"] for code, item in metrics.items()}, higher_is_better=False
        ),
        "distance_high_12m": _percentile(
            {code: item["distance_high_12m"] for code, item in metrics.items()}
        ),
    }
    scores = {
        code: (
            abs(policy.momentum_12m_weight)
            * (ranks["m12"][code] if policy.momentum_12m_weight >= 0 else 1.0 - ranks["m12"][code])
            + abs(policy.momentum_6m_weight)
            * (ranks["m6"][code] if policy.momentum_6m_weight >= 0 else 1.0 - ranks["m6"][code])
            + abs(policy.momentum_3m_weight)
            * (ranks["m3"][code] if policy.momentum_3m_weight >= 0 else 1.0 - ranks["m3"][code])
            + abs(policy.low_volatility_weight)
            * (
                ranks["low_vol"][code]
                if policy.low_volatility_weight >= 0
                else 1.0 - ranks["low_vol"][code]
            )
            + policy.low_beta_6m_weight * ranks["low_beta_6m"][code]
            + policy.distance_high_12m_weight * ranks["distance_high_12m"][code]
            + policy.low_volatility_1m_weight * ranks["low_vol_1m"][code]
        )
        for code in metrics
    }
    selected = sorted(scores, key=lambda code: (scores[code], code), reverse=True)[: policy.top_n]
    powered = {code: max(scores[code], 1e-9) ** policy.score_power for code in selected}
    total = sum(powered.values())
    weights = {code: value / total for code, value in powered.items()}
    _DIRECT_SELECTION_CACHE[cache_key] = dict(weights)
    return weights


def _is_overseas(code: str, name: str, index_name: str, index_code: str) -> bool:
    text = f"{code} {name} {index_name} {index_code}"
    return (
        code.startswith(OVERSEAS_CODE_PREFIXES)
        or any(keyword in text for keyword in OVERSEAS_KEYWORDS)
        or index_code.endswith((".HI", ".OTH"))
    )


def _return_map(
    rows: list[tuple[date, float]],
    snapshot: date,
    observations: int,
) -> dict[date, float]:
    end = bisect_right(rows, (snapshot, math.inf))
    window = rows[max(0, end - observations - 1) : end]
    return {
        current[0]: current[1] / previous[1] - 1.0
        for previous, current in zip(window[:-1], window[1:])
        if previous[1] > 0 and (current[0] - previous[0]).days <= 14
    }


def blended_etf_diagnostics(
    weights: dict[str, float],
    series: dict[str, list[tuple[date, float]]],
    snapshot: date,
    benchmark_series: list[tuple[date, float]] | None = None,
    *,
    metas_by_index: dict[str, list[EquityEtfMeta]] | None = None,
    index_series: dict[str, list[tuple[date, float]]] | None = None,
) -> dict[str, float]:
    """Point-in-time risk metrics for the ETF basket that will actually be held."""

    positive = {
        code: float(weight)
        for code, weight in weights.items()
        if weight > 0 and series.get(code)
    }
    total = sum(positive.values())
    if total <= 0:
        return {}
    normalized = {code: weight / total for code, weight in positive.items()}
    code_to_index = {
        meta.code: meta.index_code
        for metas in (metas_by_index or {}).values()
        for meta in metas
    }
    return_maps: dict[str, dict[date, float]] = {}
    for code in normalized:
        actual = _return_map(series[code], snapshot, 252)
        index_code = code_to_index.get(code)
        proxy = (
            _return_map(index_series[index_code], snapshot, 252)
            if index_series is not None
            and index_code is not None
            and index_series.get(index_code)
            else {}
        )
        # The tracker is the point-in-time exposure proxy before ETF listing;
        # actual ETF returns always win on dates where both are available.
        return_maps[code] = {**proxy, **actual}
    # A small newly listed sleeve must not erase the history of the complete
    # basket.  Build each historical return from the weights observable on
    # that day, while requiring at least 80% of today's target weight to have
    # a valid one-day return.  The previous all-series intersection made the
    # 6m volatility disappear whenever any selected ETF had <126 observations.
    all_dates = sorted(set().union(*(set(values) for values in return_maps.values())))
    basket_return_map: dict[date, float] = {}
    for day in all_dates:
        available = [
            (normalized[code], returns[day])
            for code, returns in return_maps.items()
            if day in returns
        ]
        covered_weight = sum(weight for weight, _value in available)
        if covered_weight >= 0.80 - 1e-12:
            basket_return_map[day] = (
                sum(weight * value for weight, value in available) / covered_weight
            )
    ordered_dates = sorted(basket_return_map)
    basket_returns = [basket_return_map[day] for day in ordered_dates]
    if not basket_returns:
        return {}

    output: dict[str, float] = {}

    def volatility(observations: int) -> float | None:
        if len(basket_returns) < observations:
            return None
        return statistics.pstdev(basket_returns[-observations:]) * math.sqrt(252.0)

    for observations, field in (
        (21, "selected_etf_volatility_1m"),
        (63, "selected_etf_volatility_3m"),
        (126, "selected_etf_volatility_6m"),
    ):
        value = volatility(observations)
        if value is not None:
            output[field] = value

    for observations, field in (
        (21, "selected_etf_momentum_1m"),
        (63, "selected_etf_momentum_3m"),
        (126, "selected_etf_momentum_6m"),
    ):
        if len(basket_returns) >= observations:
            output[field] = (
                math.prod(1.0 + value for value in basket_returns[-observations:])
                - 1.0
            )

    for observations, field in (
        (63, "selected_etf_drawdown_3m"),
        (126, "selected_etf_drawdown_6m"),
    ):
        if len(basket_returns) >= observations:
            factors = [1.0]
            for value in basket_returns[-observations:]:
                factors.append(factors[-1] * (1.0 + value))
            output[field] = factors[-1] / max(factors) - 1.0

    if len(basket_returns) >= 63:
        recent_3m = basket_returns[-63:]
        downside = [min(value, 0.0) for value in recent_3m]
        output["selected_etf_downside_volatility_3m"] = (
            math.sqrt(statistics.mean(value * value for value in downside))
            * math.sqrt(252.0)
        )
        tail_count = max(1, int(math.ceil(len(recent_3m) * 0.05)))
        output["selected_etf_historical_cvar_5pct_3m"] = statistics.mean(
            sorted(recent_3m)[:tail_count]
        )
        output["selected_etf_maximum_daily_loss_3m"] = min(recent_3m)
        output["selected_etf_negative_day_ratio_3m"] = (
            sum(value < 0.0 for value in recent_3m) / len(recent_3m)
        )
        output["selected_etf_positive_day_ratio_3m"] = (
            sum(value > 0.0 for value in recent_3m) / len(recent_3m)
        )
        vol_1m = output.get("selected_etf_volatility_1m")
        vol_3m = output.get("selected_etf_volatility_3m")
        if vol_1m is not None and vol_3m is not None and vol_3m > 0.0:
            output["selected_etf_volatility_acceleration_1m_3m"] = (
                vol_1m / vol_3m - 1.0
            )
    if len(basket_returns) >= 126:
        factors = [1.0]
        for value in basket_returns[-126:]:
            factors.append(factors[-1] * (1.0 + value))
        peak = factors[0]
        drawdowns = []
        for value in factors:
            peak = max(peak, value)
            drawdowns.append(value / peak - 1.0)
        output["selected_etf_max_drawdown_6m"] = min(drawdowns)
        output["selected_etf_ulcer_index_6m"] = math.sqrt(
            statistics.mean(value * value for value in drawdowns)
        )
        high = max(factors)
        output["selected_etf_days_since_high_6m"] = float(
            next(
                (
                    offset
                    for offset, value in enumerate(reversed(factors))
                    if value >= high * (1.0 - 1e-12)
                ),
                len(factors) - 1,
            )
        )
    if len(basket_returns) >= 252:
        output["selected_etf_momentum_12m"] = (
            math.prod(1.0 + value for value in basket_returns[-252:]) - 1.0
        )
        output["selected_etf_momentum_12m_skip1m"] = (
            math.prod(1.0 + value for value in basket_returns[-252:-21]) - 1.0
        )

    if benchmark_series and len(basket_returns) >= 126:
        benchmark = _return_map(benchmark_series, snapshot, 126)
        paired_dates = [day for day in ordered_dates[-126:] if day in benchmark]
        if len(paired_dates) >= 60:
            asset_values = [basket_return_map[day] for day in paired_dates]
            benchmark_values = [benchmark[day] for day in paired_dates]
            variance = statistics.pvariance(benchmark_values)
            if variance > 0:
                asset_mean = statistics.mean(asset_values)
                benchmark_mean = statistics.mean(benchmark_values)
                output["selected_etf_market_beta_6m"] = statistics.mean(
                    (asset - asset_mean) * (market - benchmark_mean)
                    for asset, market in zip(asset_values, benchmark_values)
                ) / variance
    return output


def _correlation(left: dict[date, float], right: dict[date, float]) -> float | None:
    common = sorted(set(left) & set(right))
    if len(common) < 20:
        return None
    xs = [left[day] for day in common]
    ys = [right[day] for day in common]
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    if sx <= 0 or sy <= 0:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    return statistics.mean((x - mx) * (y - my) for x, y in zip(xs, ys)) / sx / sy


def load_equity_etf_return_universe(
    conn,
) -> tuple[dict[str, list[EquityEtfMeta]], dict[str, list[tuple[date, float]]]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.ts_code, e.extname, e.index_ts_code, e.index_name,
                   e.list_date, MIN(f.trade_date)
            FROM passive_etf e
            JOIN fund_daily f ON f.ts_code=e.ts_code
            WHERE e.index_ts_code IS NOT NULL
              AND e.list_date IS NOT NULL
              AND (e.etf_type IS NULL OR e.etf_type!='QDII')
              AND (e.is_enhanced IS NULL OR e.is_enhanced=0)
              AND e.ts_code NOT LIKE '%%.OF'
              AND f.close IS NOT NULL
            GROUP BY e.ts_code, e.extname, e.index_ts_code, e.index_name, e.list_date
            ORDER BY e.index_ts_code, e.list_date, e.ts_code
            """
        )
        metas_by_index: dict[str, list[EquityEtfMeta]] = {}
        for code, name, index_code, index_name, list_date, first_trade_date in cur.fetchall():
            code = str(code)
            name = str(name or code)
            index_code = str(index_code)
            index_name = str(index_name or index_code)
            if _is_overseas(code, name, index_name, index_code):
                continue
            # Domestic commodity-index ETFs remain valid passive-index risk
            # candidates. Only bond and spot-gold benchmark ETFs are reserved
            # exclusively for the defensive sleeve.
            defensive_category = classify_defensive_etf(code, name, index_name)
            if defensive_category in {"bond", "gold"}:
                continue
            meta = EquityEtfMeta(
                code=code,
                name=name,
                index_code=index_code,
                index_name=index_name,
                list_date=list_date,
                first_trade_date=first_trade_date,
            )
            metas_by_index.setdefault(index_code, []).append(meta)

        codes = sorted(meta.code for metas in metas_by_index.values() for meta in metas)
        series: dict[str, list[tuple[date, float]]] = {code: [] for code in codes}
        for start in range(0, len(codes), 300):
            chunk = codes[start : start + 300]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"""
                SELECT ts_code, trade_date, pct_chg
                FROM fund_daily
                WHERE ts_code IN ({placeholders}) AND close IS NOT NULL
                ORDER BY ts_code, trade_date
                """,
                chunk,
            )
            cumulative = {code: 100.0 for code in chunk}
            for code, trade_date, pct_chg in cur.fetchall():
                code = str(code)
                if series[code] and pct_chg is not None:
                    cumulative[code] *= 1.0 + float(pct_chg) / 100.0
                series[code].append((trade_date, cumulative[code]))
    return metas_by_index, series


def map_indices_to_etfs(
    index_weights: dict[str, float],
    snapshot: date,
    metas_by_index: dict[str, list[EquityEtfMeta]],
    *,
    etf_series: dict[str, list[tuple[date, float]]] | None = None,
    allow_early_broad_proxy: bool = False,
    allow_correlation_proxy: bool = False,
    index_series: dict[str, list[tuple[date, float]]] | None = None,
    correlation_lookback: int = 252,
    minimum_correlation: float = 0.30,
) -> dict[str, float]:
    cache_key = (
        id(metas_by_index),
        id(etf_series),
        id(index_series),
        snapshot,
        tuple(sorted((str(code), float(weight)) for code, weight in index_weights.items())),
        allow_early_broad_proxy,
        allow_correlation_proxy,
        correlation_lookback,
        minimum_correlation,
    )
    cached = _INDEX_MAPPING_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    all_metas = [meta for metas in metas_by_index.values() for meta in metas]
    etf_weights: dict[str, float] = {}
    for index_code, weight in index_weights.items():
        candidates = [
            meta
            for meta in metas_by_index.get(index_code, [])
            if meta.list_date <= snapshot and meta.first_trade_date <= snapshot
            and (etf_series is None or has_recent_etf_price(etf_series, meta.code, snapshot))
        ]
        if not candidates:
            if allow_correlation_proxy and index_series is not None:
                target_returns = _return_map(
                    index_series.get(index_code, []),
                    snapshot,
                    correlation_lookback,
                )
                proxy_candidates = []
                for tracked_index, tracker_metas in metas_by_index.items():
                    live = [
                        meta
                        for meta in tracker_metas
                        if meta.list_date <= snapshot and meta.first_trade_date <= snapshot
                        and (
                            etf_series is None
                            or has_recent_etf_price(etf_series, meta.code, snapshot)
                        )
                    ]
                    if not live:
                        continue
                    tracked_returns = _return_map(
                        index_series.get(tracked_index, []),
                        snapshot,
                        correlation_lookback,
                    )
                    value = _correlation(target_returns, tracked_returns)
                    if value is None or value < minimum_correlation:
                        continue
                    tracker = min(
                        live,
                        key=lambda meta: (meta.list_date, meta.first_trade_date, meta.code),
                    )
                    proxy_candidates.append((value, tracker))
                if proxy_candidates:
                    best_value = max(item[0] for item in proxy_candidates)
                    candidates = [
                        tracker
                        for value, tracker in proxy_candidates
                        if abs(value - best_value) <= 1e-12
                    ]
            if candidates:
                pass
            elif allow_early_broad_proxy:
                candidates = [
                    meta
                    for meta in all_metas
                    if meta.code in EARLY_BROAD_PROXY_CODES
                    and meta.list_date <= snapshot
                    and meta.first_trade_date <= snapshot
                    and (
                        etf_series is None
                        or has_recent_etf_price(etf_series, meta.code, snapshot)
                    )
                ]
            if not candidates:
                raise RuntimeError(f"no point-in-time passive ETF for {index_code} at {snapshot}")
        # The oldest live tracker avoids selecting a fund using future liquidity information.
        selected = min(
            candidates,
            key=lambda meta: (
                EARLY_BROAD_PROXY_CODES.index(meta.code)
                if meta.code in EARLY_BROAD_PROXY_CODES
                else -1,
                meta.list_date,
                meta.first_trade_date,
                meta.code,
            ),
        )
        etf_weights[selected.code] = etf_weights.get(selected.code, 0.0) + float(weight)
    _INDEX_MAPPING_CACHE[cache_key] = dict(etf_weights)
    return etf_weights


def portfolio_turnover(previous: dict[str, float], current: dict[str, float]) -> float:
    codes = set(previous) | set(current)
    risky_l1 = sum(abs(current.get(code, 0.0) - previous.get(code, 0.0)) for code in codes)
    previous_cash = max(0.0, 1.0 - sum(previous.values()))
    current_cash = max(0.0, 1.0 - sum(current.values()))
    return 0.5 * (risky_l1 + abs(current_cash - previous_cash))


def describe_equity_universe(metas_by_index: dict[str, list[EquityEtfMeta]]) -> dict[str, object]:
    metas = [meta for values in metas_by_index.values() for meta in values]
    return {
        "index_count": len(metas_by_index),
        "etf_count": len(metas),
        "first_list_date": min((meta.list_date for meta in metas), default=None).isoformat() if metas else None,
    }
