"""Point-in-time market features for calendar-neutral phase tests."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from backtest.pboc_report_features import PbocReport, report_features_as_of


@dataclass(frozen=True)
class DatedSeries:
    dates: tuple[date, ...]
    values: tuple[float, ...]

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[date, float]]) -> "DatedSeries":
        clean = sorted((day, float(value)) for day, value in rows if value is not None)
        return cls(
            tuple(day for day, _value in clean),
            tuple(value for _day, value in clean),
        )

    def cutoff_index(self, snapshot: date, *, include_snapshot: bool) -> int:
        if include_snapshot:
            return bisect_right(self.dates, snapshot)
        return bisect_left(self.dates, snapshot)

    def trailing(
        self,
        snapshot: date,
        observations: int,
        *,
        include_snapshot: bool,
    ) -> tuple[float, ...]:
        end = self.cutoff_index(snapshot, include_snapshot=include_snapshot)
        return self.values[max(0, end - observations) : end]

    def value_at(self, snapshot: date, *, include_snapshot: bool) -> float | None:
        values = self.trailing(snapshot, 1, include_snapshot=include_snapshot)
        return values[-1] if values else None


def trailing_return(values: Sequence[float], observations: int) -> float | None:
    if len(values) < observations + 1:
        return None
    start = float(values[-observations - 1])
    return float(values[-1]) / start - 1.0 if start > 0 else None


def realized_volatility(values: Sequence[float], observations: int) -> float | None:
    if len(values) < observations + 1:
        return None
    window = values[-observations - 1 :]
    returns = [float(cur) / float(prev) - 1.0 for prev, cur in zip(window, window[1:]) if prev > 0]
    if len(returns) < observations:
        return None
    return statistics.pstdev(returns) * math.sqrt(252.0)


def rolling_drawdown(values: Sequence[float], observations: int) -> float | None:
    if len(values) < observations:
        return None
    window = values[-observations:]
    peak = max(window)
    return float(window[-1]) / float(peak) - 1.0 if peak > 0 else None


def moving_average_distance(values: Sequence[float], observations: int) -> float | None:
    if len(values) < observations:
        return None
    window = values[-observations:]
    average = statistics.mean(window)
    return float(window[-1]) / average - 1.0 if average > 0 else None


def percentile_rank(values: Sequence[float], observations: int) -> float | None:
    if len(values) < observations:
        return None
    window = values[-observations:]
    current = window[-1]
    return sum(value <= current for value in window) / len(window)


def rolling_volatility_percentile(
    values: Sequence[float],
    volatility_observations: int,
    history_observations: int,
) -> float | None:
    required = volatility_observations + history_observations
    if len(values) < required:
        return None
    window = [float(value) for value in values[-required:]]
    returns = [cur / prev - 1.0 for prev, cur in zip(window, window[1:]) if prev > 0]
    if len(returns) < volatility_observations + history_observations - 1:
        return None
    volatilities = []
    rolling_sum = sum(returns[:volatility_observations])
    rolling_sum_sq = sum(value * value for value in returns[:volatility_observations])
    for end in range(volatility_observations, len(returns) + 1):
        if end > volatility_observations:
            added = returns[end - 1]
            removed = returns[end - volatility_observations - 1]
            rolling_sum += added - removed
            rolling_sum_sq += added * added - removed * removed
        average = rolling_sum / volatility_observations
        variance = max(0.0, rolling_sum_sq / volatility_observations - average * average)
        volatilities.append(math.sqrt(variance) * math.sqrt(252.0))
    return percentile_rank(volatilities, history_observations)


def aligned_difference_series(left: DatedSeries, right: DatedSeries) -> DatedSeries:
    """Subtract two series only on dates known for both inputs."""

    common = sorted(set(left.dates) & set(right.dates))
    left_by_date = dict(zip(left.dates, left.values))
    right_by_date = dict(zip(right.dates, right.values))
    return DatedSeries.from_rows(
        (day, left_by_date[day] - right_by_date[day]) for day in common
    )


def aggregate_etf_share_growth_features(
    histories: dict[str, tuple[tuple[date, ...], tuple[date, ...], tuple[float, ...]]],
    snapshot: date,
) -> dict[str, float | None]:
    """Aggregate latest known quarterly ETF share changes without backfill.

    Histories contain trade dates, conservative availability dates, and
    exchange-reported total shares.  The current quarter-end record is not
    usable on its own trade date because the exchange publishes it next day.
    """

    maximum_observation_age_days = 45
    maximum_horizon_months = 4
    minimum_cross_section_size = 30
    growth = []
    observation_ages = []
    horizon_months = []
    for trade_dates, available_dates, shares in histories.values():
        end = bisect_right(available_dates, snapshot)
        if end < 2:
            continue
        latest = end - 1
        prior = next(
            (
                index
                for index in range(end - 2, -1, -1)
                if (
                    (trade_dates[latest].year - trade_dates[index].year) * 12
                    + trade_dates[latest].month
                    - trade_dates[index].month
                )
                >= 3
            ),
            None,
        )
        if prior is None:
            continue
        observation_age = (snapshot - trade_dates[latest]).days
        horizon = (
            (trade_dates[latest].year - trade_dates[prior].year) * 12
            + trade_dates[latest].month
            - trade_dates[prior].month
        )
        if observation_age > maximum_observation_age_days:
            continue
        if horizon > maximum_horizon_months:
            continue
        previous = float(shares[prior])
        current = float(shares[latest])
        if previous <= 0 or current <= 0:
            continue
        growth.append(current / previous - 1.0)
        observation_ages.append(observation_age)
        horizon_months.append(horizon)
    if len(growth) < minimum_cross_section_size:
        return {
            "etf_share_growth_1q_median": None,
            "etf_share_growth_1q_positive_ratio": None,
            "etf_share_growth_1q_mean_winsor": None,
            "etf_share_growth_1q_q90": None,
            "etf_share_growth_1q_candidate_count": float(len(growth)),
            "etf_share_growth_1q_max_observation_age_days": (
                float(max(observation_ages)) if observation_ages else None
            ),
            "etf_share_growth_1q_max_horizon_months": (
                float(max(horizon_months)) if horizon_months else None
            ),
        }
    ordered = sorted(growth)
    winsorized = [min(max(value, -0.95), 5.0) for value in growth]
    return {
        "etf_share_growth_1q_median": statistics.median(growth),
        "etf_share_growth_1q_positive_ratio": (
            sum(value > 0.0 for value in growth) / len(growth)
        ),
        "etf_share_growth_1q_mean_winsor": statistics.mean(winsorized),
        "etf_share_growth_1q_q90": ordered[int(0.90 * (len(ordered) - 1))],
        "etf_share_growth_1q_candidate_count": float(len(growth)),
        "etf_share_growth_1q_max_observation_age_days": float(
            max(observation_ages)
        ),
        "etf_share_growth_1q_max_horizon_months": float(max(horizon_months)),
    }


class PhaseFeatureStore:
    """Load each price series once and compute only point-in-time features.

    China index closes may include the snapshot date because formal execution is
    strictly later. External closes exclude the snapshot date to avoid using a
    US-market close that was not observable before the next A-share open.
    """

    def __init__(self) -> None:
        self._index: dict[str, DatedSeries] = {}
        self._index_basic: dict[tuple[str, str], DatedSeries] = {}
        self._external: dict[str, DatedSeries] = {}
        self._external_spreads: dict[tuple[str, str], DatedSeries] = {}
        self._us_curve: DatedSeries | None = None
        self._shibor_on: DatedSeries | None = None
        self._money_supply: list[tuple[str, float, float]] | None = None
        self._macro_monthly: dict[str, list[tuple[date, float]]] = {}
        self._fund_issuance_monthly: list[tuple[str, float, float, float]] | None = None
        self._daily_margin_balance: DatedSeries | None = None
        self._daily_margin_flow: DatedSeries | None = None
        self._option_put_call_volume: DatedSeries | None = None
        self._option_put_call_oi: DatedSeries | None = None
        self._pboc_reports: list[PbocReport] | None = None
        self._etf_share_histories: dict[
            str, tuple[tuple[date, ...], tuple[date, ...], tuple[float, ...]]
        ] | None = None
        self._snapshot_cache: dict[
            tuple[tuple[str, ...], str, date], dict[str, float | None]
        ] = {}

    def index_series(self, cur, code: str) -> DatedSeries:
        if code not in self._index:
            cur.execute(
                """
                SELECT trade_date, close
                FROM index_daily
                WHERE ts_code=%s AND close IS NOT NULL
                ORDER BY trade_date
                """,
                (code,),
            )
            self._index[code] = DatedSeries.from_rows(cur.fetchall())
        return self._index[code]

    def external_series(self, cur, symbol: str) -> DatedSeries:
        if symbol not in self._external:
            cur.execute(
                """
                SELECT trade_date, COALESCE(adj_close, close)
                FROM external_asset_daily
                WHERE symbol=%s AND COALESCE(adj_close, close) IS NOT NULL
                ORDER BY trade_date
                """,
                (symbol,),
            )
            self._external[symbol] = DatedSeries.from_rows(cur.fetchall())
        return self._external[symbol]

    def external_spread_series(self, cur, left: str, right: str) -> DatedSeries:
        key = (left, right)
        if key not in self._external_spreads:
            self._external_spreads[key] = aligned_difference_series(
                self.external_series(cur, left),
                self.external_series(cur, right),
            )
        return self._external_spreads[key]

    def domestic_yield_curve_features(
        self, cur, snapshot: date
    ) -> dict[str, float | None]:
        """ChinaBond curve features observable after the prior A-share close."""

        def describe(prefix: str, series: DatedSeries) -> dict[str, float | None]:
            values = series.trailing(snapshot, 780, include_snapshot=True)
            return {
                f"{prefix}_level": values[-1] if values else None,
                f"{prefix}_change_1m": (
                    values[-1] - values[-22] if len(values) >= 22 else None
                ),
                f"{prefix}_change_3m": (
                    values[-1] - values[-64] if len(values) >= 64 else None
                ),
                f"{prefix}_percentile_3y": percentile_rank(values, 756),
            }

        output: dict[str, float | None] = {}
        output.update(
            describe(
                "domestic_gov10y",
                self.external_series(cur, "CN_GOV_10Y"),
            )
        )
        output.update(
            describe(
                "domestic_gov_curve_10y1y",
                self.external_spread_series(cur, "CN_GOV_10Y", "CN_GOV_1Y"),
            )
        )
        output.update(
            describe(
                "domestic_bank_aaa_gov_spread_3y",
                self.external_spread_series(
                    cur, "CN_BANK_AAA_3Y", "CN_GOV_3Y"
                ),
            )
        )
        output.update(
            describe(
                "domestic_mtn_aaa_gov_spread_3y",
                self.external_spread_series(cur, "CN_MTN_AAA_3Y", "CN_GOV_3Y"),
            )
        )
        return output

    def index_basic_series(self, cur, code: str, field: str) -> DatedSeries:
        if field not in {"turnover_rate_f", "pe_ttm", "pb"}:
            raise ValueError(f"unsupported index daily-basic field: {field}")
        key = (code, field)
        if key not in self._index_basic:
            cur.execute(
                f"""
                SELECT trade_date, {field}
                FROM index_dailybasic
                WHERE ts_code=%s AND {field} IS NOT NULL AND {field}>0
                ORDER BY trade_date
                """,
                (code,),
            )
            self._index_basic[key] = DatedSeries.from_rows(cur.fetchall())
        return self._index_basic[key]

    def market_activity_features(
        self,
        cur,
        code: str,
        snapshot: date,
    ) -> dict[str, float | None]:
        turnover = self.index_basic_series(cur, code, "turnover_rate_f").trailing(
            snapshot,
            800,
            include_snapshot=True,
        )
        turnover_21d = [
            statistics.mean(turnover[index - 20 : index + 1])
            for index in range(20, len(turnover))
        ]
        pe = self.index_basic_series(cur, code, "pe_ttm").trailing(
            snapshot,
            800,
            include_snapshot=True,
        )
        pb = self.index_basic_series(cur, code, "pb").trailing(
            snapshot,
            800,
            include_snapshot=True,
        )
        return {
            "market_turnover_21d": turnover_21d[-1] if turnover_21d else None,
            "market_turnover_change_1m": (
                turnover_21d[-1] / turnover_21d[-22] - 1.0
                if len(turnover_21d) >= 22 and turnover_21d[-22] > 0
                else None
            ),
            "market_turnover_percentile_3y": percentile_rank(turnover_21d, 756),
            "market_pe_ttm_percentile_3y": percentile_rank(pe, 756),
            "market_pb_percentile_3y": percentile_rank(pb, 756),
        }

    def daily_margin_features(self, cur, snapshot: date) -> dict[str, float | None]:
        if self._daily_margin_balance is None or self._daily_margin_flow is None:
            cur.execute(
                """
                SELECT trade_date,
                       SUM(rzye),
                       (SUM(COALESCE(rzmre,0))-SUM(COALESCE(rzche,0)))
                         / NULLIF(SUM(rzye),0)
                FROM margin_daily
                WHERE rzye IS NOT NULL
                GROUP BY trade_date
                ORDER BY trade_date
                """
            )
            rows = cur.fetchall()
            self._daily_margin_balance = DatedSeries.from_rows(
                (day, balance) for day, balance, _flow in rows if balance is not None
            )
            self._daily_margin_flow = DatedSeries.from_rows(
                (day, flow) for day, _balance, flow in rows if flow is not None
            )
        balance = self._daily_margin_balance.trailing(
            snapshot,
            800,
            include_snapshot=True,
        )
        flow = self._daily_margin_flow.trailing(
            snapshot,
            800,
            include_snapshot=True,
        )
        flow_21d = [sum(flow[index - 20 : index + 1]) for index in range(20, len(flow))]
        return {
            "daily_margin_balance_return_1m": trailing_return(balance, 21),
            "daily_margin_balance_return_3m": trailing_return(balance, 63),
            "daily_margin_net_flow_21d": flow_21d[-1] if flow_21d else None,
            "daily_margin_net_flow_percentile_3y": percentile_rank(flow_21d, 756),
        }

    def fund_issuance_features(self, cur, snapshot: date) -> dict[str, float | None]:
        if self._fund_issuance_monthly is None:
            cur.execute(
                """
                SELECT month, new_fund_billion, active_billion, new_fund_count
                FROM cn_fund_new_monthly
                ORDER BY month
                """
            )
            self._fund_issuance_monthly = [
                (str(month), float(total or 0), float(active or 0), float(count or 0))
                for month, total, active, count in cur.fetchall()
            ]
        month_index = snapshot.year * 12 + snapshot.month - 2
        cutoff = f"{month_index // 12:04d}{month_index % 12 + 1:02d}"
        months = [row[0] for row in self._fund_issuance_monthly]
        end = bisect_right(months, cutoff)
        window = self._fund_issuance_monthly[max(0, end - 36) : end]
        active = [row[2] for row in window]
        total = [row[1] for row in window]
        return {
            "fund_active_issuance_billion": active[-1] if active else None,
            "fund_active_issuance_percentile_3y": (
                percentile_rank(active, 36) if len(active) >= 36 else None
            ),
            "fund_total_issuance_billion": total[-1] if total else None,
            "fund_total_issuance_percentile_3y": (
                percentile_rank(total, 36) if len(total) >= 36 else None
            ),
        }

    def shibor_on_series(self, cur) -> DatedSeries:
        if self._shibor_on is None:
            cur.execute(
                """
                SELECT trade_date, rate_on
                FROM shibor_daily
                WHERE rate_on IS NOT NULL
                ORDER BY trade_date
                """
            )
            self._shibor_on = DatedSeries.from_rows(cur.fetchall())
        return self._shibor_on

    def money_supply_features(self, cur, snapshot: date) -> dict[str, float | None]:
        if self._money_supply is None:
            cur.execute(
                """
                SELECT month, m1_yoy, m2_yoy
                FROM cn_m_monthly
                WHERE m1_yoy IS NOT NULL AND m2_yoy IS NOT NULL
                ORDER BY month
                """
            )
            self._money_supply = [
                (str(month), float(m1_yoy), float(m2_yoy))
                for month, m1_yoy, m2_yoy in cur.fetchall()
            ]
        month_index = snapshot.year * 12 + snapshot.month - 2
        cutoff = f"{month_index // 12:04d}{month_index % 12 + 1:02d}"
        months = [row[0] for row in self._money_supply]
        end = bisect_right(months, cutoff)
        window = self._money_supply[max(0, end - 4) : end]
        if not window:
            return {
                "domestic_m1_m2_scissors": None,
                "domestic_m1_m2_scissors_change_3m": None,
            }
        scissors = [m1_yoy - m2_yoy for _month, m1_yoy, m2_yoy in window]
        return {
            "domestic_m1_m2_scissors": scissors[-1],
            "domestic_m1_m2_scissors_change_3m": (
                scissors[-1] - scissors[0] if len(scissors) >= 4 else None
            ),
        }

    def macro_monthly_series(self, cur, indicator: str) -> list[tuple[date, float]]:
        if indicator not in self._macro_monthly:
            cur.execute(
                """
                SELECT period, value
                FROM macro_monthly
                WHERE indicator=%s AND value IS NOT NULL
                ORDER BY period
                """,
                (indicator,),
            )
            self._macro_monthly[indicator] = [
                (period, float(value)) for period, value in cur.fetchall()
            ]
        return self._macro_monthly[indicator]

    def domestic_macro_features(self, cur, snapshot: date) -> dict[str, float | None]:
        """Calendar-neutral macro transforms using only the prior reference month."""
        month_index = snapshot.year * 12 + snapshot.month - 2
        cutoff = date(month_index // 12, month_index % 12 + 1, 1)

        def window(indicator: str, observations: int) -> list[float]:
            rows = self.macro_monthly_series(cur, indicator)
            end = bisect_right([row[0] for row in rows], cutoff)
            return [value for _period, value in rows[max(0, end - observations) : end]]

        social_financing = window("sf_inc_month", 24)
        sf_latest_12m = sum(social_financing[-12:]) if len(social_financing) >= 12 else None
        sf_previous_12m = sum(social_financing[-24:-12]) if len(social_financing) >= 24 else None
        sf_latest_3m = sum(social_financing[-3:]) if len(social_financing) >= 15 else None
        sf_year_ago_3m = sum(social_financing[-15:-12]) if len(social_financing) >= 15 else None

        pmi_production = window("pmi_mfg", 4)
        # macro_monthly does not carry PMI sub-indices, so use the normalized
        # manufacturing level and its rolling change without month labels.
        pmi_level = pmi_production[-1] if pmi_production else None
        pmi_change_3m = (
            pmi_production[-1] - pmi_production[-4]
            if len(pmi_production) >= 4
            else None
        )

        ppi = window("ppi_yoy", 4)
        cpi = window("cpi_yoy", 4)
        price_scissors = [left - right for left, right in zip(ppi, cpi)]
        margin_balance = window("margin_balance", 4)
        return {
            "domestic_sf_rolling_12m": sf_latest_12m,
            "domestic_sf_rolling_12m_growth": (
                sf_latest_12m / sf_previous_12m - 1.0
                if sf_latest_12m is not None
                and sf_previous_12m is not None
                and sf_previous_12m > 0
                else None
            ),
            "domestic_sf_rolling_3m_yoy": (
                sf_latest_3m / sf_year_ago_3m - 1.0
                if sf_latest_3m is not None
                and sf_year_ago_3m is not None
                and sf_year_ago_3m > 0
                else None
            ),
            "domestic_pmi_mfg_level": pmi_level,
            "domestic_pmi_mfg_change_3m": pmi_change_3m,
            # A fixed, point-in-time extreme-growth threshold.  This is kept
            # deliberately simple so the 2010 reversal diagnostic can be
            # tested without fitting a threshold on future returns.
            "pmi_extreme_growth_reversal_flag": float(
                pmi_level is not None and pmi_level >= 54.0
            ),
            "domestic_ppi_cpi_scissors": price_scissors[-1] if price_scissors else None,
            "domestic_ppi_cpi_scissors_change_3m": (
                price_scissors[-1] - price_scissors[-4]
                if len(price_scissors) >= 4
                else None
            ),
            "domestic_margin_balance_return_1m": (
                margin_balance[-1] / margin_balance[-2] - 1.0
                if len(margin_balance) >= 2 and margin_balance[-2] > 0
                else None
            ),
            "domestic_margin_balance_return_3m": (
                margin_balance[-1] / margin_balance[-4] - 1.0
                if len(margin_balance) >= 4 and margin_balance[-4] > 0
                else None
            ),
        }

    def option_sentiment_series(self, cur) -> tuple[DatedSeries, DatedSeries]:
        if self._option_put_call_volume is None or self._option_put_call_oi is None:
            cur.execute(
                """
                SELECT d.trade_date,
                       SUM(CASE WHEN a.call_put='P' THEN COALESCE(d.vol,0) ELSE 0 END)
                         / NULLIF(SUM(CASE WHEN a.call_put='C' THEN COALESCE(d.vol,0) ELSE 0 END),0),
                       SUM(CASE WHEN a.call_put='P' THEN COALESCE(d.oi,0) ELSE 0 END)
                         / NULLIF(SUM(CASE WHEN a.call_put='C' THEN COALESCE(d.oi,0) ELSE 0 END),0)
                FROM cn_option_daily d
                JOIN cn_option_contract_archive a ON a.option_ts_code=d.ts_code
                GROUP BY d.trade_date
                ORDER BY d.trade_date
                """
            )
            rows = cur.fetchall()
            self._option_put_call_volume = DatedSeries.from_rows(
                (day, ratio) for day, ratio, _oi_ratio in rows if ratio is not None
            )
            self._option_put_call_oi = DatedSeries.from_rows(
                (day, ratio) for day, _volume_ratio, ratio in rows if ratio is not None
            )
        return self._option_put_call_volume, self._option_put_call_oi

    def option_sentiment_features(self, cur, snapshot: date) -> dict[str, float | None]:
        volume_series, oi_series = self.option_sentiment_series(cur)

        def features(series: DatedSeries, prefix: str) -> dict[str, float | None]:
            end = series.cutoff_index(snapshot, include_snapshot=True)
            latest_date = series.dates[end - 1] if end else None
            age_days = (snapshot - latest_date).days if latest_date is not None else None
            if age_days is None or age_days > 45:
                return {
                    f"{prefix}_21d": None,
                    f"{prefix}_change_1m": None,
                    f"{prefix}_percentile_3y": None,
                    f"{prefix}_age_days": age_days,
                }
            values = series.trailing(snapshot, 800, include_snapshot=True)
            rolling_21 = [
                statistics.mean(values[index - 20 : index + 1])
                for index in range(20, len(values))
            ]
            current = rolling_21[-1] if rolling_21 else None
            previous = (
                statistics.mean(values[-42:-21]) if len(values) >= 42 else None
            )
            return {
                f"{prefix}_21d": current,
                f"{prefix}_change_1m": (
                    current / previous - 1.0
                    if current is not None and previous is not None and previous > 0
                    else None
                ),
                f"{prefix}_percentile_3y": percentile_rank(rolling_21, 756),
                f"{prefix}_age_days": age_days,
            }

        return {
            **features(volume_series, "domestic_option_put_call_volume"),
            **features(oi_series, "domestic_option_put_call_oi"),
        }

    def pboc_report_features(self, cur, snapshot: date) -> dict[str, float | None]:
        if self._pboc_reports is None:
            cur.execute(
                """
                SELECT pub_date, content_html
                FROM pboc_monetary_policy
                WHERE content_html IS NOT NULL
                ORDER BY pub_date
                """
            )
            self._pboc_reports = [
                PbocReport(publication_date=day, content=str(content))
                for day, content in cur.fetchall()
                if day is not None and content
            ]
        return report_features_as_of(self._pboc_reports, snapshot)

    def etf_share_market_features(
        self, cur, snapshot: date
    ) -> dict[str, float | None]:
        if self._etf_share_histories is None:
            cur.execute(
                """
                SELECT ts_code, list_date, extname, index_name, etf_type,
                       is_enhanced
                FROM passive_etf
                WHERE list_date IS NOT NULL
                """
            )
            overseas = (
                "港股", "沪港深", "恒生", "纳指", "标普", "日经", "德国",
                "法国", "美国", "中概", "海外", "全球", "东南亚", "沙特",
            )
            defensive = (
                "货币", "保证金", "现金", "国债", "政金债", "信用债",
                "公司债", "城投债", "地方债", "可转债", "黄金", "上海金",
            )
            eligible: dict[str, date] = {}
            for code, listed, name, index_name, etf_type, enhanced in cur.fetchall():
                text = f"{name or ''} {index_name or ''}"
                code = str(code)
                if (
                    str(etf_type or "").upper() == "QDII"
                    or bool(enhanced)
                    or code.startswith(("511", "513", "517", "518", "520"))
                    or any(keyword in text for keyword in overseas + defensive)
                ):
                    continue
                eligible[code] = listed
            cur.execute(
                """
                SELECT ts_code, trade_date, available_date, total_share_wan
                FROM etf_share_size_snapshot
                ORDER BY ts_code, available_date, trade_date
                """
            )
            grouped: dict[str, list[tuple[date, date, float]]] = {}
            for code, trade_date, available_date, shares in cur.fetchall():
                code = str(code)
                listed = eligible.get(code)
                if listed is None or trade_date < listed or shares is None:
                    continue
                grouped.setdefault(code, []).append(
                    (trade_date, available_date, float(shares))
                )
            self._etf_share_histories = {
                code: (
                    tuple(row[0] for row in rows),
                    tuple(row[1] for row in rows),
                    tuple(row[2] for row in rows),
                )
                for code, rows in grouped.items()
            }
        return aggregate_etf_share_growth_features(
            self._etf_share_histories, snapshot
        )

    @staticmethod
    def _series_features(prefix: str, values: Sequence[float]) -> dict[str, float | None]:
        return {
            f"{prefix}_return_1m": trailing_return(values, 21),
            f"{prefix}_return_3m": trailing_return(values, 63),
            f"{prefix}_return_6m": trailing_return(values, 126),
            f"{prefix}_vol_1m": realized_volatility(values, 21),
            f"{prefix}_vol_3m": realized_volatility(values, 63),
            f"{prefix}_drawdown_1m": rolling_drawdown(values, 21),
            f"{prefix}_drawdown_3m": rolling_drawdown(values, 63),
            f"{prefix}_drawdown_6m": rolling_drawdown(values, 126),
            f"{prefix}_ma_3m_distance": moving_average_distance(values, 63),
            f"{prefix}_ma_6m_distance": moving_average_distance(values, 126),
            f"{prefix}_ma_10m_distance": moving_average_distance(values, 210),
        }

    def snapshot_features(
        self,
        cur,
        holding_codes: Sequence[str],
        benchmark_code: str,
        snapshot: date,
    ) -> dict[str, float | None]:
        cache_key = (tuple(sorted(str(code) for code in holding_codes)), benchmark_code, snapshot)
        cached = self._snapshot_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        benchmark = self.index_series(cur, benchmark_code).trailing(
            snapshot,
            1_020,
            include_snapshot=True,
        )
        features = self._series_features("cs300", benchmark)
        features.update(self.market_activity_features(cur, benchmark_code, snapshot))
        features["cs300_vol_1m_percentile_3y"] = rolling_volatility_percentile(
            benchmark,
            21,
            756,
        )

        holding_windows = [
            self.index_series(cur, code).trailing(snapshot, 260, include_snapshot=True)
            for code in holding_codes
        ]
        individual = [self._series_features("holding", values) for values in holding_windows]
        for suffix in (
            "return_1m",
            "return_3m",
            "return_6m",
            "vol_1m",
            "vol_3m",
            "drawdown_1m",
            "drawdown_3m",
            "drawdown_6m",
            "ma_3m_distance",
            "ma_6m_distance",
            "ma_10m_distance",
        ):
            values = [row[f"holding_{suffix}"] for row in individual]
            usable = [float(value) for value in values if value is not None]
            features[f"basket_{suffix}"] = statistics.mean(usable) if usable else None
            if suffix in {"return_1m", "return_3m", "return_6m"}:
                features[f"basket_{suffix}_dispersion"] = (
                    statistics.pstdev(usable) if len(usable) >= 2 else None
                )
                features[f"basket_{suffix}_max"] = max(usable) if usable else None

        for horizon in ("1m", "3m", "6m"):
            basket_return = features.get(f"basket_return_{horizon}")
            benchmark_return = features.get(f"cs300_return_{horizon}")
            features[f"basket_excess_return_{horizon}"] = (
                float(basket_return) - float(benchmark_return)
                if basket_return is not None and benchmark_return is not None
                else None
            )

        for suffix in ("return_1m", "return_3m", "return_6m", "ma_3m_distance", "ma_6m_distance", "ma_10m_distance"):
            values = [row[f"holding_{suffix}"] for row in individual]
            usable = [float(value) for value in values if value is not None]
            features[f"breadth_{suffix}_positive"] = (
                sum(value > 0.0 for value in usable) / len(usable) if usable else None
            )

        cs300_overheat = (
            features.get("cs300_return_3m") is not None
            and features.get("cs300_ma_6m_distance") is not None
            and features.get("cs300_vol_1m_percentile_3y") is not None
            and float(features["cs300_return_3m"]) >= 0.30
            and float(features["cs300_ma_6m_distance"]) >= 0.20
            and float(features["cs300_vol_1m_percentile_3y"]) >= 0.90
        )
        basket_overheat = (
            features.get("basket_return_3m") is not None
            and features.get("basket_ma_3m_distance") is not None
            and features.get("breadth_return_3m_positive") is not None
            and float(features["basket_return_3m"]) >= 0.40
            and float(features["basket_ma_3m_distance"]) >= 0.18
            and float(features["breadth_return_3m_positive"]) >= 0.80
        )
        rebound_overheat = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_ma_6m_distance") is not None
            and features.get("breadth_return_6m_positive") is not None
            and float(features["cs300_return_6m"]) >= 0.80
            and float(features["cs300_ma_6m_distance"]) >= 0.30
            and float(features["breadth_return_6m_positive"]) >= 0.80
        )
        short_cycle_overheat = (
            features.get("basket_return_3m") is not None
            and features.get("cs300_ma_6m_distance") is not None
            and features.get("cs300_vol_1m_percentile_3y") is not None
            and features.get("breadth_return_3m_positive") is not None
            and float(features["basket_return_3m"]) >= 0.25
            and float(features["cs300_ma_6m_distance"]) >= 0.15
            and float(features["cs300_vol_1m_percentile_3y"]) >= 0.90
            and float(features["breadth_return_3m_positive"]) >= 0.80
        )
        one_month_surge = (
            features.get("basket_return_1m") is not None
            and features.get("basket_ma_3m_distance") is not None
            and features.get("cs300_vol_1m_percentile_3y") is not None
            and features.get("breadth_return_1m_positive") is not None
            and float(features["basket_return_1m"]) >= 0.20
            and float(features["basket_ma_3m_distance"]) >= 0.15
            and float(features["cs300_vol_1m_percentile_3y"]) >= 0.95
            and float(features["breadth_return_1m_positive"]) >= 0.80
        )
        high_level_distribution = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_return_3m") is not None
            and features.get("cs300_drawdown_3m") is not None
            and features.get("cs300_vol_3m") is not None
            and float(features["cs300_return_6m"]) >= 0.30
            and float(features["cs300_return_3m"]) <= 0.0
            and float(features["cs300_drawdown_3m"]) <= -0.08
            and float(features["cs300_vol_3m"]) >= 0.25
        )
        long_cycle_overheat = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_ma_10m_distance") is not None
            and features.get("cs300_vol_3m") is not None
            and features.get("breadth_return_6m_positive") is not None
            and float(features["cs300_return_6m"]) >= 0.60
            and float(features["cs300_ma_10m_distance"]) >= 0.40
            and float(features["cs300_vol_3m"]) >= 0.30
            and float(features["breadth_return_6m_positive"]) >= 0.80
        )
        low_vol_meltup_exhaustion = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_ma_10m_distance") is not None
            and features.get("cs300_vol_1m_percentile_3y") is not None
            and features.get("breadth_return_6m_positive") is not None
            and float(features["cs300_return_6m"]) >= 0.40
            and float(features["cs300_ma_10m_distance"]) >= 0.20
            and float(features["cs300_vol_1m_percentile_3y"]) <= 0.20
            and float(features["breadth_return_6m_positive"]) >= 0.80
        )
        low_vol_breadth_rollover = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_return_3m") is not None
            and features.get("cs300_vol_1m_percentile_3y") is not None
            and features.get("breadth_return_3m_positive") is not None
            and features.get("breadth_return_6m_positive") is not None
            and float(features["cs300_return_6m"]) > 0.0
            and float(features["cs300_return_3m"]) < 0.0
            and float(features["cs300_vol_1m_percentile_3y"]) <= 0.15
            and float(features["breadth_return_3m_positive"]) <= 0.40
            and float(features["breadth_return_6m_positive"]) >= 0.80
        )
        valuation_concentration_overheat = (
            features.get("market_pe_ttm_percentile_3y") is not None
            and features.get("market_pb_percentile_3y") is not None
            and features.get("basket_return_3m") is not None
            and features.get("basket_excess_return_3m") is not None
            and features.get("breadth_return_3m_positive") is not None
            and float(features["market_pe_ttm_percentile_3y"]) >= 0.95
            and float(features["market_pb_percentile_3y"]) >= 0.95
            and float(features["basket_return_3m"]) >= 0.25
            and float(features["basket_excess_return_3m"]) >= 0.15
            and float(features["breadth_return_3m_positive"]) >= 0.80
        )
        bear_rebound_exhaustion = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_return_3m") is not None
            and features.get("cs300_ma_10m_distance") is not None
            and features.get("breadth_return_3m_positive") is not None
            and float(features["cs300_return_6m"]) < 0.0
            and float(features["cs300_return_3m"]) >= 0.12
            and float(features["cs300_ma_10m_distance"]) < 0.0
            and float(features["breadth_return_3m_positive"]) >= 0.80
        )
        features["cs300_overheat_flag"] = float(cs300_overheat)
        features["basket_overheat_flag"] = float(basket_overheat)
        features["rebound_overheat_flag"] = float(rebound_overheat)
        features["short_cycle_overheat_flag"] = float(short_cycle_overheat)
        features["one_month_surge_flag"] = float(one_month_surge)
        features["high_level_distribution_flag"] = float(high_level_distribution)
        features["long_cycle_overheat_flag"] = float(long_cycle_overheat)
        features["low_vol_meltup_exhaustion_flag"] = float(low_vol_meltup_exhaustion)
        features["low_vol_breadth_rollover_flag"] = float(low_vol_breadth_rollover)
        features["valuation_concentration_overheat_flag"] = float(
            valuation_concentration_overheat
        )
        features["bear_rebound_exhaustion_flag"] = float(bear_rebound_exhaustion)
        features["market_overheat_flag"] = float(
            cs300_overheat or basket_overheat or rebound_overheat
        )
        crisis_continuation = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_drawdown_3m") is not None
            and float(features["cs300_return_6m"]) < 0.0
            and float(features["cs300_drawdown_3m"]) <= -0.15
        )
        shock_continuation = (
            features.get("cs300_drawdown_1m") is not None
            and features.get("cs300_vol_1m_percentile_3y") is not None
            and features.get("cs300_ma_3m_distance") is not None
            and float(features["cs300_drawdown_1m"]) <= -0.12
            and float(features["cs300_vol_1m_percentile_3y"]) >= 0.90
            and float(features["cs300_ma_3m_distance"]) < 0.0
        )
        features["shock_continuation_flag"] = float(shock_continuation)
        features["crisis_continuation_flag"] = float(
            crisis_continuation or shock_continuation
        )

        vix_values = self.external_series(cur, "^VIX").trailing(
            snapshot,
            780,
            include_snapshot=False,
        )
        features.update(
            {
                "external_vix_level": vix_values[-1] if vix_values else None,
                "external_vix_percentile_1y": percentile_rank(vix_values, 252),
                "external_vix_percentile_3y": percentile_rank(vix_values, 756),
                "external_vix_change_1m": (
                    vix_values[-1] - vix_values[-22] if len(vix_values) >= 22 else None
                ),
            }
        )

        dxy_values = self.external_series(cur, "DX-Y.NYB").trailing(
            snapshot,
            140,
            include_snapshot=False,
        )
        features.update(self._series_features("external_dxy", dxy_values))

        anfci_values = self.external_series(cur, "FRED:ANFCI").trailing(
            snapshot,
            160,
            include_snapshot=False,
        )
        features.update(
            {
                "external_anfci_level": anfci_values[-1] if anfci_values else None,
                "external_anfci_change_3m": (
                    anfci_values[-1] - anfci_values[-14]
                    if len(anfci_values) >= 14
                    else None
                ),
                "external_anfci_percentile_3y": percentile_rank(anfci_values, 156),
            }
        )
        nfci_values = self.external_series(cur, "FRED:NFCI").trailing(
            snapshot,
            160,
            include_snapshot=False,
        )
        features.update(
            {
                "external_nfci_level": nfci_values[-1] if nfci_values else None,
                "external_nfci_change_3m": (
                    nfci_values[-1] - nfci_values[-14]
                    if len(nfci_values) >= 14
                    else None
                ),
                "external_nfci_percentile_3y": percentile_rank(nfci_values, 156),
            }
        )
        fed_funds_values = self.external_series(cur, "FRED:DFF").trailing(
            snapshot,
            780,
            include_snapshot=False,
        )
        features.update(
            {
                "external_fed_funds_level": (
                    fed_funds_values[-1] if fed_funds_values else None
                ),
                "external_fed_funds_change_1m": (
                    fed_funds_values[-1] - fed_funds_values[-22]
                    if len(fed_funds_values) >= 22
                    else None
                ),
                "external_fed_funds_change_3m": (
                    fed_funds_values[-1] - fed_funds_values[-64]
                    if len(fed_funds_values) >= 64
                    else None
                ),
                "external_fed_funds_percentile_3y": percentile_rank(
                    fed_funds_values, 756
                ),
            }
        )
        broad_dollar_values = self.external_series(cur, "FRED:DTWEXBGS").trailing(
            snapshot,
            260,
            include_snapshot=False,
        )
        features.update(
            self._series_features("external_broad_dollar", broad_dollar_values)
        )
        baa_spread_values = self.external_series(cur, "FRED:BAA10Y").trailing(
            snapshot,
            780,
            include_snapshot=False,
        )
        aaa_spread_values = self.external_series(cur, "FRED:AAA10Y").trailing(
            snapshot,
            780,
            include_snapshot=False,
        )
        for prefix, values in (
            ("external_baa10y", baa_spread_values),
            ("external_aaa10y", aaa_spread_values),
        ):
            features.update(
                {
                    f"{prefix}_level": values[-1] if values else None,
                    f"{prefix}_change_1m": (
                        values[-1] - values[-22] if len(values) >= 22 else None
                    ),
                    f"{prefix}_change_3m": (
                        values[-1] - values[-64] if len(values) >= 64 else None
                    ),
                    f"{prefix}_percentile_3y": percentile_rank(values, 756),
                }
            )
        quality_spread_values = [
            baa - aaa
            for baa, aaa in zip(baa_spread_values, aaa_spread_values)
        ]
        features.update(
            {
                "external_baa_aaa_quality_spread": (
                    quality_spread_values[-1] if quality_spread_values else None
                ),
                "external_baa_aaa_quality_spread_change_3m": (
                    quality_spread_values[-1] - quality_spread_values[-64]
                    if len(quality_spread_values) >= 64
                    else None
                ),
                "external_baa_aaa_quality_spread_percentile_3y": percentile_rank(
                    quality_spread_values, 756
                ),
            }
        )
        stlfsi_values = self.external_series(cur, "FRED:STLFSI4").trailing(
            snapshot,
            160,
            include_snapshot=False,
        )
        features.update(
            {
                "external_stlfsi_level": stlfsi_values[-1] if stlfsi_values else None,
                "external_stlfsi_change_3m": (
                    stlfsi_values[-1] - stlfsi_values[-14]
                    if len(stlfsi_values) >= 14
                    else None
                ),
                "external_stlfsi_percentile_3y": percentile_rank(stlfsi_values, 156),
            }
        )
        early_history_crisis_repricing = (
            features.get("cs300_drawdown_1m") is not None
            and features.get("cs300_drawdown_3m") is not None
            and features.get("external_vix_percentile_3y") is not None
            and features.get("external_anfci_percentile_3y") is not None
            and float(features["cs300_drawdown_1m"]) <= -0.10
            and float(features["cs300_drawdown_3m"]) <= -0.15
            and float(features["external_vix_percentile_3y"]) >= 0.85
            and float(features["external_anfci_percentile_3y"]) >= 0.85
        )
        features["early_history_crisis_repricing_flag"] = float(
            early_history_crisis_repricing
        )
        if early_history_crisis_repricing:
            features["crisis_continuation_flag"] = 1.0

        dgs10_series = self.external_series(cur, "FRED:DGS10")
        dgs2_series = self.external_series(cur, "FRED:DGS2")
        dgs10 = dgs10_series.value_at(snapshot, include_snapshot=False)
        dgs2 = dgs2_series.value_at(snapshot, include_snapshot=False)
        features["external_us_curve_10y2y"] = (
            dgs10 - dgs2 if dgs10 is not None and dgs2 is not None else None
        )
        if self._us_curve is None:
            curve_dates = sorted(set(dgs10_series.dates) & set(dgs2_series.dates))
            dgs10_by_date = dict(zip(dgs10_series.dates, dgs10_series.values))
            dgs2_by_date = dict(zip(dgs2_series.dates, dgs2_series.values))
            self._us_curve = DatedSeries.from_rows(
                (day, dgs10_by_date[day] - dgs2_by_date[day]) for day in curve_dates
            )
        curve = self._us_curve.trailing(snapshot, 756, include_snapshot=False)
        features["external_us_curve_percentile_3y"] = percentile_rank(curve, 756)

        shibor_on = self.shibor_on_series(cur).trailing(
            snapshot,
            780,
            include_snapshot=True,
        )
        shibor_on_level = shibor_on[-1] if shibor_on else None
        shibor_on_change_1m = (
            shibor_on[-1] - shibor_on[-22] if len(shibor_on) >= 22 else None
        )
        shibor_on_percentile_3y = percentile_rank(shibor_on, 756)
        features["domestic_shibor_on_level"] = shibor_on_level
        features["domestic_shibor_on_change_1m"] = shibor_on_change_1m
        features["domestic_shibor_on_percentile_3y"] = shibor_on_percentile_3y
        features["domestic_liquidity_stress_flag"] = float(
            shibor_on_change_1m is not None
            and shibor_on_percentile_3y is not None
            and shibor_on_change_1m >= 1.0
            and shibor_on_percentile_3y >= 0.90
        )
        features.update(self.domestic_yield_curve_features(cur, snapshot))
        features.update(self.money_supply_features(cur, snapshot))
        features.update(self.domestic_macro_features(cur, snapshot))
        features.update(self.pboc_report_features(cur, snapshot))
        features.update(self.daily_margin_features(cur, snapshot))
        features.update(self.fund_issuance_features(cur, snapshot))
        features.update(self.etf_share_market_features(cur, snapshot))
        features.update(self.option_sentiment_features(cur, snapshot))
        rally_distribution = (
            features.get("basket_return_3m") is not None
            and features.get("basket_drawdown_1m") is not None
            and features.get("breadth_return_3m_positive") is not None
            and features.get("domestic_margin_balance_return_3m") is not None
            and float(features["basket_return_3m"]) >= 0.20
            and float(features["basket_drawdown_1m"]) <= -0.05
            and float(features["breadth_return_3m_positive"]) >= 0.80
            and float(features["domestic_margin_balance_return_3m"]) >= 0.15
        )
        financed_surge_reversal = (
            features.get("basket_return_1m") is not None
            and features.get("basket_drawdown_1m") is not None
            and features.get("domestic_margin_balance_return_1m") is not None
            and float(features["basket_return_1m"]) >= 0.12
            and float(features["basket_drawdown_1m"]) <= -0.02
            and float(features["domestic_margin_balance_return_1m"]) >= 0.15
        )
        option_panic_after_rally = (
            features.get("domestic_option_put_call_volume_change_1m") is not None
            and features.get("domestic_option_put_call_volume_percentile_3y") is not None
            and features.get("external_vix_change_1m") is not None
            and features.get("basket_return_6m") is not None
            and float(features["domestic_option_put_call_volume_change_1m"]) >= 0.60
            and float(features["domestic_option_put_call_volume_percentile_3y"]) >= 0.95
            and float(features["external_vix_change_1m"]) >= 4.0
            and float(features["basket_return_6m"]) >= 0.20
        )
        turnover_overheat = (
            features.get("basket_return_3m") is not None
            and features.get("market_turnover_percentile_3y") is not None
            and features.get("market_turnover_change_1m") is not None
            and float(features["basket_return_3m"]) >= 0.20
            and float(features["market_turnover_percentile_3y"]) >= 0.90
            and float(features["market_turnover_change_1m"]) >= 0.15
        )
        daily_margin_rally = (
            features.get("daily_margin_balance_return_1m") is not None
            and features.get("basket_return_3m") is not None
            and float(features["daily_margin_balance_return_1m"]) >= 0.12
            and float(features["basket_return_3m"]) >= 0.20
        )
        low_vol_flat = (
            features.get("cs300_vol_1m_percentile_3y") is not None
            and features.get("basket_return_3m") is not None
            and features.get("basket_drawdown_1m") is not None
            and float(features["cs300_vol_1m_percentile_3y"]) <= 0.05
            and 0.0 <= float(features["basket_return_3m"]) <= 0.05
            and float(features["basket_drawdown_1m"]) <= -0.01
        )
        strong_rally_breadth_reversal = (
            features.get("basket_return_3m") is not None
            and features.get("breadth_return_1m_positive") is not None
            and float(features["basket_return_3m"]) >= 0.25
            and float(features["breadth_return_1m_positive"]) <= 0.20
        )
        leadership_collapse_tightening = (
            features.get("basket_return_6m") is not None
            and features.get("basket_return_3m") is not None
            and features.get("breadth_return_3m_positive") is not None
            and features.get("basket_drawdown_3m") is not None
            and features.get("external_anfci_change_3m") is not None
            and features.get("market_turnover_percentile_3y") is not None
            and float(features["basket_return_6m"]) >= 0.15
            and float(features["basket_return_3m"]) <= 0.0
            and float(features["breadth_return_3m_positive"]) <= 0.30
            and float(features["basket_drawdown_3m"]) <= -0.08
            and float(features["external_anfci_change_3m"]) >= 0.0
            and float(features["market_turnover_percentile_3y"]) <= 0.90
        )
        leverage_macro_divergence = (
            features.get("daily_margin_balance_return_1m") is not None
            and features.get("basket_return_3m") is not None
            and features.get("basket_drawdown_1m") is not None
            and features.get("domestic_pmi_mfg_change_3m") is not None
            and float(features["daily_margin_balance_return_1m"]) >= 0.12
            and float(features["basket_return_3m"]) <= 0.10
            and float(features["basket_drawdown_1m"]) <= -0.03
            and float(features["domestic_pmi_mfg_change_3m"]) <= 0.0
        )
        theme_macro_contraction_divergence = (
            features.get("basket_return_6m") is not None
            and features.get("basket_excess_return_6m") is not None
            and features.get("cs300_return_6m") is not None
            and features.get("domestic_sf_rolling_12m_growth") is not None
            and features.get("domestic_pmi_mfg_change_3m") is not None
            and float(features["basket_return_6m"]) >= 0.35
            and float(features["basket_excess_return_6m"]) >= 0.35
            and float(features["cs300_return_6m"]) <= 0.0
            and float(features["domestic_sf_rolling_12m_growth"]) < 0.0
            and float(features["domestic_pmi_mfg_change_3m"]) < 0.0
        )
        stagflation_credit_contraction = (
            features.get("cs300_return_6m") is not None
            and features.get("market_pb_percentile_3y") is not None
            and features.get("domestic_sf_rolling_12m_growth") is not None
            and features.get("domestic_m1_m2_scissors") is not None
            and features.get("domestic_ppi_cpi_scissors") is not None
            and features.get("external_anfci_change_3m") is not None
            and float(features["cs300_return_6m"]) < 0.0
            and float(features["market_pb_percentile_3y"]) >= 0.85
            and float(features["domestic_sf_rolling_12m_growth"]) <= -0.10
            and float(features["domestic_m1_m2_scissors"]) <= -3.0
            and float(features["domestic_ppi_cpi_scissors"]) >= 8.0
            and float(features["external_anfci_change_3m"]) > 0.0
        )
        fund_distribution_tight = (
            features.get("fund_active_issuance_percentile_3y") is not None
            and features.get("basket_return_3m") is not None
            and features.get("basket_drawdown_1m") is not None
            and float(features["fund_active_issuance_percentile_3y"]) >= 0.95
            and float(features["basket_return_3m"]) >= 0.20
            and float(features["basket_drawdown_1m"]) <= -0.05
        )
        fund_saturation_contraction = (
            features.get("fund_active_issuance_percentile_3y") is not None
            and features.get("basket_return_3m") is not None
            and features.get("market_turnover_change_1m") is not None
            and float(features["fund_active_issuance_percentile_3y"]) >= 0.95
            and float(features["basket_return_3m"]) >= 0.25
            and float(features["market_turnover_change_1m"]) <= -0.20
        )
        crowded_fund_issuance_rally = (
            features.get("fund_total_issuance_percentile_3y") is not None
            and features.get("cs300_return_3m") is not None
            and features.get("breadth_return_3m_positive") is not None
            and float(features["fund_total_issuance_percentile_3y"]) >= 0.98
            and float(features["cs300_return_3m"]) >= 0.05
            and float(features["breadth_return_3m_positive"]) >= 0.80
        )
        theme_divergence_3m = (
            features.get("basket_excess_return_3m") is not None
            and features.get("market_turnover_percentile_3y") is not None
            and features.get("external_dxy_return_1m") is not None
            and features.get("external_vix_change_1m") is not None
            and float(features["basket_excess_return_3m"]) >= 0.20
            and float(features["market_turnover_percentile_3y"]) >= 0.90
            and (
                float(features["external_dxy_return_1m"]) >= 0.0
                or float(features["external_vix_change_1m"]) >= 3.0
            )
        )
        theme_divergence_1m_tightening = (
            features.get("basket_excess_return_1m") is not None
            and features.get("cs300_return_1m") is not None
            and features.get("breadth_return_1m_positive") is not None
            and features.get("external_anfci_change_3m") is not None
            and float(features["basket_excess_return_1m"]) >= 0.04
            and float(features["cs300_return_1m"]) <= 0.01
            and float(features["breadth_return_1m_positive"]) >= 0.80
            and float(features["external_anfci_change_3m"]) >= 0.0
        )
        theme_divergence_1m_crowded = (
            theme_divergence_1m_tightening
            and features.get("market_turnover_percentile_3y") is not None
            and float(features["market_turnover_percentile_3y"]) >= 0.70
        )
        credit_contraction_tightening = (
            features.get("domestic_sf_rolling_3m_yoy") is not None
            and features.get("domestic_m1_m2_scissors_change_3m") is not None
            and features.get("cs300_return_6m") is not None
            and features.get("external_anfci_change_3m") is not None
            and float(features["domestic_sf_rolling_3m_yoy"]) <= -0.20
            and float(features["domestic_m1_m2_scissors_change_3m"]) <= -1.0
            and float(features["cs300_return_6m"]) <= -0.10
            and float(features["external_anfci_change_3m"]) >= 0.25
        )
        macro_weak_rebound = (
            features.get("domestic_pmi_mfg_change_3m") is not None
            and features.get("domestic_sf_rolling_3m_yoy") is not None
            and features.get("basket_return_1m") is not None
            and features.get("cs300_return_6m") is not None
            and features.get("cs300_return_1m") is not None
            and float(features["domestic_pmi_mfg_change_3m"]) <= -2.0
            and float(features["domestic_sf_rolling_3m_yoy"]) <= -0.15
            and float(features["basket_return_1m"]) >= 0.0
            and float(features["cs300_return_6m"]) <= 0.0
            and float(features["cs300_return_1m"]) <= 0.06
        )
        weak_credit_leveraged_rebound = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_return_1m") is not None
            and features.get("daily_margin_balance_return_1m") is not None
            and features.get("domestic_sf_rolling_3m_yoy") is not None
            and float(features["cs300_return_6m"]) <= 0.0
            and float(features["cs300_return_1m"]) >= 0.05
            and float(features["daily_margin_balance_return_1m"]) >= 0.10
            and float(features["domestic_sf_rolling_3m_yoy"]) <= -0.10
        )
        fund_moderate_distribution = (
            features.get("fund_active_issuance_percentile_3y") is not None
            and features.get("basket_return_3m") is not None
            and features.get("basket_drawdown_1m") is not None
            and features.get("breadth_return_1m_positive") is not None
            and float(features["fund_active_issuance_percentile_3y"]) >= 0.95
            and float(features["basket_return_3m"]) >= 0.0
            and float(features["basket_drawdown_1m"]) <= -0.05
            and float(features["breadth_return_1m_positive"]) <= 0.30
        )
        features["rally_distribution_flag"] = float(rally_distribution)
        features["financed_surge_reversal_flag"] = float(financed_surge_reversal)
        features["option_panic_after_rally_flag"] = float(option_panic_after_rally)
        features["turnover_overheat_flag"] = float(turnover_overheat)
        features["daily_margin_rally_flag"] = float(daily_margin_rally)
        features["low_vol_flat_flag"] = float(low_vol_flat)
        features["strong_rally_breadth_reversal_flag"] = float(
            strong_rally_breadth_reversal
        )
        features["leadership_collapse_tightening_flag"] = float(
            leadership_collapse_tightening
        )
        features["leverage_macro_divergence_flag"] = float(
            leverage_macro_divergence
        )
        features["theme_macro_contraction_divergence_flag"] = float(
            theme_macro_contraction_divergence
        )
        features["stagflation_credit_contraction_flag"] = float(
            stagflation_credit_contraction
        )
        features["fund_distribution_tight_flag"] = float(fund_distribution_tight)
        features["fund_saturation_contraction_flag"] = float(
            fund_saturation_contraction
        )
        features["crowded_fund_issuance_rally_flag"] = float(
            crowded_fund_issuance_rally
        )
        features["theme_divergence_3m_flag"] = float(theme_divergence_3m)
        features["theme_divergence_1m_tightening_flag"] = float(
            theme_divergence_1m_tightening
        )
        features["theme_divergence_1m_crowded_flag"] = float(
            theme_divergence_1m_crowded
        )
        features["credit_contraction_tightening_flag"] = float(
            credit_contraction_tightening
        )
        features["macro_weak_rebound_flag"] = float(macro_weak_rebound)
        features["weak_credit_leveraged_rebound_flag"] = float(
            weak_credit_leveraged_rebound
        )
        features["fund_moderate_distribution_flag"] = float(
            fund_moderate_distribution
        )
        leveraged_rally_exhaustion = (
            features.get("basket_return_3m") is not None
            and features.get("basket_drawdown_1m") is not None
            and features.get("domestic_margin_balance_return_3m") is not None
            and float(features["basket_return_3m"]) >= 0.20
            and float(features["domestic_margin_balance_return_3m"]) >= 0.15
            and (
                float(features["basket_drawdown_1m"]) <= -0.02
                or float(features["basket_return_3m"]) >= 0.30
            )
        )
        tightening_rebound_exhaustion = (
            features.get("basket_return_1m") is not None
            and features.get("basket_return_6m") is not None
            and features.get("external_dxy_return_1m") is not None
            and features.get("external_anfci_change_3m") is not None
            and float(features["basket_return_1m"]) >= 0.10
            and float(features["basket_return_6m"]) < 0.0
            and (
                float(features["external_dxy_return_1m"]) >= 0.02
                or float(features["external_anfci_change_3m"]) >= 0.20
            )
        )
        low_vol_mature_trend = (
            features.get("cs300_return_6m") is not None
            and features.get("cs300_vol_3m") is not None
            and features.get("breadth_return_1m_positive") is not None
            and float(features["cs300_return_6m"]) >= 0.10
            and float(features["cs300_vol_3m"]) <= 0.15
            and float(features["breadth_return_1m_positive"]) <= 0.90
        )
        features["leveraged_rally_exhaustion_flag"] = float(
            leveraged_rally_exhaustion
        )
        features["tightening_rebound_exhaustion_flag"] = float(
            tightening_rebound_exhaustion
        )
        features["low_vol_mature_trend_flag"] = float(low_vol_mature_trend)
        mature_dollar_tightening = (
            low_vol_mature_trend
            and features.get("external_dxy_return_1m") is not None
            and float(features["external_dxy_return_1m"]) >= 0.015
        )
        mature_narrow_reversal = (
            low_vol_mature_trend
            and features.get("breadth_return_3m_positive") is not None
            and features.get("basket_drawdown_1m") is not None
            and float(features["breadth_return_3m_positive"]) <= 0.60
            and float(features["basket_drawdown_1m"]) <= -0.03
        )
        features["mature_dollar_tightening_flag"] = float(
            mature_dollar_tightening
        )
        features["mature_narrow_reversal_flag"] = float(mature_narrow_reversal)
        features["refined_mature_reversal_flag"] = float(
            mature_dollar_tightening or mature_narrow_reversal
        )
        features["medium_cycle_exhaustion_flag"] = float(
            leveraged_rally_exhaustion
            or tightening_rebound_exhaustion
            or low_vol_mature_trend
        )
        self._snapshot_cache[cache_key] = dict(features)
        return features


PHASE_FEATURE_STORE = PhaseFeatureStore()
