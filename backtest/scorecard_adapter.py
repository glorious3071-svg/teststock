"""scorecard_adapter.py — MySQL → ScorecardInputs 适配器（v3.4.14 + v12/v13 字段）

覆盖估值 + 流动性 + 基本面 + 部分外部（us_monthly_pct + global_recession）字段，
以及 v12-R1（ROE 趋势）、v12-M4（美 10Y 变化）、v13-B1（企业景气）等已采纳规则所需输入。
情绪 / 政策 / 外部其余字段保留 None（评分时跳过），保证 baseline 与 candidate
在同一份"非候选维度"基线上比较，公平地隔离候选规则的边际效果。

红线：所有取数 as_of_date ≤ snapshot_date（year-1-12-31），严防上帝视角。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pymysql
from dotenv import load_dotenv

from backtest.scorecard import ScorecardInputs

ROOT = Path(__file__).resolve().parents[1]

# PMI 阈值（与 scorecard.py 内一致）
PMI_BELOW_THRESHOLD = 52.0
PMI_EXPANSION_LINE = 50.0

# OECD CLI 衰退信号阈值（评分卡 spec §六 行 178）
OECD_CLI_TREND_LINE = 100.0           # 长期趋势线
OECD_CLI_RECESSION_VOTES = 2          # 5 经济体投票 ≥ 该值 → global_recession=True
OECD_CLI_TREND_WINDOW = 3             # 判断持续下行所需的连续月数（含当月）
OECD_RECESSION_REF_AREAS = (
    "USA", "CHN", "JPN", "G4E", "G7",  # 评分卡 spec §六 行 178 指定的 5 经济体
)

# global_stimulus 计票阈值（评分卡 spec §六 行 180）
GLOBAL_STIMULUS_LOOKBACK_MONTHS = 12   # 过去 N 个月内观察 cut 事件
GLOBAL_STIMULUS_MIN_VOTES = 3          # 5 大央行 cut 投票 ≥ 该值 → global_stimulus=True
GLOBAL_STIMULUS_FOREIGN_CBS = ("FED", "ECB", "BOE", "BOJ")  # PBoC 单独从 cn_deposit_rate / cn_rrr_changes 取


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host":     os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.getenv("MYSQL_PORT", "3306")),
        "user":     os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset":  "utf8mb4",
    }


# ──────────────────────────────────────────────────────────────────
# 单项查询函数（私有，命令查询分离）
# ──────────────────────────────────────────────────────────────────
def _latest_pe_pb(cur, ts_code: str, snapshot_date: date) -> tuple[float | None, float | None]:
    cur.execute(
        """
        SELECT pe_ttm, pb FROM index_dailybasic
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (ts_code, snapshot_date),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    pe = float(row[0]) if row[0] is not None else None
    pb = float(row[1]) if row[1] is not None else None
    return pe, pb


def _shibor_3m_cum_bp(cur, snapshot_date: date) -> float | None:
    """末点 − 起点 SHIBOR_3M，× 100 转 bp"""
    one_year_ago = snapshot_date - timedelta(days=365)
    cur.execute(
        "SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (snapshot_date,),
    )
    cur_row = cur.fetchone()
    cur.execute(
        "SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
        (one_year_ago,),
    )
    prior_row = cur.fetchone()
    if not (cur_row and prior_row and cur_row[0] is not None and prior_row[0] is not None):
        return None
    return (float(cur_row[0]) - float(prior_row[0])) * 100.0


def _rrr_cum_pp_12m(cur, snapshot_date: date) -> float:
    one_year_ago = snapshot_date - timedelta(days=365)
    cur.execute(
        """
        SELECT COALESCE(SUM(rrr_change_pp), 0)
        FROM cn_rrr_changes
        WHERE effective_date > %s AND effective_date <= %s
          AND inst_type IN ('large', 'all')
        """,
        (one_year_ago, snapshot_date),
    )
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _deposit_1y_rate(cur, snapshot_date: date) -> float | None:
    cur.execute(
        """
        SELECT rate_after_pct FROM cn_deposit_rate
        WHERE effective_date <= %s
        ORDER BY effective_date DESC LIMIT 1
        """,
        (snapshot_date,),
    )
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _pmi_series_until(cur, snapshot_month: str, n: int) -> list[dict]:
    """取 snapshot_month 及之前共 n 个月的 PMI（升序）"""
    cur.execute(
        """
        SELECT month, pmi_mfg, pmi_production, pmi_new_order
        FROM cn_pmi_monthly
        WHERE month <= %s
        ORDER BY month DESC
        LIMIT %s
        """,
        (snapshot_month, n),
    )
    rows = cur.fetchall()
    series = []
    for r in reversed(rows):
        series.append({
            "month": r[0],
            "pmi_mfg": float(r[1]) if r[1] is not None else None,
            "pmi_production": float(r[2]) if r[2] is not None else None,
            "pmi_new_order": float(r[3]) if r[3] is not None else None,
        })
    return series


