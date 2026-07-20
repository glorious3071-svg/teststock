from __future__ import annotations

import unittest
from datetime import date, timedelta

from backtest.domestic_equity_etf import (
    DIRECT_ETF_POLICIES,
    EquityEtfMeta,
    direct_blend_share,
    healthcare_resilience_share_from_policy,
    pure_structural_rotation_active,
    structural_direct_blend_share_from_policy,
    structural_price_cold_start_scores,
    structural_repair_share_from_policy,
    structural_repair_top_n_from_policy,
)
from backtest.strict_passive_etf_objective import (
    STRICT_OBJECTIVE,
    validate_case_matrix,
    validate_quarterly_weight_path,
    validate_target_assets,
)
from scripts.backtest_calendar_neutral_csi_tipp import (
    benchmark_bear_state,
    benchmark_trend_diagnostics,
)
from scripts.backtest_scorecard_csi_midyear_risk import CS300_CODE
from scripts.backtest_scorecard_csi_strict_quarterly_etf import (
    DEFENSIVE_POLICIES,
    QUARTERLY_RISK_FLAGS,
    RISK_FLAG_CLUSTERS,
    RULES,
    crisis_rebound_blocks_quality_acceleration,
    crisis_rebound_state,
    crisis_relative_strength_reentry_signal,
    boost_exposure_with_active_caps,
    cold_start_models_unavailable,
    cold_start_price_damage_signal,
    direction_boost_allowed,
    direction_boost_blocked_by_macro_weakness,
    append_exposure_trace_stage,
    annual_score_adjusted_cushion_multiplier,
    apply_feature_exposure_cap,
    apply_feature_cushion_multiplier,
    broad_participation_reentry_signal,
    initial_exposure_from_limits,
    market_recovery_signal,
    mark_frozen_positions,
    observe_quality_features,
    option_panic_after_rally_cap_signal,
    policy_supported_oversold_reentry_signal,
    quality_adjusted_cushion_multiplier,
    quality_multiplier_trend_confirmed,
    rebalance_frozen_positions,
    remap_annual_base_weight,
    resolve_feature_risk_cap,
    risk_flag_clusters,
    safe_gate_cluster_allowed,
    safe_gate_flags_allowed,
    safe_gate_pathrisk_blocked,
    short_cycle_structural_reentry_signal,
    evaluate_path,
    walkforward_quality_score,
    walkforward_upper_tail_signal,
)