def _pmi_below_52_consecutive(series: list[dict]) -> int:
    """倒数连续 pmi_mfg < 52 的月数"""
    cnt = 0
    for row in reversed(series):
        v = row["pmi_mfg"]
        if v is None or v >= PMI_BELOW_THRESHOLD:
            break
        cnt += 1
    return cnt


def _pmi_resume_expansion(series: list[dict]) -> bool:
    """最近一个月 PMI ≥ 50 且前一个月 < 50"""
    if len(series) < 2:
        return False
    last = series[-1]["pmi_mfg"]
    prev = series[-2]["pmi_mfg"]
    if last is None or prev is None:
        return False
    return prev < PMI_EXPANSION_LINE <= last


def _pmi_mfg_3m_avg(series: list[dict]) -> float | None:
    vals = [r["pmi_mfg"] for r in series[-3:] if r["pmi_mfg"] is not None]
    if len(vals) < 3:
        return None
    return sum(vals) / 3.0


def _pmi_prod_minus_order(series: list[dict]) -> float | None:
    if not series:
        return None
    last = series[-1]
    p, o = last["pmi_production"], last["pmi_new_order"]
    if p is None or o is None:
        return None
    return p - o


def _us_monthly_pct(cur, snapshot_date: date, ts_code: str = "SPX.US") -> float | None:
    """snapshot 当月末 close / 上月末 close − 1，× 100 转 %。

    严格按月：取 snapshot 当月最后一个交易日 close 与 snapshot 上月最后一个交易日 close。
    数据缺失返回 None。
    """
    cur.execute(
        """
        SELECT close FROM us_index_daily
        WHERE ts_code = %s AND YEAR(trade_date) = %s AND MONTH(trade_date) = %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (ts_code, snapshot_date.year, snapshot_date.month),
    )
    cur_row = cur.fetchone()
    if not cur_row or cur_row[0] is None:
        return None

    prev_year = snapshot_date.year if snapshot_date.month > 1 else snapshot_date.year - 1
    prev_month = snapshot_date.month - 1 if snapshot_date.month > 1 else 12
    cur.execute(
        """
        SELECT close FROM us_index_daily
        WHERE ts_code = %s AND YEAR(trade_date) = %s AND MONTH(trade_date) = %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (ts_code, prev_year, prev_month),
    )
    prev_row = cur.fetchone()
    if not prev_row or prev_row[0] is None:
        return None

    cur_close = float(cur_row[0])
    prev_close = float(prev_row[0])
    if prev_close == 0:
        return None
    return (cur_close / prev_close - 1.0) * 100.0


def _is_economy_in_recession(cli_values_desc: list[float]) -> bool:
    """单经济体衰退判定：当月 cli < 100 且连续 N 个月严格下行。

    Args:
        cli_values_desc: 最近 OECD_CLI_TREND_WINDOW 个月 cli_value，按时间降序。
                         如 [m0, m_minus_1, m_minus_2]，m0 是最新月。
    """
    if len(cli_values_desc) < OECD_CLI_TREND_WINDOW:
        return False
    latest = cli_values_desc[0]
    if latest >= OECD_CLI_TREND_LINE:
        return False
    # 严格下行：m0 < m_minus_1 < m_minus_2 < ... （从最近往前严格递增）
    for prior, later in zip(cli_values_desc[1:], cli_values_desc[:-1]):
        if not later < prior:
            return False
    return True


def _global_recession(cur, snapshot_date: date) -> bool:
    """评分卡 spec §六 行 178：5 经济体 (USA/CHN/JPN/G4E/G7) 当月 recession_signal=1 投票 ≥ 2。

    每个经济体的 recession_signal 由 _is_economy_in_recession 计算（CLI<100 + 持续下行）。

    Args:
        cur: pymysql cursor
        snapshot_date: 评分基准日（取 ≤ snapshot 的最新 N 个 CLI 月度值）

    Returns:
        True 当且仅当 ≥ OECD_CLI_RECESSION_VOTES 个经济体满足衰退信号。
        数据缺失的经济体不计票（保守，避免假阳性）。
    """
    votes = 0
    for ref_area in OECD_RECESSION_REF_AREAS:
        cur.execute(
            """
            SELECT cli_value FROM oecd_cli_monthly
            WHERE ref_area = %s AND period <= %s
            ORDER BY period DESC LIMIT %s
            """,
            (ref_area, snapshot_date, OECD_CLI_TREND_WINDOW),
        )
        rows = cur.fetchall()
        cli_desc = [float(r[0]) for r in rows if r[0] is not None]
        if _is_economy_in_recession(cli_desc):
            votes += 1
    return votes >= OECD_CLI_RECESSION_VOTES


def _global_stimulus(cur, snapshot_date: date) -> bool:
    """评分卡 spec §六 行 180：5 大央行 (Fed/ECB/BoE/BoJ/PBoC) 过去 12 个月 cut 投票 ≥3 家。

    口径：每家央行只要在窗口内出现一次降息事件即记 1 票（不重复计数）。
        - 外资四家：global_cb_rate_events.direction='cut'
        - PBoC：cn_deposit_rate.direction='cut' ∪ cn_rrr_changes.rrr_change_pp<0
          （PBoC 2015-10 后存款基准冻结，需纳入数量型工具 RRR 才能反映宽松行为；
          与 sql/global_cb_rate_events_schema.sql 行 14 注释口径一致）

    Args:
        cur: pymysql cursor
        snapshot_date: 评分基准日（窗口 = (snapshot - 12M, snapshot]，含右端）

    Returns:
        True 当且仅当 ≥ GLOBAL_STIMULUS_MIN_VOTES 家央行在窗口内有降息动作。
    """
    votes = 0
    for cb_code in GLOBAL_STIMULUS_FOREIGN_CBS:
        cur.execute(
            """
            SELECT 1 FROM global_cb_rate_events
            WHERE cb_code = %s AND direction = 'cut'
              AND effective_date > DATE_SUB(%s, INTERVAL %s MONTH)
              AND effective_date <= %s
            LIMIT 1
            """,
            (cb_code, snapshot_date, GLOBAL_STIMULUS_LOOKBACK_MONTHS, snapshot_date),
        )
        if cur.fetchone():
            votes += 1

    # PBoC：存款基准 cut ∪ RRR 下调，任意一家有事件即记 1 票
    cur.execute(
        """
        SELECT 1 FROM cn_deposit_rate
        WHERE direction = 'cut'
          AND effective_date > DATE_SUB(%s, INTERVAL %s MONTH)
          AND effective_date <= %s
        LIMIT 1
        """,
        (snapshot_date, GLOBAL_STIMULUS_LOOKBACK_MONTHS, snapshot_date),
    )
    pboc_hit = cur.fetchone() is not None
    if not pboc_hit:
        cur.execute(
            """
            SELECT 1 FROM cn_rrr_changes
            WHERE rrr_change_pp < 0
              AND effective_date > DATE_SUB(%s, INTERVAL %s MONTH)
              AND effective_date <= %s
              AND inst_type IN ('large', 'all')
            LIMIT 1
            """,
            (snapshot_date, GLOBAL_STIMULUS_LOOKBACK_MONTHS, snapshot_date),
        )
        pboc_hit = cur.fetchone() is not None
    if pboc_hit:
        votes += 1

    return votes >= GLOBAL_STIMULUS_MIN_VOTES


def _normalize_pboc_tone(raw: str | None) -> str | None:
    """三态归一化：'从紧'/'紧缩' → 'tight'；'适度宽松'/'宽松' → 'loose'；'稳健'/'中性' → 'neutral'。"""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "紧" in s:
        return "tight"
    if "适度宽松" in s or "宽松" in s:
        return "loose"
    if "稳健" in s or "中性" in s:
        return "neutral"
    return None