class StrictPassiveEtfObjectiveTest(unittest.TestCase):
    def test_bear_indicator_uses_60_observations_and_20_intervals(self) -> None:
        start = date(2020, 1, 1)
        rows = [
            (start + timedelta(days=index), 100.0 - max(index - 60, 0))
            for index in range(81)
        ]
        series = {CS300_CODE: rows}
        dates = [day for day, _value in rows]
        diagnostic = benchmark_trend_diagnostics(
            series,
            dates,
            dates[-1],
            ma_days=60,
            return_days=20,
        )
        expected_ma = sum(value for _day, value in rows[-60:]) / 60.0
        expected_return = rows[-1][1] / rows[-21][1] - 1.0
        self.assertAlmostEqual(diagnostic["moving_average"], expected_ma)
        self.assertAlmostEqual(diagnostic["trailing_return"], expected_return)
        self.assertTrue(diagnostic["bear_state"])
        self.assertTrue(benchmark_bear_state(series, dates, dates[-1], 60, 20))

    def test_initial_exposure_audit_names_the_binding_limit(self) -> None:
        exposure, audit = initial_exposure_from_limits(
            max_exposure=1.0,
            base_weight=0.48,
            base_scale=1.25,
            cppi_limit=0.42,
        )
        self.assertEqual(exposure, 0.42)
        self.assertEqual(audit["limits"]["annual_scorecard"], 0.60)
        self.assertEqual(audit["binding_limits"], ["cppi_cushion"])

    def test_annual_score_boost_stays_inside_cushion_multiplier(self) -> None:
        multiplier, active = annual_score_adjusted_cushion_multiplier(
            4.25, -2, -1, 1.50
        )
        self.assertTrue(active)
        self.assertAlmostEqual(multiplier, 6.375)
        multiplier, active = annual_score_adjusted_cushion_multiplier(
            4.25, 0, -1, 1.50
        )
        self.assertFalse(active)
        self.assertEqual(multiplier, 4.25)

    def test_annual_scorecard_band_override_is_explicit_and_exact(self) -> None:
        overrides = ((0.60, 0.30), (0.85, 0.65))
        self.assertEqual(remap_annual_base_weight(0.60, overrides), 0.30)
        self.assertEqual(remap_annual_base_weight(0.85, overrides), 0.65)
        self.assertEqual(remap_annual_base_weight(0.80, overrides), 0.80)

    def test_exposure_trace_distinguishes_active_from_applied(self) -> None:
        trace = []
        append_exposure_trace_stage(
            trace,
            "cap",
            0.20,
            0.20,
            active=True,
            details={"cap": 0.30},
        )
        append_exposure_trace_stage(
            trace,
            "exit",
            0.20,
            0.0,
            active=True,
        )
        self.assertTrue(trace[0]["active"])
        self.assertFalse(trace[0]["applied"])
        self.assertTrue(trace[1]["applied"])
        self.assertEqual(trace[1]["effect"], "decrease")

    def test_direction_boost_respects_predecision_drawdown_guard(self) -> None:
        policy = next(
            item.direction_policy
            for item in RULES
            if item.name.endswith("direction_return4_boost")
        )
        guarded = policy.__class__(
            **{**policy.__dict__, "boost_allowed_drawdown_gte": -0.05}
        )
        decision = {"score": 0.25, "vote_count": 4}
        self.assertTrue(direction_boost_allowed(decision, guarded, -0.04))
        self.assertFalse(direction_boost_allowed(decision, guarded, -0.06))

    def test_direction_boost_cannot_bypass_an_active_bear_cap(self) -> None:
        self.assertEqual(
            boost_exposure_with_active_caps(0.36, 1.575, 1.0, (0.36,)),
            0.36,
        )
        self.assertEqual(
            boost_exposure_with_active_caps(0.20, 1.575, 1.0, (0.36,)),
            0.315,
        )

    def test_cold_start_requires_both_walkforward_models_to_be_unavailable(self) -> None:
        self.assertTrue(
            cold_start_models_unavailable({"score": None}, {"score": None})
        )
        self.assertFalse(
            cold_start_models_unavailable({"score": 0.0}, {"score": None})
        )
        self.assertFalse(
            cold_start_models_unavailable({"score": None}, {"score": -0.2})
        )

    def test_crisis_relative_strength_reentry_requires_visible_basket_repair(self) -> None:
        repaired = {
            "crisis_continuation_flag": 1.0,
            "early_history_crisis_repricing_flag": 0.0,
            "basket_excess_return_3m": 0.10,
            "breadth_return_3m_positive": 0.60,
            "basket_ma_3m_distance": 0.01,
        }
        self.assertTrue(crisis_relative_strength_reentry_signal(repaired))
        self.assertFalse(
            crisis_relative_strength_reentry_signal(
                {**repaired, "basket_ma_3m_distance": -0.01}
            )
        )
        self.assertFalse(
            crisis_relative_strength_reentry_signal(
                {**repaired, "early_history_crisis_repricing_flag": 1.0}
            )
        )

    def test_policy_supported_oversold_reentry_requires_policy_tone(self) -> None:
        oversold = {
            "crisis_continuation_flag": 0.0,
            "domestic_liquidity_stress_flag": 0.0,
            "early_history_crisis_repricing_flag": 0.0,
            "low_vol_breadth_rollover_flag": 0.0,
            "daily_margin_rally_flag": 0.0,
            "high_level_distribution_flag": 0.0,
            "leverage_macro_divergence_flag": 0.0,
            "cs300_return_6m": -0.19,
            "basket_drawdown_6m": -0.18,
            "basket_vol_3m": 0.19,
            "breadth_return_3m_positive": 0.0,
            "basket_return_3m_max": -0.01,
            "pboc_outlook_net_tone": 5.3,
        }
        self.assertTrue(
            policy_supported_oversold_reentry_signal(
                oversold,
                active_risk_flags=[],
            )
        )
        self.assertFalse(
            policy_supported_oversold_reentry_signal(
                {**oversold, "pboc_outlook_net_tone": 4.9},
                active_risk_flags=[],
            )
        )
        self.assertFalse(
            policy_supported_oversold_reentry_signal(
                {**oversold, "low_vol_breadth_rollover_flag": 1.0},
                active_risk_flags=[],
            )
        )
        self.assertFalse(
            policy_supported_oversold_reentry_signal(
                oversold,
                active_risk_flags=["domestic_liquidity_stress_flag"],
            )
        )
        breadth_repaired = {
            **oversold,
            "pboc_outlook_net_tone": 0.0,
            "breadth_return_1m_positive": 1.0,
            "basket_return_1m": 0.02,
            "selected_etf_drawdown_3m": -0.06,
        }
        self.assertTrue(
            policy_supported_oversold_reentry_signal(
                breadth_repaired,
                active_risk_flags=[],
            )
        )
        self.assertFalse(
            policy_supported_oversold_reentry_signal(
                {**breadth_repaired, "selected_etf_drawdown_3m": -0.08},
                active_risk_flags=[],
            )
        )

    def test_cold_start_price_damage_requires_both_trend_and_drawdown(self) -> None:
        damaged = {
            "selected_etf_momentum_12m_skip1m": -0.20,
            "selected_etf_max_drawdown_6m": -0.18,
        }
        self.assertTrue(cold_start_price_damage_signal(damaged))
        self.assertFalse(
            cold_start_price_damage_signal(
                {**damaged, "selected_etf_max_drawdown_6m": -0.10}
            )
        )

    def test_short_cycle_structural_reentry_requires_visible_local_repair(self) -> None:
        state = {
            "cs300_return_3m": -0.06,
            "basket_return_1m": 0.09,
            "basket_return_1m_dispersion": 0.075,
            "basket_return_1m_max": 0.25,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_3m": -0.035,
            "selected_etf_momentum_3m": 0.022,
            "selected_etf_drawdown_3m": -0.004,
            "basket_vol_3m": 0.218,
        }
        self.assertTrue(
            short_cycle_structural_reentry_signal(
                state,
                active_risk_flags=[],
            )
        )
        self.assertFalse(
            short_cycle_structural_reentry_signal(
                {**state, "basket_return_1m_dispersion": 0.05},
                active_risk_flags=[],
            )
        )

    def test_short_cycle_structural_reentry_respects_hard_risk_flags(self) -> None:
        state = {
            "cs300_return_3m": -0.06,
            "basket_return_1m": 0.09,
            "basket_return_1m_dispersion": 0.075,
            "basket_return_1m_max": 0.25,
            "breadth_return_1m_positive": 1.0,
            "basket_drawdown_3m": -0.035,
            "selected_etf_momentum_3m": 0.022,
            "selected_etf_drawdown_3m": -0.004,
            "basket_vol_3m": 0.218,
            "crisis_continuation_flag": 1.0,
        }
        self.assertFalse(
            short_cycle_structural_reentry_signal(
                state,
                active_risk_flags=[],
            )
        )
        self.assertFalse(
            short_cycle_structural_reentry_signal(
                {**state, "crisis_continuation_flag": 0.0},
                active_risk_flags=["daily_margin_rally_flag"],
            )
        )

    def test_direction_macro_block_requires_both_point_in_time_conditions(self) -> None:
        state = {
            "pboc_outlook_net_tone": -6.0,
            "cs300_ma_6m_distance": -0.03,
        }
        self.assertTrue(
            direction_boost_blocked_by_macro_weakness(state, 0.0, 0.0)
        )
        self.assertFalse(
            direction_boost_blocked_by_macro_weakness(state, -10.0, 0.0)
        )
        self.assertFalse(
            direction_boost_blocked_by_macro_weakness(
                {"pboc_outlook_net_tone": -6.0}, 0.0, 0.0
            )
        )

    def test_risk_flags_are_assigned_to_exactly_one_independent_cluster(self) -> None:
        assigned = [
            flag for members in RISK_FLAG_CLUSTERS.values() for flag in members
        ]
        self.assertEqual(set(assigned), set(QUARTERLY_RISK_FLAGS))
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_independent_overheat_flags_count_as_two_risk_clusters(self) -> None:
        clusters = risk_flag_clusters(
            [
                "market_overheat_flag",
                "daily_margin_rally_flag",
                "leveraged_rally_exhaustion_flag",
            ]
        )
        self.assertEqual(clusters, ["leverage_crowding", "price_cycle"])

    def test_leveraged_exhaustion_is_not_duplicated_via_composite_flag(self) -> None:
        self.assertEqual(
            risk_flag_clusters(
                [
                    "leveraged_rally_exhaustion_flag",
                    "medium_cycle_exhaustion_flag",
                ]
            ),
            ["leverage_crowding"],
        )

    def test_safe_gate_relaxes_only_an_active_risk_cap(self) -> None:
        cap, cluster_relaxed, safe_relaxed = resolve_feature_risk_cap(
            0.185,
            True,
            ["macro_liquidity"],
            None,
            None,
            True,
            0.27,
        )
        self.assertEqual(cap, 0.27)
        self.assertFalse(cluster_relaxed)
        self.assertTrue(safe_relaxed)

        cap, _cluster_relaxed, safe_relaxed = resolve_feature_risk_cap(
            0.185,
            False,
            [],
            None,
            None,
            True,
            0.27,
        )
        self.assertEqual(cap, 0.185)
        self.assertFalse(safe_relaxed)

    def test_safe_gate_cluster_scope_rejects_undeclared_risk(self) -> None:
        allowed = ("price_cycle", "breadth_leadership", "macro_liquidity")
        self.assertTrue(safe_gate_cluster_allowed(["price_cycle"], allowed))
        self.assertFalse(
            safe_gate_cluster_allowed(["leverage_crowding"], allowed)
        )
        self.assertFalse(safe_gate_cluster_allowed([], allowed))

    def test_safe_gate_block_flag_rejects_margin_rally(self) -> None:
        blocked = ("daily_margin_rally_flag",)
        self.assertTrue(safe_gate_flags_allowed(["market_overheat_flag"], blocked))
        self.assertFalse(
            safe_gate_flags_allowed(["daily_margin_rally_flag"], blocked)
        )

    def test_safe_gate_pathrisk_blocks_negative_leverage_divergence(self) -> None:
        self.assertTrue(
            safe_gate_pathrisk_blocked(
                ["leverage_macro_divergence_flag"],
                {"score": -0.11},
            )
        )
        self.assertFalse(
            safe_gate_pathrisk_blocked(
                ["leverage_macro_divergence_flag"],
                {"score": -0.09},
            )
        )
        self.assertFalse(
            safe_gate_pathrisk_blocked(
                ["market_overheat_flag"],
                {"score": -0.20},
            )
        )

    def test_cluster_and_safe_gate_relaxations_take_the_declared_maximum(self) -> None:
        cap, cluster_relaxed, safe_relaxed = resolve_feature_risk_cap(
            0.185,
            True,
            ["price_cycle"],
            ("price_cycle", "leverage_crowding"),
            0.24,
            True,
            0.21,
        )
        self.assertEqual(cap, 0.24)
        self.assertTrue(cluster_relaxed)
        self.assertTrue(safe_relaxed)

    def test_risky_label_is_counterfactual_when_portfolio_exposure_is_zero(self) -> None:
        rule = next(
            item
            for item in RULES
            if item.name == "q_pboc_frozenmargin_e64c28_f900_n03"
        )

        def row(previous_day: date, day: date) -> dict:
            return {
                "previous_day": previous_day,
                "day": day,
                "window_start": True,
                "base_weight": 0.0,
                "bear_state": False,
                "market_state": {},
                "equity_etf_weights": {"ETF": 1.0},
                "rebalance_anchor": previous_day.isoformat(),
                "bear_signal_timing": "snapshot",
                "bear_signal_date": previous_day,
            }

        path = {
            "daily": [
                row(date(2020, 1, 1), date(2020, 1, 2)),
                row(date(2020, 4, 1), date(2020, 4, 2)),
            ],
            "phase": 0,
            "lag": 0,
            "sample_start": "2020-01-01",
            "sample_end": "2020-04-02",
            "sample_shift_cycles": 0,
        }
        equity_series = {
            "ETF": [
                (date(2020, 1, 1), 100.0),
                (date(2020, 1, 2), 110.0),
                (date(2020, 4, 1), 110.0),
                (date(2020, 4, 2), 110.0),
            ]
        }
        result = evaluate_path(
            path,
            rule,
            equity_series,
            [],
            {},
            DEFENSIVE_POLICIES[0],
            include_decision_rows=True,
        )
        first = result["decision_rows"][0]
        self.assertEqual(first["exposure"], 0.0)
        self.assertAlmostEqual(first["realized_risk_return"], 0.10)
        formation = first["exposure_formation"]
        self.assertEqual(formation["scorecard_limit"], 0.0)
        self.assertEqual(formation["initial_binding_limits"], ["annual_scorecard"])
        self.assertEqual(formation["final_exposure"], first["exposure"])

    def test_feature_cushion_multiplier_only_boosts_low_values(self) -> None:
        boosted, applied = apply_feature_cushion_multiplier(
            4.25, {"crowding": 0.30}, "crowding", 0.35, 8.0
        )
        self.assertEqual(boosted, 8.0)
        self.assertTrue(applied)
        unchanged, applied = apply_feature_cushion_multiplier(
            4.25, {"crowding": 0.40}, "crowding", 0.35, 8.0
        )
        self.assertEqual(unchanged, 4.25)
        self.assertFalse(applied)

    def test_point_in_time_feature_cap_only_reduces_existing_exposure(self) -> None:
        capped, applied = apply_feature_exposure_cap(
            0.40,
            {"curve_percentile": 0.62},
            "curve_percentile",
            0.60,
            0.26,
        )
        self.assertTrue(applied)
        self.assertEqual(capped, 0.26)
        unchanged, applied = apply_feature_exposure_cap(
            0.20,
            {"curve_percentile": 0.62},
            "curve_percentile",
            0.60,
            0.26,
        )
        self.assertFalse(applied)
        self.assertEqual(unchanged, 0.20)
        missing, applied = apply_feature_exposure_cap(
            0.40,
            {},
            "curve_percentile",
            0.60,
            0.26,
        )
        self.assertFalse(applied)
        self.assertEqual(missing, 0.40)

    def test_quarterly_positions_drift_without_hidden_daily_rebalance(self) -> None:
        positions, cost, turnover = rebalance_frozen_positions(
            {"CASH": 1_000_000.0},
            {"ETF_A": 0.5, "ETF_B": 0.5},
            1_000_000.0,
            transaction_cost_bps=0.0,
        )
        self.assertEqual(cost, 0.0)
        self.assertEqual(turnover, 1.0)
        positions = mark_frozen_positions(
            positions,
            {"ETF_A": 1.0, "ETF_B": 0.0},
        )
        self.assertAlmostEqual(positions["ETF_A"] / sum(positions.values()), 2.0 / 3.0)
        positions = mark_frozen_positions(
            positions,
            {"ETF_A": -0.5, "ETF_B": 0.0},
        )
        self.assertAlmostEqual(sum(positions.values()), 1_000_000.0)

    def test_quarterly_rebalance_cost_uses_whole_portfolio_turnover(self) -> None:
        positions, cost, turnover = rebalance_frozen_positions(
            {"ETF_A": 600_000.0, "CASH": 400_000.0},
            {"ETF_B": 0.6, "CASH": 0.4},
            1_000_000.0,
            transaction_cost_bps=5.0,
        )
        self.assertEqual(turnover, 0.6)
        self.assertEqual(cost, 300.0)
        self.assertAlmostEqual(sum(positions.values()), 999_700.0)

    def test_regime_blend_share_uses_quarter_boundary_state_only(self) -> None:
        policy = next(
            item
            for item in DIRECT_ETF_POLICIES
            if item.name == "blend_index_weighted_stable_v5_top1_regime_w35_s70"
        )
        strong = {
            "cs300_return_3m": 0.08,
            "cs300_return_6m": 0.01,
            "cs300_ma_6m_distance": 0.02,
            "basket_drawdown_6m": -0.04,
        }
        self.assertEqual(direct_blend_share(policy, strong), 0.70)
        weak = dict(strong, basket_drawdown_6m=-0.06)
        self.assertEqual(direct_blend_share(policy, weak), 0.35)
        missing = dict(strong)
        missing.pop("basket_drawdown_6m")
        self.assertEqual(direct_blend_share(policy, missing), 0.35)

    def test_structural_mainline_conditional_blend_uses_point_in_time_state(self) -> None:
        policy = next(
            item
            for item in DIRECT_ETF_POLICIES
            if item.name == "blend_index_structural_mainline_top5_cond_w10"
        )
        structural = {
            "cs300_return_3m": -0.02,
            "basket_return_3m_dispersion": 0.10,
            "basket_return_3m_max": 0.12,
            "breadth_return_3m_positive": 0.58,
            "basket_drawdown_6m": -0.08,
        }
        self.assertEqual(direct_blend_share(policy, structural), 0.10)
        self.assertEqual(
            direct_blend_share(policy, dict(structural, cs300_return_3m=0.08)),
            0.0,
        )
        self.assertEqual(
            direct_blend_share(
                policy,
                dict(structural, crisis_continuation_flag=1.0),
            ),
            0.0,
        )
        missing = dict(structural)
        missing.pop("basket_return_3m_dispersion")
        self.assertEqual(direct_blend_share(policy, missing), 0.0)

    def test_structural_repair_share_is_encoded_in_policy_name(self) -> None:
        self.assertEqual(
            structural_repair_share_from_policy(
                "blend_index_weighted_stable_v9_structural_repair_top5_s05_regime_w49_s92"
            ),
            0.05,
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                "blend_index_weighted_stable_v9_structural_repair_top5_s20_regime_w49_s92"
            ),
            0.20,
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                "blend_index_weighted_stable_v9_structural_flow_repair_top5_s05_regime_w49_s92"
            ),
            0.05,
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_regime_w49_s92"
            ),
            0.05,
        )
        structural = {
            "cs300_return_3m": -0.02,
            "basket_return_3m_dispersion": 0.10,
            "basket_return_3m_max": 0.12,
            "breadth_return_3m_positive": 0.58,
            "basket_drawdown_6m": -0.08,
        }
        cond_name = (
            "blend_index_weighted_stable_v9_structural_flow_repair_top5"
            "_s05_cond20_regime_w49_s92"
        )
        self.assertEqual(
            structural_repair_share_from_policy(cond_name, structural),
            0.20,
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                cond_name,
                dict(structural, crisis_continuation_flag=1.0),
            ),
            0.05,
        )
        wide_name = (
            "blend_index_weighted_stable_v9_structural_flow_repair_top5"
            "_s05_widecond35_regime_w49_s92"
        )
        wide = {
            "cs300_return_3m": 0.12,
            "basket_return_3m_dispersion": 0.04,
            "basket_return_3m_max": 0.07,
            "breadth_return_3m_positive": 0.55,
            "basket_drawdown_6m": -0.08,
        }
        self.assertEqual(
            structural_repair_share_from_policy(wide_name, wide),
            0.35,
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                wide_name,
                dict(wide, basket_drawdown_6m=-0.14),
            ),
            0.05,
        )
        rotation_name = (
            "blend_index_weighted_stable_v9_structural_flow_repair_top10"
            "_s05_rotcond20_regime_w49_s92"
        )
        rotation = {
            "cs300_return_3m": 0.066,
            "basket_return_3m_dispersion": 0.009,
            "basket_return_3m_max": 0.108,
            "breadth_return_3m_positive": 1.0,
            "basket_drawdown_6m": -0.007,
            "selected_etf_momentum_3m": 0.063,
            "selector_score_margin": 0.019,
        }
        self.assertEqual(
            structural_repair_share_from_policy(rotation_name, rotation),
            0.20,
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                rotation_name,
                dict(rotation, domestic_liquidity_stress_flag=1.0),
            ),
            0.05,
        )
        early_rotation_name = (
            "blend_index_weighted_stable_v9_structural_flow_repair_top10"
            "_s05_earlyrotcond50_regime_w49_s92"
        )
        self.assertEqual(
            structural_repair_share_from_policy(early_rotation_name, rotation),
            0.50,
        )
        self.assertEqual(
            structural_repair_share_from_policy(early_rotation_name, wide),
            0.05,
        )
        self.assertEqual(structural_repair_share_from_policy("unknown"), 0.05)
        self.assertEqual(
            structural_repair_top_n_from_policy(
                "blend_index_weighted_stable_v9_structural_flow_repair_top10_s05_regime_w49_s92"
            ),
            10,
        )
        self.assertEqual(structural_repair_top_n_from_policy("unknown"), 5)
        self.assertEqual(
            healthcare_resilience_share_from_policy(
                "blend_index_weighted_stable_v9_structural_conditional_repair_top3_s10_rotcond35_hcres50_shockres100_earlyres50_regime_w49_s92"
            ),
            0.50,
        )
        self.assertEqual(
            structural_repair_top_n_from_policy(
                "blend_index_weighted_stable_v9_structural_multistate_repair_top3_s10_rotcond35_hcres100_shockres100_earlyres50_regime_w49_s92"
            ),
            3,
        )
        structural_blend_name = (
            "blend_index_weighted_stable_v9_structural_multistate_repair_top3"
            "_s10_rotcond35_hcres100_structblend70_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertEqual(
            structural_direct_blend_share_from_policy(structural_blend_name),
            0.70,
        )
        self.assertIsNone(structural_direct_blend_share_from_policy("unknown"))
        self.assertIn(
            structural_blend_name,
            {policy.name for policy in DIRECT_ETF_POLICIES},
        )
        self.assertIsNone(healthcare_resilience_share_from_policy("unknown"))

        policy = next(
            policy
            for policy in DIRECT_ETF_POLICIES
            if policy.name == structural_blend_name
        )
        self.assertEqual(direct_blend_share(policy, rotation), 0.70)
        self.assertEqual(
            direct_blend_share(policy, dict(rotation, domestic_liquidity_stress_flag=1.0)),
            0.49,
        )
        broad_rotation = dict(rotation, selected_etf_momentum_3m=0.021)
        self.assertEqual(direct_blend_share(policy, broad_rotation), 0.70)
        weak_rotation = dict(rotation, selected_etf_momentum_3m=0.019)
        self.assertEqual(direct_blend_share(policy, weak_rotation), 0.49)
        exhaustion_blend_name = (
            "blend_index_weighted_stable_v9_structural_multistate_repair_top3"
            "_s10_rotcond35_hcres100_structblend85_exhaustfallback"
            "_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertEqual(
            structural_direct_blend_share_from_policy(exhaustion_blend_name),
            0.85,
        )
        self.assertIn(
            exhaustion_blend_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        late_cycle_blend_name = (
            "blend_index_weighted_stable_v9_structural_latecycle_repair_top3"
            "_s10_rotcond35_hcres100_structblend85_exhaustfallback"
            "_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertEqual(
            structural_repair_top_n_from_policy(late_cycle_blend_name),
            3,
        )
        self.assertEqual(
            structural_direct_blend_share_from_policy(late_cycle_blend_name),
            0.85,
        )
        self.assertIn(
            late_cycle_blend_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        late_cycle_pure_name = (
            "blend_index_weighted_stable_v9_structural_latecycle_repair_top3"
            "_s20_rotcond50_hcres100_structblend85_purestruct_exhaustfallback"
            "_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertEqual(
            structural_repair_top_n_from_policy(late_cycle_pure_name),
            3,
        )
        self.assertEqual(
            structural_direct_blend_share_from_policy(late_cycle_pure_name),
            0.85,
        )
        self.assertIn(
            late_cycle_pure_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        late_cycle_cond_name = (
            "blend_index_weighted_stable_v9_structural_latecycle_repair_top3"
            "_s20_rotcond50_hcres100_structblend85_purestructcond_exhaustfallback"
            "_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertEqual(
            structural_repair_top_n_from_policy(late_cycle_cond_name),
            3,
        )
        self.assertEqual(
            structural_direct_blend_share_from_policy(late_cycle_cond_name),
            0.85,
        )
        self.assertIn(
            late_cycle_cond_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        cold_start_name = (
            "blend_index_weighted_stable_v9_structural_latecycle_repair_top3"
            "_s20_rotcond50_hcres100_structblend85_purestructcond_coldstart"
            "_exhaustfallback_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertIn(
            cold_start_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        finance_defensive_name = (
            "blend_index_weighted_stable_v9_structural_latecycle_techpullback_repair_top3"
            "_s20_rotcond50_hcres100_hcblend85_neres100_neblend85_drotres100"
            "_drotblend85_findefres100_structblend85_purestructcond_coldstart"
            "_exhaustfallback_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertEqual(
            structural_repair_top_n_from_policy(finance_defensive_name),
            3,
        )
        self.assertEqual(
            structural_direct_blend_share_from_policy(finance_defensive_name),
            0.85,
        )
        self.assertIn(
            finance_defensive_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        participation_reentry_name = "q_mdd20_qfree_stack_highdist800_partreentry50"
        self.assertIn(
            participation_reentry_name,
            {candidate.name for candidate in RULES},
        )
        policycat_name = (
            "blend_index_weighted_stable_v9_structural_policycat_repair_top3"
            "_s20_rotcond50_hcres100_structblend85_exhaustfallback"
            "_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertEqual(structural_repair_top_n_from_policy(policycat_name), 3)
        self.assertEqual(
            structural_direct_blend_share_from_policy(policycat_name),
            0.85,
        )
        self.assertIn(
            policycat_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        cooling_repair_name = (
            "blend_index_weighted_stable_v9_structural_cooling_repair_top3"
            "_s00_rotcond100_structblend85_shockres100_earlyres50_regime_w49_s92"
        )
        self.assertIn(
            cooling_repair_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        self.assertEqual(structural_repair_top_n_from_policy(cooling_repair_name), 3)
        self.assertEqual(structural_repair_share_from_policy(cooling_repair_name), 0.0)
        self.assertEqual(
            structural_repair_share_from_policy(cooling_repair_name, broad_rotation),
            1.0,
        )
        cooling_repair_half_name = cooling_repair_name.replace(
            "_rotcond100_",
            "_rotcond50_",
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                cooling_repair_half_name,
                broad_rotation,
            ),
            0.50,
        )
        cooling_repair_no_override_name = (
            "blend_index_weighted_stable_v9_structural_cooling_repair_top3"
            "_s00_rotcond50_structblend85_regime_w49_s92"
        )
        self.assertIn(
            cooling_repair_no_override_name,
            {candidate.name for candidate in DIRECT_ETF_POLICIES},
        )
        self.assertEqual(
            structural_repair_share_from_policy(
                cooling_repair_no_override_name,
                broad_rotation,
            ),
            0.50,
        )
        cooling_policy = next(
            candidate
            for candidate in DIRECT_ETF_POLICIES
            if candidate.name == cooling_repair_name
        )
        self.assertEqual(direct_blend_share(cooling_policy, broad_rotation), 0.85)
        exhaustion_policy = next(
            candidate
            for candidate in DIRECT_ETF_POLICIES
            if candidate.name == exhaustion_blend_name
        )
        exhaustion_setup = {
            "cs300_return_3m": -0.02,
            "basket_return_3m_max": 0.12,
            "breadth_return_3m_positive": 0.40,
            "basket_drawdown_6m": -0.08,
            "selector_score_margin": 0.04,
        }
        self.assertEqual(direct_blend_share(exhaustion_policy, exhaustion_setup), 0.85)
        self.assertEqual(
            direct_blend_share(
                exhaustion_policy,
                dict(exhaustion_setup, breadth_return_3m_positive=0.80),
            ),
            0.49,
        )
        self.assertEqual(
            direct_blend_share(
                exhaustion_policy,
                dict(exhaustion_setup, selector_score_margin=0.01),
            ),
            0.49,
        )

    def test_pure_structural_rotation_guard_filters_weak_downtrend(self) -> None:
        weak_downtrend = {
            "basket_return_3m_max": 0.007,
            "breadth_return_3m_positive": 0.25,
            "basket_drawdown_6m": -0.10,
            "basket_vol_3m": 0.27,
        }
        self.assertFalse(pure_structural_rotation_active(weak_downtrend))
        strong_structure = {
            "basket_return_3m_max": 0.15,
            "breadth_return_3m_positive": 1.0,
            "basket_drawdown_6m": -0.04,
            "basket_vol_3m": 0.22,
        }
        self.assertTrue(pure_structural_rotation_active(strong_structure))

    def test_broad_participation_reentry_blocks_distribution_risk(self) -> None:
        market_state = {
            "basket_return_3m_max": 0.22,
            "breadth_return_3m_positive": 0.90,
            "basket_drawdown_6m": -0.03,
            "basket_vol_3m": 0.24,
        }
        self.assertTrue(
            broad_participation_reentry_signal(
                market_state,
                active_risk_flags=["domestic_liquidity_stress_flag"],
            )
        )
        self.assertFalse(
            broad_participation_reentry_signal(
                market_state,
                active_risk_flags=["daily_margin_rally_flag"],
            )
        )
        self.assertFalse(
            broad_participation_reentry_signal(
                dict(market_state, basket_drawdown_6m=-0.08),
                active_risk_flags=[],
            )
        )

    def test_structural_price_cold_start_scores_are_point_in_time(self) -> None:
        snapshot = date(2023, 2, 28)
        metas_by_index = {
            "ai": [
                EquityEtfMeta(
                    "515070.SH",
                    "人工智能ETF华夏",
                    "930713.CSI",
                    "中证人工智能主题指数",
                    date(2019, 12, 24),
                    date(2019, 12, 24),
                )
            ],
            "bond": [
                EquityEtfMeta(
                    "511270.SH",
                    "10年地方债ETF海富通",
                    "931161.CSI",
                    "10年地方债指数",
                    date(2018, 11, 22),
                    date(2018, 11, 22),
                )
            ],
            "old": [
                EquityEtfMeta(
                    "512930.SH",
                    "AI人工智能ETF平安",
                    "930713.CSI",
                    "中证人工智能主题指数",
                    date(2019, 8, 23),
                    date(2019, 8, 23),
                )
            ],
            "extended": [
                EquityEtfMeta(
                    "512560.SH",
                    "军工ETF易方达",
                    "399967.SZ",
                    "中证军工指数",
                    date(2019, 7, 5),
                    date(2019, 7, 5),
                )
            ],
        }
        series = {
            "515070.SH": [
                (snapshot - timedelta(days=offset), 1.0 + (90 - offset) * 0.002)
                for offset in range(90, -1, -1)
            ],
            "511270.SH": [
                (snapshot - timedelta(days=offset), 1.0 + (90 - offset) * 0.001)
                for offset in range(90, -1, -1)
            ],
            "512930.SH": [
                (snapshot - timedelta(days=offset), 1.0 + (90 - offset) * 0.002)
                for offset in range(90, -1, -1)
            ],
            "512560.SH": [
                (
                    snapshot - timedelta(days=offset),
                    1.0
                    + (130 - offset) * 0.005
                    + (0.025 if (130 - offset) % 2 else -0.025),
                )
                for offset in range(130, -1, -1)
            ],
        }
        scores = structural_price_cold_start_scores(
            metas_by_index,
            series,
            snapshot,
            excluded_codes={"512930.SH"},
        )
        self.assertIn("515070.SH", scores)
        self.assertNotIn("511270.SH", scores)
        self.assertNotIn("512930.SH", scores)
        self.assertNotIn("512560.SH", scores)

    def test_structural_cold_start_blocks_short_history_hot_semiconductor(self) -> None:
        snapshot = date(2023, 3, 31)
        days = [snapshot - timedelta(days=offset) for offset in range(80, -1, -1)]
        metas_by_index = {
            "semiconductor": [
                EquityEtfMeta(
                    "588290.SH",
                    "科创芯片ETF华安",
                    "000001.SH",
                    "上证科创板芯片指数",
                    snapshot - timedelta(days=90),
                    snapshot - timedelta(days=80),
                )
            ],
            "communication": [
                EquityEtfMeta(
                    "159994.SZ",
                    "通信ETF银华",
                    "000002.SH",
                    "中证全指通信设备指数",
                    snapshot - timedelta(days=200),
                    snapshot - timedelta(days=180),
                )
            ],
        }
        semiconductor_prices = []
        communication_prices = []
        semiconductor_price = 100.0
        communication_price = 100.0
        for index, day in enumerate(days):
            if index:
                semiconductor_price *= 1.022 if index % 2 else 0.994
                communication_price *= 1.020 if index % 2 else 0.994
            semiconductor_prices.append((day, semiconductor_price))
            communication_prices.append((day, communication_price))
        scores = structural_price_cold_start_scores(
            metas_by_index,
            {
                "588290.SH": semiconductor_prices,
                "159994.SZ": communication_prices,
            },
            snapshot,
        )
        self.assertNotIn("588290.SH", scores)
        self.assertIn("159994.SZ", scores)

    def test_market_recovery_requires_all_configured_trend_confirmations(self) -> None:
        state = {
            "cs300_return_3m": 0.08,
            "cs300_return_6m": 0.12,
            "cs300_ma_6m_distance": 0.03,
            "basket_drawdown_6m": -0.04,
            "domestic_m1_m2_scissors_change_3m": 0.02,
            "basket_vol_3m": 0.18,
        }
        self.assertTrue(market_recovery_signal(state, 0.05, 0.10, 0.0))
        self.assertFalse(market_recovery_signal(state, 0.09, 0.10, 0.0))
        self.assertFalse(market_recovery_signal(state, 0.05, 0.13, 0.0))
        self.assertFalse(market_recovery_signal(state, 0.05, 0.10, 0.04))
        self.assertTrue(
            market_recovery_signal(state, 0.05, 0.10, 0.0, -0.05, 0.0)
        )
        self.assertFalse(
            market_recovery_signal(state, 0.05, 0.10, 0.0, -0.03, 0.0)
        )
        self.assertTrue(
            market_recovery_signal(state, 0.05, 0.10, 0.0, basket_vol_3m_max=0.18)
        )
        self.assertFalse(
            market_recovery_signal(state, 0.05, 0.10, 0.0, basket_vol_3m_max=0.17)
        )
        state["selector_score_candidate_count"] = 6.0
        self.assertTrue(
            market_recovery_signal(
                state, 0.05, 0.10, 0.0, selector_candidate_count_min=5
            )
        )
        self.assertFalse(
            market_recovery_signal(
                state, 0.05, 0.10, 0.0, selector_candidate_count_min=10
            )
        )
        state.pop("selector_score_candidate_count")
        self.assertFalse(
            market_recovery_signal(
                state, 0.05, 0.10, 0.0, selector_candidate_count_min=1
            )
        )
        missing_vol = dict(state)
        missing_vol.pop("basket_vol_3m")
        self.assertFalse(
            market_recovery_signal(
                missing_vol,
                0.05,
                0.10,
                0.0,
                basket_vol_3m_max=0.18,
            )
        )
        missing = dict(state)
        missing.pop("domestic_m1_m2_scissors_change_3m")
        self.assertFalse(
            market_recovery_signal(missing, 0.05, 0.10, 0.0, -0.05, 0.0)
        )

    def test_selector_dispersion_threshold_uses_prior_history_only(self) -> None:
        history = [0.10, 0.20, 0.30, 0.40]
        flagged, threshold = walkforward_upper_tail_signal(history, 0.35, 0.50, 4)
        self.assertTrue(flagged)
        self.assertEqual(threshold, 0.30)
        self.assertEqual(history, [0.10, 0.20, 0.30, 0.40])

    def test_quality_score_uses_only_prior_quarter_percentiles(self) -> None:
        history = {
            "basket_excess_return_6m": [0.01, 0.02, 0.03, 0.04],
            "market_turnover_21d": [1.0, 2.0, 3.0, 4.0],
            "basket_vol_3m": [0.10, 0.20, 0.30, 0.40],
        }
        state = {
            "basket_excess_return_6m": 0.01,
            "market_turnover_21d": 1.0,
            "basket_vol_3m": 0.10,
        }
        decision = walkforward_quality_score(
            history, state, "tail_stable6", minimum_history=4
        )
        self.assertEqual(decision["usable_feature_count"], 3)
        self.assertGreater(float(decision["score"]), 0.80)
        self.assertEqual(len(history["basket_vol_3m"]), 4)
        observe_quality_features(history, state, "tail_stable6")
        self.assertEqual(len(history["basket_vol_3m"]), 5)
        disabled = walkforward_quality_score(history, state, None, minimum_history=4)
        self.assertIsNone(disabled["score"])

    def test_pboc_tone_score_is_a_strictly_prior_online_percentile(self) -> None:
        history = {"pboc_outlook_net_tone": [-2.0, -1.0, 0.0, 1.0]}
        state = {"pboc_outlook_net_tone": 2.0}
        decision = walkforward_quality_score(
            history, state, "pboc_tone1", minimum_history=4
        )
        self.assertEqual(decision["usable_feature_count"], 1)
        self.assertEqual(decision["score"], 1.0)
        self.assertEqual(history["pboc_outlook_net_tone"], [-2.0, -1.0, 0.0, 1.0])

    def test_quality_multiplier_keeps_cushion_logic_separate(self) -> None:
        self.assertEqual(
            quality_adjusted_cushion_multiplier(
                4.25, {"score": 0.80}, 0.70, 8.0, 0.20, 2.0
            ),
            8.0,
        )
        self.assertEqual(
            quality_adjusted_cushion_multiplier(
                4.25, {"score": 0.10}, 0.70, 8.0, 0.20, 2.0
            ),
            2.0,
        )
        self.assertEqual(
            quality_adjusted_cushion_multiplier(
                4.25, {"score": None}, 0.70, 8.0, 0.20, 2.0
            ),
            4.25,
        )

    def test_quality_multiplier_trend_confirmation_requires_all_inputs(self) -> None:
        state = {
            "cs300_return_3m": 0.01,
            "cs300_return_6m": 0.02,
            "cs300_ma_6m_distance": 0.01,
            "basket_drawdown_6m": -0.04,
        }
        self.assertTrue(quality_multiplier_trend_confirmed(state))
        self.assertFalse(
            quality_multiplier_trend_confirmed(dict(state, cs300_return_3m=-0.01))
        )
        missing = dict(state)
        missing.pop("basket_drawdown_6m")
        self.assertFalse(quality_multiplier_trend_confirmed(missing))

    def test_crisis_rebound_block_requires_visible_price_damage(self) -> None:
        self.assertTrue(
            crisis_rebound_blocks_quality_acceleration(
                {
                    "basket_drawdown_6m": -0.38,
                    "cs300_return_3m": -0.19,
                    "external_vix_percentile_3y": 0.92,
                }
            )
        )
        self.assertTrue(
            crisis_rebound_blocks_quality_acceleration(
                {
                    "basket_drawdown_6m": -0.10,
                    "cs300_return_3m": -0.10,
                    "external_vix_percentile_3y": 0.21,
                }
            )
        )
        self.assertFalse(
            crisis_rebound_blocks_quality_acceleration(
                {
                    "basket_drawdown_6m": -0.04,
                    "cs300_return_3m": 0.03,
                    "external_vix_percentile_3y": 0.30,
                }
            )
        )
        self.assertEqual(
            crisis_rebound_state(
                {
                    "basket_drawdown_6m": -0.38,
                    "cs300_return_3m": -0.19,
                    "external_vix_percentile_3y": 0.92,
                }
            ),
            "severe",
        )
        self.assertEqual(
            crisis_rebound_state(
                {
                    "basket_drawdown_6m": -0.10,
                    "cs300_return_3m": -0.10,
                    "external_vix_percentile_3y": 0.21,
                }
            ),
            "correction",
        )

    def test_option_panic_after_rally_cap_signal_requires_supported_high_vol_rally(self) -> None:
        market_state = {
            "cs300_return_3m": 0.006,
            "cs300_return_6m": 0.16,
            "basket_return_1m": 0.076,
            "basket_drawdown_6m": -0.064,
            "basket_vol_3m": 0.317,
            "pboc_outlook_net_tone": 25.0,
            "domestic_m1_m2_scissors_change_3m": 7.0,
        }
        self.assertTrue(
            option_panic_after_rally_cap_signal(
                market_state,
                ["option_panic_after_rally_flag"],
            )
        )

        weak_policy = dict(market_state)
        weak_policy["pboc_outlook_net_tone"] = -5.0
        self.assertFalse(
            option_panic_after_rally_cap_signal(
                weak_policy,
                ["option_panic_after_rally_flag"],
            )
        )

    def test_full_case_matrix_requires_every_phase_and_lag(self) -> None:
        cases = [
            {
                "phase_month_offset": phase,
                "execution_lag_days": lag,
                "final_capital": 40_000_000.0,
                "max_drawdown": -0.10,
            }
            for phase in STRICT_OBJECTIVE.phase_offsets
            for lag in STRICT_OBJECTIVE.execution_lags
        ]
        result = validate_case_matrix(cases)
        self.assertTrue(result["matrix_complete"])
        self.assertTrue(result["all_cases_pass"])

        result = validate_case_matrix(cases[:-1])
        self.assertFalse(result["matrix_complete"])
        self.assertFalse(result["all_cases_pass"])

    def test_any_numerical_failure_fails_the_objective(self) -> None:
        cases = [
            {
                "phase_month_offset": phase,
                "execution_lag_days": lag,
                "final_capital": 40_000_000.0,
                "max_drawdown": -0.10,
            }
            for phase in STRICT_OBJECTIVE.phase_offsets
            for lag in STRICT_OBJECTIVE.execution_lags
        ]
        cases[7]["max_drawdown"] = -0.20001
        result = validate_case_matrix(cases)
        self.assertFalse(result["all_cases_pass"])
        self.assertEqual(len(result["failed_cases"]), 1)

    def test_monthly_or_daily_weight_change_is_rejected(self) -> None:
        rows = [
            {"decision_date": "2024-01-31", "target_weights": {"510300.SH": 1.0}},
            {"decision_date": "2024-02-29", "target_weights": {"510300.SH": 0.5}},
            {"decision_date": "2024-04-30", "target_weights": {"510300.SH": 0.5}},
        ]
        violations = validate_quarterly_weight_path(rows)
        self.assertEqual(len(violations), 1)
        self.assertIn("after 1 months", violations[0])

    def test_quarterly_changes_and_unchanged_monthly_marks_are_allowed(self) -> None:
        rows = [
            {"decision_date": "2024-01-31", "target_weights": {"510300.SH": 0.8}},
            {"decision_date": "2024-02-29", "target_weights": {"510300.SH": 0.8}},
            {"decision_date": "2024-03-31", "target_weights": {"510300.SH": 0.8}},
            {"decision_date": "2024-04-30", "target_weights": {"510500.SH": 0.8}},
        ]
        self.assertEqual(validate_quarterly_weight_path(rows), [])

    def test_strict_path_requires_exact_three_month_rebalance_spacing(self) -> None:
        valid_rows = [
            {
                "decision_date": "2024-02-29",
                "rebalance_anchor": "2024-01-31",
                "target_weights": {"510300.SH": 0.8},
            },
            {
                "decision_date": "2024-05-08",
                "rebalance_anchor": "2024-04-30",
                "target_weights": {"510500.SH": 0.8},
            },
            {
                "decision_date": "2024-08-07",
                "rebalance_anchor": "2024-07-31",
                "target_weights": {"510300.SH": 0.8},
            },
        ]
        self.assertEqual(
            validate_quarterly_weight_path(
                valid_rows,
                require_exact_rebalance_spacing=True,
            ),
            [],
        )

        skipped_quarter = [valid_rows[0], valid_rows[2]]
        violations = validate_quarterly_weight_path(
            skipped_quarter,
            require_exact_rebalance_spacing=True,
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("required exactly 3", violations[0])

    def test_target_asset_gate_rejects_qdii_enhanced_and_unknown(self) -> None:
        targets = [
            {"ts_code": "510300.SH", "target_weight_pct": 40.0},
            {"ts_code": "513500.SH", "target_weight_pct": 20.0},
            {"ts_code": "159999.SZ", "target_weight_pct": 20.0},
            {"ts_code": "CASH", "target_weight_pct": 20.0},
        ]
        metas = {
            "510300.SH": {"etf_type": "股票型", "is_enhanced": 0},
            "513500.SH": {"etf_type": "QDII", "is_enhanced": 0},
            "159999.SZ": {"etf_type": "股票型", "is_enhanced": 1},
        }
        violations = validate_target_assets(targets, metas)
        self.assertEqual(len(violations), 2)


if __name__ == "__main__":
    unittest.main()