def _latest_cewc_apply_year(cur, snapshot_date: date) -> int | None:
    """Return the latest meeting record observable at the snapshot."""
    cur.execute(
        """
        SELECT apply_year FROM cewc_annual
        WHERE meeting_end IS NOT NULL AND meeting_end <= %s
        ORDER BY meeting_end DESC, apply_year DESC
        LIMIT 1
        """,
        (snapshot_date,),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _pboc_tone(cur, snapshot_date: date, source: str = "tags_first") -> str | None:
    """评分卡 spec §六 行 181：央行口径三态。

    取数策略（v3.4.4 升级，CEWC 公报 12 月上中旬发布，snapshot=12-31 已在公报后，不算上帝视角）：
    - source='tags_first'（默认）：优先 cewc_tags，缺失退回 cewc_annual
    - source='annual_only'：仅 cewc_annual.monetary_policy

    三态归一化：tight / loose / neutral / None
    """
    apply_year = _latest_cewc_apply_year(cur, snapshot_date)
    if apply_year is None:
        return None

    if source == "tags_first":
        # 优先 cewc_tags（取最新 extracted_at 批次的最高 confidence 记录）
        cur.execute(
            """
            SELECT tag_value FROM cewc_tags
            WHERE apply_year = %s
              AND tag_category = 'policy_stance'
              AND tag_name = 'monetary'
              AND extracted_at = (
                SELECT MAX(extracted_at) FROM cewc_tags WHERE apply_year = %s
              )
            ORDER BY confidence DESC LIMIT 1
            """,
            (apply_year, apply_year),
        )
        row = cur.fetchone()
        if row and row[0]:
            tone = _normalize_pboc_tone(row[0])
            if tone is not None:
                return tone

    # 兜底 / annual_only：cewc_annual.monetary_policy
    cur.execute(
        "SELECT monetary_policy FROM cewc_annual WHERE apply_year = %s",
        (apply_year,),
    )
    row = cur.fetchone()
    if row and row[0]:
        return _normalize_pboc_tone(row[0])
    return None


def _stamp_duty(cur, snapshot_date: date,
                lookback_months: int = 12) -> str | None:
    """评分卡 spec §六 行 182：印花税/IPO 监管事件三态。

    取数：snapshot 之前 lookback_months 月内最近一次事件的 direction。
    数据来源：stamp_duty_events（财政部 + 证监会公开公告，手工 seed）

    Args:
        cur: pymysql cursor
        snapshot_date: 评分基准日（窗口 = (snapshot - lookback_months, snapshot]）
        lookback_months: 回溯窗口月数，默认 12

    Returns:
        'tighten' / 'loosen' / None（窗口内无事件）
    """
    lookback_start = snapshot_date - timedelta(days=lookback_months * 30)
    cur.execute(
        """
        SELECT direction FROM stamp_duty_events
        WHERE effective_date > %s AND effective_date <= %s
        ORDER BY effective_date DESC LIMIT 1
        """,
        (lookback_start, snapshot_date),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    direction = str(row[0]).strip().lower()
    if direction in ("tighten", "loosen"):
        return direction
    return None


def _national_team_action(cur, snapshot_date: date,
                          lookback_months: int = 12,
                          min_intensity: str = "normal") -> str | None:
    """评分卡 spec §六 行新增：国家队入场事件三态。

    取数：snapshot 之前 lookback_months 月内最近一次事件的 direction。
    数据来源：national_team_actions（汇金/证金/平准基金公告手工 seed）

    Args:
        cur: pymysql cursor
        snapshot_date: 评分基准日（窗口 = (snapshot - lookback_months, snapshot]）
        lookback_months: 回溯窗口月数，默认 12
        min_intensity: 'strong' 仅取危机救市强信号；'normal' 包含年度例行增持

    Returns:
        'entry' / 'exit' / None
    """
    lookback_start = snapshot_date - timedelta(days=lookback_months * 30)
    if min_intensity == "strong":
        sql = """
            SELECT direction FROM national_team_actions
            WHERE effective_date > %s AND effective_date <= %s
              AND intensity = 'strong'
            ORDER BY effective_date DESC LIMIT 1
        """
    else:
        sql = """
            SELECT direction FROM national_team_actions
            WHERE effective_date > %s AND effective_date <= %s
            ORDER BY effective_date DESC LIMIT 1
        """
    cur.execute(sql, (lookback_start, snapshot_date))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    d = str(row[0]).strip().lower()
    if d in ("entry", "exit"):
        return d
    return None


def _property_policy(cur, snapshot_date: date,
                     lookback_months: int = 12,
                     min_intensity: str = "normal") -> str | None:
    """评分卡 spec §六（v3.4.10 新增）：房地产政策大转向三态。

    取数：snapshot 之前 lookback_months 月内最近一次事件的 direction。
    数据来源：property_policy_events（住建部/财政部/央行/政治局会议手工 seed）

    Args:
        cur: pymysql cursor
        snapshot_date: 评分基准日（窗口 = (snapshot - lookback_months, snapshot]）
        lookback_months: 回溯窗口月数，默认 12
        min_intensity: 'strong' 仅取大转向 / 'normal' 包含局部调整

    Returns:
        'tighten' / 'loosen' / None
    """
    lookback_start = snapshot_date - timedelta(days=lookback_months * 30)
    if min_intensity == "strong":
        sql = """
            SELECT direction FROM property_policy_events
            WHERE effective_date > %s AND effective_date <= %s
              AND intensity = 'strong'
            ORDER BY effective_date DESC LIMIT 1
        """
    else:
        sql = """
            SELECT direction FROM property_policy_events
            WHERE effective_date > %s AND effective_date <= %s
            ORDER BY effective_date DESC LIMIT 1
        """
    cur.execute(sql, (lookback_start, snapshot_date))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    d = str(row[0]).strip().lower()
    if d in ("tighten", "loosen"):
        return d
    return None


def _normalize_central_meeting_tone(
    raw_tone: str | None,
    raw_theme: str | None = None,
    tag_phrases: list[str] | None = None,
) -> str | None:
    """中央会议口径三态归一化（dual_prevent / expansionary / neutral）。

    复合判别优先级：
        1. dual_prevent — tone/theme 含「防止…过热」「防止…通胀」 或 tag_phrases 含双防关键词
        2. expansionary — 含「保增长 / 扩内需 / 平稳较快发展 / 稳住楼市股市 / 更加积极有为」
        3. neutral     — 含「稳中求进 / 稳健 / 稳定」（默认）
        4. None        — 全空
    """
    haystack = " ".join(filter(None, [raw_tone, raw_theme] + (tag_phrases or [])))
    if not haystack:
        return None

    # 1) dual_prevent
    if "防止" in haystack and ("过热" in haystack or "通胀" in haystack or "通货膨胀" in haystack):
        return "dual_prevent"
    if "双防" in haystack or "防过热" in haystack or "防通胀" in haystack:
        return "dual_prevent"

    # 2) expansionary（积极宽松信号）
    expansionary_keywords = (
        "保增长", "扩内需", "扩大内需", "平稳较快发展",
        "稳住楼市股市", "更加积极有为", "更积极有为",
        "积极的财政政策和适度宽松", "适度宽松的货币政策",
    )
    for kw in expansionary_keywords:
        if kw in haystack:
            return "expansionary"

    # 3) neutral（默认稳健类）
    if "稳中求进" in haystack or "稳健" in haystack or "稳定" in haystack:
        return "neutral"
    return None


def _central_meeting_tone(cur, snapshot_date: date,
                          source: str = "tags_first") -> str | None:
    """评分卡 spec §六 行 183：中央会议口径三态。

    取数策略：
    - source='tags_first'（默认）：复合判别 cewc_tags（key_phrase + primary_focus）
      + cewc_annual.tone + theme；缺失时退回纯 annual 判别。
    - source='annual_only'：仅 cewc_annual.tone + theme 关键字判别。
    """
    apply_year = _latest_cewc_apply_year(cur, snapshot_date)
    if apply_year is None:
        return None

    # 先取 cewc_annual.tone + theme
    cur.execute(
        "SELECT tone, theme FROM cewc_annual WHERE apply_year = %s",
        (apply_year,),
    )
    row = cur.fetchone()
    annual_tone, annual_theme = (row or (None, None))

    if source == "annual_only":
        return _normalize_central_meeting_tone(annual_tone, annual_theme)

    # tags_first：补充 cewc_tags 的 key_phrase + primary_focus tag_value
    cur.execute(
        """
        SELECT tag_value FROM cewc_tags
        WHERE apply_year = %s
          AND tag_category IN ('key_phrase', 'primary_focus')
          AND extracted_at = (
            SELECT MAX(extracted_at) FROM cewc_tags WHERE apply_year = %s
          )
        """,
        (apply_year, apply_year),
    )
    tag_values = [str(r[0]) for r in cur.fetchall() if r and r[0]]

    return _normalize_central_meeting_tone(annual_tone, annual_theme, tag_values)


def _ppi_yoy_and_change(cur, snapshot_month: str) -> tuple[float | None, str | None]:
    """当月 ppi_yoy 与 12 月前对比，输出三态变化"""
    cur.execute(
        "SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1",
        (snapshot_month,),
    )
    cur_row = cur.fetchone()
    if not cur_row or cur_row[0] is None:
        return None, None
    cur_val = float(cur_row[0])

    # 12 月前
    sm_year = int(snapshot_month[:4]) - 1
    sm_month = snapshot_month[4:6]
    prior_month = f"{sm_year}{sm_month}"
    cur.execute(
        "SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1",
        (prior_month,),
    )
    prior_row = cur.fetchone()
    if not prior_row or prior_row[0] is None:
        return cur_val, "flat"
    prior_val = float(prior_row[0])

    # 状态变化：跨零判定
    if prior_val >= 0 and cur_val < 0:
        return cur_val, "turn_negative"
    if prior_val < 0 and cur_val >= 0:
        return cur_val, "turn_positive"
    return cur_val, "flat"


def _previous_month_key(snapshot_date: date) -> str:
    if snapshot_date.month == 1:
        return f"{snapshot_date.year - 1}12"
    return f"{snapshot_date.year}{snapshot_date.month - 1:02d}"


def observable_macro_months(snapshot_date: date) -> dict[str, str]:
    """Reference months safe to use when source rows lack release dates."""

    previous = _previous_month_key(snapshot_date)
    return {"pmi": previous, "ppi": previous}


def _roe_implied_and_trend(
    cur, ts_code: str, snapshot_date: date,
) -> tuple[float | None, str | None]:
    """v12-R1: 隐含 ROE 及 3 年趋势（rising/flat/declining）"""
    three_y_ago = snapshot_date - timedelta(days=365 * 3)
    cur.execute(
        """
        SELECT pe_ttm, pb FROM index_dailybasic
        WHERE ts_code = %s AND trade_date BETWEEN %s AND %s
          AND pe_ttm > 0 AND pb > 0
        ORDER BY trade_date
        """,
        (ts_code, three_y_ago, snapshot_date),
    )
    rows = cur.fetchall()
    if len(rows) < 60:
        return None, None
    roes = [float(r[1]) / float(r[0]) * 100.0 for r in rows]
    recent = roes[-120:] if len(roes) >= 120 else roes
    prior = roes[:120] if len(roes) >= 120 else roes[: len(roes) // 2]
    roe_now = sum(recent) / len(recent)
    roe_prior = sum(prior) / len(prior)
    diff = roe_now - roe_prior
    trend = "rising" if diff > 1.0 else ("declining" if diff < -1.0 else "flat")
    return roe_now, trend


def _enterprise_boom_index(cur, snapshot_date: date) -> float | None:
    """v13-B1: use the latest quarter after a conservative 45-day release lag."""
    observable_quarter_end = snapshot_date - timedelta(days=45)
    cur.execute(
        """
        SELECT boom_index FROM cn_enterprise_boom_quarterly
        WHERE quarter_date <= %s AND boom_index IS NOT NULL
        ORDER BY quarter_date DESC LIMIT 1
        """,
        (observable_quarter_end,),
    )
    row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _us10y_chg_12m_bp(cur, snapshot_date: date) -> float | None:
    """v12-M4: 美 10Y 名义收益率 12 月变化（bp）"""
    one_year_ago = snapshot_date - timedelta(days=365)
    cur.execute(
        """
        SELECT y10 FROM us_tycr_daily
        WHERE trade_date <= %s AND y10 IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
        """,
        (snapshot_date,),
    )
    cur_row = cur.fetchone()
    cur.execute(
        """
        SELECT y10 FROM us_tycr_daily
        WHERE trade_date <= %s AND y10 IS NOT NULL
        ORDER BY trade_date DESC LIMIT 1
        """,
        (one_year_ago,),
    )
    prior_row = cur.fetchone()
    if not (cur_row and prior_row and cur_row[0] is not None and prior_row[0] is not None):
        return None
    return (float(cur_row[0]) - float(prior_row[0])) * 100.0


def _cs300_6m_return(cur, ts_code: str, snapshot_date: date) -> float | None:
    """v12-M2: CS300 过去 6 月累计收益 %（供 momentum_filter 使用）"""
    six_mo_ago = snapshot_date - timedelta(days=183)
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (ts_code, snapshot_date),
    )
    cur_row = cur.fetchone()
    cur.execute(
        """
        SELECT close FROM index_daily
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 1
        """,
        (ts_code, six_mo_ago),
    )
    prior_row = cur.fetchone()
    if not (cur_row and prior_row and cur_row[0] is not None and prior_row[0] is not None):
        return None
    return (float(cur_row[0]) / float(prior_row[0]) - 1.0) * 100.0


def _cs300_pe_p20_60m(cur, ts_code: str, snapshot_date: date) -> float | None:
    """v12-M3: PE 60 月滚动 P20"""
    five_y_ago = snapshot_date - timedelta(days=365 * 5)
    cur.execute(
        """
        SELECT pe_ttm FROM index_dailybasic
        WHERE ts_code = %s AND trade_date BETWEEN %s AND %s
          AND pe_ttm IS NOT NULL
        """,
        (ts_code, five_y_ago, snapshot_date),
    )
    pes = [float(r[0]) for r in cur.fetchall() if r[0] is not None]
    if len(pes) < 200:
        return None
    pes.sort()
    idx = int(len(pes) * 0.20)
    return pes[idx]


# ──────────────────────────────────────────────────────────────────
# 主入口：构造 ScorecardInputs
# ──────────────────────────────────────────────────────────────────
@dataclass
class AdapterOptions:
    """适配器开关 — 用于 backtest 对比 baseline vs candidate"""
    include_pmi_3m_avg: bool = True          # 是否填充 pmi_mfg_3m_avg
    include_pmi_prod_order: bool = True      # 是否填充 pmi_prod_minus_order
    include_us_monthly_pct: bool = True      # 是否填充 us_monthly_pct（默认 True：spec 承诺接入 + v3.4.3 验证 ρ 略优 P&L 持平）
    include_global_recession: bool = False   # v3.4.4 候选：默认关闭，回测 P&L -1.77pp 且 ρ 恶化 0.11，未采纳（详见 scripts/backtest_scorecard_v344.py）
    include_global_stimulus: bool = True     # v3.4.7 已采纳：累计回报 71.51→77.41%（+5.90pp）/ 回撤持平 / ρ -0.52→-0.51（噪声级），详见 scripts/backtest_scorecard_v344_stimulus.py
    include_pboc_tone: bool = True           # v3.4.5 已采纳：回测累计回报 70.11→71.51% / ρ -0.41→-0.52，仅 2025 跨档
    pboc_tone_source: str = "tags_first"     # 'tags_first'（cewc_tags 优先+cewc_annual 兜底）/ 'annual_only'
    include_stamp_duty: bool = True          # v3.4.6 已采纳：回测累计回报 71.51→109.66%（+38pp），最大回撤 -32.12→-25.90%（+6pp 改善）
    include_central_meeting_tone: bool = False   # v3.4.8 REJECT：P&L 完全持平（8 次触发都在档位内未跨档），按 v3.4.2 同等"档位吞没单信号"标准不采纳；保留实现供未来若细化档位映射后重启评估
    central_meeting_tone_source: str = "tags_first"  # 'tags_first'（cewc_tags + cewc_annual 复合）/ 'annual_only'
    include_national_team: bool = True             # v3.4.9 已采纳：回测累计回报 112.17→114.27%（+2.10pp），回撤持平，仅救市强信号触发（避免年度例行增持的噪声）
    national_team_min_intensity: str = "strong"    # 'strong'（仅救市强信号，推荐）/ 'normal'（含年度例行）
    include_property_policy: bool = False          # v3.4.10 REJECT：+bidir 累计回报 -3.00pp（2016 loosen 加仓+地产周期与 A 股 mismatch）/ +loose_only -1.45pp；保留实现供未来重启
    property_policy_min_intensity: str = "strong"  # 'strong'（仅大转向）/ 'normal'（含局部调整）
    property_policy_mode: str = "bidir"            # 'bidir'（双向 ±1）/ 'loose_only'（仅 loosen -1，tighten 视为 None）
    include_roe_trend: bool = True                 # v12-R1 已采纳：PE 信号结合 ROE 趋势，避免估值陷阱
    include_enterprise_boom: bool = True           # v13-B1 已采纳：企业景气 <110 → -1 机会
    include_us10y_chg: bool = True                   # v12-M4 已采纳：美 10Y 12 月升 >100bp → +1 风险
    include_momentum_fields: bool = True             # v12-M2/M3：加载 cs300_6m_return / pe_p20（供动量过滤脚本）


def load_scorecard_inputs(
    snapshot_date: date,
    *,
    options: AdapterOptions | None = None,
    cs300_code: str = "000300.SH",
    conn: pymysql.connections.Connection | None = None,
) -> ScorecardInputs:
    """从 MySQL 加载评分输入（仅估值 + 流动性 + 基本面三维）。

    Args:
        snapshot_date: 评分基准日（如 2009-12-31，用于 apply_year=2010 评分）
        options: 字段开关（baseline 把 PMI 新字段关闭以做对比）
        cs300_code: 沪深300 代码
        conn: 复用外部连接；None 时自建并关闭

    Returns:
        ScorecardInputs：覆盖估值/流动性/基本面三维字段
    """
    opts = options or AdapterOptions()
    own_conn = conn is None
    if own_conn:
        conn = pymysql.connect(**mysql_config())

    # The local PMI table stores a reference month but no publication date.
    # Treat the current month as unavailable at a month-end decision; otherwise
    # historical snapshots can see PMI releases that were published only after
    # the rebalance boundary.  This matches the already-conservative PPI rule.
    observable_months = observable_macro_months(snapshot_date)
    pmi_observable_month = observable_months["pmi"]
    ppi_observable_month = observable_months["ppi"]

    try:
        with conn.cursor() as cur:
            pe, pb = _latest_pe_pb(cur, cs300_code, snapshot_date)
            rate_bp = _shibor_3m_cum_bp(cur, snapshot_date)
            rrr_pp = _rrr_cum_pp_12m(cur, snapshot_date)
            deposit = _deposit_1y_rate(cur, snapshot_date)

            pmi_series = _pmi_series_until(cur, pmi_observable_month, n=12)
            below_52 = _pmi_below_52_consecutive(pmi_series)
            resume = _pmi_resume_expansion(pmi_series)
            mfg_3m = _pmi_mfg_3m_avg(pmi_series) if opts.include_pmi_3m_avg else None
            prod_order = (
                _pmi_prod_minus_order(pmi_series)
                if opts.include_pmi_prod_order else None
            )

            ppi_yoy, ppi_change = _ppi_yoy_and_change(cur, ppi_observable_month)

            us_pct = _us_monthly_pct(cur, snapshot_date) if opts.include_us_monthly_pct else None
            recession = (
                _global_recession(cur, snapshot_date)
                if opts.include_global_recession else False
            )
            stimulus = (
                _global_stimulus(cur, snapshot_date)
                if opts.include_global_stimulus else False
            )
            pboc_tone = (
                _pboc_tone(cur, snapshot_date, source=opts.pboc_tone_source)
                if opts.include_pboc_tone else None
            )
            stamp_duty = (
                _stamp_duty(cur, snapshot_date)
                if opts.include_stamp_duty else None
            )
            meeting_tone = (
                _central_meeting_tone(
                    cur, snapshot_date,
                    source=opts.central_meeting_tone_source,
                )
                if opts.include_central_meeting_tone else None
            )
            national_team = (
                _national_team_action(
                    cur, snapshot_date,
                    min_intensity=opts.national_team_min_intensity,
                )
                if opts.include_national_team else None
            )
            property_policy = None
            if opts.include_property_policy:
                _raw_property = _property_policy(
                    cur, snapshot_date,
                    min_intensity=opts.property_policy_min_intensity,
                )
                if opts.property_policy_mode == "loose_only":
                    property_policy = _raw_property if _raw_property == "loosen" else None
                else:  # 'bidir'
                    property_policy = _raw_property

            roe_implied, roe_trend = (
                _roe_implied_and_trend(cur, cs300_code, snapshot_date)
                if opts.include_roe_trend else (None, None)
            )
            enterprise_boom = (
                _enterprise_boom_index(cur, snapshot_date)
                if opts.include_enterprise_boom else None
            )
            us10y_chg = (
                _us10y_chg_12m_bp(cur, snapshot_date)
                if opts.include_us10y_chg else None
            )
            cs300_6m = pe_p20 = None
            if opts.include_momentum_fields:
                cs300_6m = _cs300_6m_return(cur, cs300_code, snapshot_date)
                pe_p20 = _cs300_pe_p20_60m(cur, cs300_code, snapshot_date)
    finally:
        if own_conn:
            conn.close()

    return ScorecardInputs(
        # 估值
        cs300_pe_ttm=pe,
        cs300_pb=pb,
        # 流动性
        rate_cum_bp_12m=rate_bp,
        rrr_cum_pp_12m=rrr_pp,
        deposit_1y_rate=deposit,
        # 基本面
        pmi_below_52_months=below_52,
        pmi_resume_expansion=resume,
        pmi_mfg_3m_avg=mfg_3m,
        pmi_prod_minus_order=prod_order,
        ppi_yoy=ppi_yoy,
        ppi_yoy_change=ppi_change,
        iva_yoy_trend=None,  # cn_iva_monthly 表不存在，留空
        # 外部 — 接入 us_monthly_pct；global_recession / global_stimulus 默认关（候选）
        us_monthly_pct=us_pct,
        global_recession=recession,
        global_stimulus=stimulus,
        # 政策 — v3.4.5：pboc_tone；v3.4.6：stamp_duty；v3.4.8 候选：central_meeting_tone；v3.4.9：national_team；v3.4.10 候选：property_policy
        pboc_tone=pboc_tone,
        stamp_duty=stamp_duty,
        central_meeting_tone=meeting_tone,
        national_team_action=national_team,
        property_policy=property_policy,
        # v12/v13 新增字段
        roe_implied=roe_implied,
        roe_3y_trend=roe_trend,
        enterprise_boom_index=enterprise_boom,
        us10y_chg_12m_bp=us10y_chg,
        cs300_6m_return=cs300_6m,
        cs300_pe_p20_60m=pe_p20,
        # 情绪 / 外部其余 — 不接入（保持 baseline / candidate 同等空白）
    )
