#!/usr/bin/env python3
"""长周期组合回测：2007-2025（19年）

对比三条策略：
  A. 纯沪深300 买入持有（基准）
  B. 年度评分卡 择时 × 沪深300（仓位调节，权益部分持 CS300）
  C. 年度评分卡 × 行业评分卡（仓位调节 + Top-N 行业指数选股）

行业指数宇宙：CSI 优先，无 CSI 价格数据时用 SI（申万）替代
初始资金：1,000,000 元
换仓周期：每年 1 月 1 日

用法：
  python3 scripts/backtest_long.py
  python3 scripts/backtest_long.py --top 5
  python3 scripts/backtest_long.py --top 10
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pymysql
from dotenv import load_dotenv

from backtest.scorecard import ScorecardInputs, evaluate_scorecard

load_dotenv(ROOT / ".env")

BENCHMARK        = "000300.SH"
INITIAL_CAPITAL  = 1_000_000
BACKTEST_YEARS   = list(range(2007, 2026))   # 2007-2025，共 19 年
MOMENTUM_DAYS    = 252
MIN_HISTORY_DAYS = 20   # SI 为周频数据，年均 ~60 行；CSI 日频年均 ~250 行

STRENGTH_SCORE  = {"强": 3, "中": 2, "弱": 1}
RELEVANCE_SCORE = {"强": 3, "中": 2, "弱": 1}
FACTOR_WEIGHTS  = {"policy": 0.40, "momentum": 0.35, "macro": 0.15, "north": 0.10}

# 主题持续时长乘数：同一主题连续出现越多年，F1 政策分乘数越大（上限 2×）。
# 经济逻辑：长期结构性主题（如贸易战）即使未立即兑现，累积的概率敞口在统计上正向。
THEME_DURATION_MULT_PER_YEAR = 0.15   # 每额外连续出现一年 +15%
THEME_DURATION_MULT_CAP      = 2.0    # 乘数上限（对应连续 8 年以上）

# 热度抑制：板块 2 年累计涨幅过高时，自动对综合分施加负向惩罚，防止高位追入。
# 经济逻辑：大涨后均值回归风险上升，继续重仓该板块的风险收益比恶化。
MOMENTUM_HEAT_THRESHOLD = 0.50   # 2 年累计>50% 触发
MOMENTUM_HEAT_WEIGHT    = 0.20   # 惩罚项权重（负值，最大贡献 -0.20）

# F2 动量衰减：主题持续年数越长，该指数的动量权重越低。
# 经济逻辑：新主题需要动量确认；多年结构性主题的短期动量是滞后噪声。
MOMENTUM_DECAY_PER_YEAR = 0.15   # 每多持续一年，F2 权重衰减 15%
MOMENTUM_DECAY_FLOOR    = 0.20   # 最低保留 20%（防止完全忽视动量）


def get_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "teststock"),
        password=os.getenv("MYSQL_PASSWORD", "teststock"),
        database=os.getenv("MYSQL_DATABASE", "teststock"),
        charset="utf8mb4",
    )


# ── 年度评分卡仓位 ────────────────────────────────────────────────────

def load_scorecard_position(conn, signal_year: int) -> float:
    """用 signal_year 年底数据调 v3.4 评分卡，返回目标仓位 0~1。"""
    snap_str = f"{signal_year}-12-31"
    one_y    = f"{signal_year - 1}-12-31"
    two_y    = f"{signal_year - 2}-12-31"
    snap_m   = f"{signal_year}12"
    six_m    = f"{signal_year}-06-30"

    def q(sql, *args):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()

    def qall(sql, *args):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    # ── 估值 ──────────────────────────────────────────────────────────
    r = q("SELECT pe_ttm FROM index_dailybasic WHERE ts_code='000300.SH' AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1", snap_str)
    pe = float(r[0]) if r and r[0] else None

    # ── 流动性 ────────────────────────────────────────────────────────
    # Shibor 3M 年度变化 → 近似利率累计变化 bp
    r0 = q("SELECT rate_3m FROM shibor_daily WHERE trade_date<=%s ORDER BY trade_date DESC LIMIT 1", one_y)
    r1 = q("SELECT rate_3m FROM shibor_daily WHERE trade_date<=%s ORDER BY trade_date DESC LIMIT 1", snap_str)
    rate_cum_bp = (float(r1[0]) - float(r0[0])) * 100 if r0 and r1 and r0[0] and r1[0] else None

    # 降准累计 pp（负值表示降准）
    r = q("SELECT COALESCE(SUM(rrr_change_pp),0) FROM cn_rrr_changes WHERE effective_date>%s AND effective_date<=%s AND inst_type IN ('large','all')", one_y, snap_str)
    rrr_cum = float(r[0]) if r else 0.0

    # ── 基本面 — PMI ──────────────────────────────────────────────────
    # 连续低于 52 的月数（取近 24 个月倒推）
    pmi_rows = qall(
        "SELECT pmi_manu FROM macro_pmi WHERE month<=%s AND pmi_manu IS NOT NULL ORDER BY month DESC LIMIT 24",
        snap_m,
    )
    pmi_below_52_months = 0
    for (v,) in pmi_rows:
        if float(v) < 52:
            pmi_below_52_months += 1
        else:
            break
    # PMI 是否从 <50 回到 ≥50（上月 <50，本月 ≥50）
    pmi_last2 = qall(
        "SELECT pmi_manu FROM macro_pmi WHERE month<=%s AND pmi_manu IS NOT NULL ORDER BY month DESC LIMIT 2",
        snap_m,
    )
    pmi_resume_expansion = False
    if len(pmi_last2) == 2:
        cur_pmi, prev_pmi = float(pmi_last2[0][0]), float(pmi_last2[1][0])
        pmi_resume_expansion = (prev_pmi < 50) and (cur_pmi >= 50)

    # ── 情绪 — 两融同比 ───────────────────────────────────────────────
    r_cur  = q("SELECT SUM(rzye) FROM margin_daily WHERE trade_date>%s AND trade_date<=%s", one_y, snap_str)
    r_prev = q("SELECT SUM(rzye) FROM margin_daily WHERE trade_date>%s AND trade_date<=%s", two_y, one_y)
    margin_growth = None
    if r_cur and r_prev and r_cur[0] and r_prev[0] and float(r_prev[0]) > 0:
        margin_growth = (float(r_cur[0]) - float(r_prev[0])) / float(r_prev[0]) * 100

    # ── 外部宏观（FRED）─────────────────────────────────────────────
    def fred_val(sid, date_str):
        r = q("SELECT value FROM macro_rates WHERE series_id=%s AND obs_date<=%s AND value IS NOT NULL ORDER BY obs_date DESC LIMIT 1", sid, date_str)
        return float(r[0]) if r and r[0] else None

    fed_prev = fred_val("FEDFUNDS", one_y)
    fed_cur  = fred_val("FEDFUNDS", snap_str)
    # 判断美联储方向转向
    fed_reversal = None
    if fed_prev is not None and fed_cur is not None:
        if fed_cur > fed_prev + 0.5:
            fed_reversal = "cut_to_hike"
        elif fed_cur < fed_prev - 0.5:
            fed_reversal = "hike_to_cut"

    # 美股月度涨跌（纳指 12 月单月）
    nq_m_start = fred_val("NASDAQCOM", f"{signal_year}-11-30")
    nq_m_end   = fred_val("NASDAQCOM", snap_str)
    us_monthly = ((nq_m_end / nq_m_start) - 1) * 100 if nq_m_start and nq_m_end and nq_m_start > 0 else None

    # ── 价格动量 — CS300 6M ──────────────────────────────────────────
    p_end  = q("SELECT close FROM index_daily WHERE ts_code='000300.SH' AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1", snap_str)
    p_mid  = q("SELECT close FROM index_daily WHERE ts_code='000300.SH' AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1", six_m)
    cs300_6m = ((float(p_end[0]) / float(p_mid[0])) - 1) * 100 if p_end and p_mid and p_mid[0] and float(p_mid[0]) > 0 else None

    # ── 政策 ──────────────────────────────────────────────────────────
    r = q("SELECT COUNT(*) FROM stamp_duty_events WHERE effective_date>%s AND effective_date<=%s AND direction='reduce'", one_y, snap_str)
    stamp_duty = 'loosen' if (r and r[0] > 0) else None

    r = q("SELECT COUNT(*) FROM national_team_actions WHERE effective_date>%s AND effective_date<=%s AND action_type IN ('buy','increase')", one_y, snap_str)
    nt_action = 'entry' if (r and r[0] > 0) else None

    # 央行口径：有降准或降息 → 近似为宽松
    pboc_tone = None
    if rrr_cum < -0.5 or (rate_cum_bp is not None and rate_cum_bp < -20):
        pboc_tone = 'loose'
    elif rrr_cum > 0.5 or (rate_cum_bp is not None and rate_cum_bp > 20):
        pboc_tone = 'tight'

    inp = ScorecardInputs(
        cs300_pe_ttm=pe,
        rate_cum_bp_12m=rate_cum_bp,
        rrr_cum_pp_12m=rrr_cum,
        margin_growth_pct=margin_growth,
        cs300_6m_return=cs300_6m,
        pmi_below_52_months=pmi_below_52_months,
        pmi_resume_expansion=pmi_resume_expansion,
        fed_reversal=fed_reversal,
        us_monthly_pct=us_monthly,
        stamp_duty=stamp_duty,
        national_team_action=nt_action,
        pboc_tone=pboc_tone,
    )
    result = evaluate_scorecard(signal_year, inp)
    return result.target_equity_pct / 100.0


# ── 行业评分卡选股 ────────────────────────────────────────────────────

def get_theme_duration(conn, theme: str, up_to_year: int) -> int:
    """主题在 annual_sector_signals 中截至 up_to_year 的连续出现年数（含当年）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT apply_year FROM annual_sector_signals "
            "WHERE theme=%s AND apply_year<=%s ORDER BY apply_year DESC",
            (theme, up_to_year),
        )
        years = [r[0] for r in cur.fetchall()]
    if not years or years[0] != up_to_year:
        return 0
    count = 1
    for i in range(1, len(years)):
        if years[i] == up_to_year - i:
            count += 1
        else:
            break
    return count


def get_macro_norm(conn, year: int) -> float:
    h1, h2 = f"{year - 1}-06-01", f"{year - 1}-12-31"
    score = 0.0
    with conn.cursor() as cur:
        for sid, sign in [("FEDFUNDS", -1), ("DTWEXBGS", -1), ("NASDAQCOM", 1)]:
            cur.execute("SELECT value FROM macro_rates WHERE series_id=%s AND obs_date>=%s AND value IS NOT NULL ORDER BY obs_date LIMIT 1", (sid, h1))
            r0 = cur.fetchone()
            cur.execute("SELECT value FROM macro_rates WHERE series_id=%s AND obs_date<=%s AND value IS NOT NULL ORDER BY obs_date DESC LIMIT 1", (sid, h2))
            r1 = cur.fetchone()
            if r0 and r1 and r0[0] and r1[0]:
                score += sign * (1.0 if sign * (float(r1[0]) - float(r0[0])) > 0 else -1.0)
    return score / 3.0


def get_north_sign(conn, year: int) -> float:
    if year < 2015:   # 北向 2014-11 才开通
        return 0.0
    start, end = f"{year - 1}-10-01", f"{year - 1}-12-31"
    with conn.cursor() as cur:
        cur.execute("SELECT north_money FROM moneyflow_hsgt WHERE trade_date BETWEEN %s AND %s AND north_money IS NOT NULL ORDER BY trade_date", (start, end))
        vals = [float(r[0]) for r in cur.fetchall()]
    if len(vals) < 10:
        return 0.0
    slope = float(np.polyfit(np.arange(len(vals)), vals, 1)[0])
    return 1.0 if slope > 0 else -1.0


def zscore_map(values: list[float], clip: float = 3.0) -> dict[float, float]:
    if not values:
        return {}
    arr = np.array(values, dtype=float)
    mu, std = arr.mean(), arr.std()
    if std < 1e-9:
        return {v: 0.0 for v in values}
    return {v: float(np.clip((v - mu) / std, -clip, clip)) for v in values}


def select_indices(conn, year: int, top_n: int) -> list[tuple[str, float, float]]:
    """CSI 优先 + 相关性去重。返回 [(ts_code, composite_score, hist_vol), ...]。"""
    macro_norm = get_macro_norm(conn, year)
    north_sign = get_north_sign(conn, year)

    # 政策信号
    with conn.cursor() as cur:
        cur.execute("SELECT theme, signal_strength FROM annual_sector_signals WHERE apply_year=%s", (year,))
        sig_rows = cur.fetchall()
    signals: dict[str, int] = {}
    for theme, strength in sig_rows:
        signals[theme] = max(signals.get(theme, 0), STRENGTH_SCORE.get(strength, 0))

    # 主题映射（CSI + SI）
    with conn.cursor() as cur:
        cur.execute("SELECT ts_code, theme, relevance FROM theme_index_map WHERE ts_code LIKE '%%.CSI' OR ts_code LIKE '%%.SI'")
        map_rows = cur.fetchall()

    # 各指数前两年价格（动量 + 热度计算，多取一年）
    prev_start = f"{year - 3}-01-01"
    prev_end   = f"{year - 1}-12-31"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts_code, trade_date, close FROM index_daily "
            "WHERE (ts_code LIKE '%%.CSI' OR ts_code LIKE '%%.SI') "
            "AND trade_date BETWEEN %s AND %s ORDER BY ts_code, trade_date",
            (prev_start, prev_end),
        )
        price_rows = cur.fetchall()

    prices: dict[str, list[float]] = defaultdict(list)
    for ts, _, cl in price_rows:
        if cl:
            prices[ts].append(float(cl))

    # 只保留有足够历史的指数
    valid_ts = {ts for ts, ps in prices.items() if len(ps) >= MIN_HISTORY_DAYS}

    # 聚合政策分（含主题持续时长乘数）
    theme_dur_cache: dict[str, int] = {}

    def theme_dur(theme: str) -> int:
        if theme not in theme_dur_cache:
            theme_dur_cache[theme] = get_theme_duration(conn, theme, year)
        return theme_dur_cache[theme]

    code_pol: dict[str, float] = {}
    ts_themes: dict[str, set[str]] = defaultdict(set)   # ts_code → 有效主题集合
    for ts, theme, rel in map_rows:
        if ts not in valid_ts:
            continue
        sig_strength = signals.get(theme, 0)
        if sig_strength == 0:
            continue
        ts_themes[ts].add(theme)
        dur = theme_dur(theme)
        dur_mult = min(1.0 + THEME_DURATION_MULT_PER_YEAR * (dur - 1), THEME_DURATION_MULT_CAP)
        pol = float(sig_strength * RELEVANCE_SCORE.get(rel, 1) * dur_mult)
        code_pol[ts] = code_pol.get(ts, 0.0) + pol

    if not code_pol:
        return []

    codes    = list(code_pol.keys())
    pol_vals = [code_pol[ts] for ts in codes]

    def momentum(ts: str) -> float | None:
        ps = prices.get(ts, [])
        n  = min(MOMENTUM_DAYS, len(ps))
        if n < 20 or ps[-n] <= 0:
            return None
        return (ps[-1] - ps[-n]) / ps[-n]

    mom_raw  = [momentum(ts) for ts in codes]
    mom_vals = [v for v in mom_raw if v is not None]

    pol_z = zscore_map(pol_vals)
    mom_z = zscore_map(mom_vals)

    def heat_penalty(ts: str) -> float:
        """2 年累计涨幅超阈值时返回负值惩罚 (-1 ~ 0)，防止高位追入。"""
        ps = prices.get(ts, [])
        lookback = MOMENTUM_DAYS * 2
        if len(ps) < lookback:
            return 0.0
        start, end = ps[-lookback], ps[-1]
        if start <= 0:
            return 0.0
        cum_2y = end / start - 1
        if cum_2y <= MOMENTUM_HEAT_THRESHOLD:
            return 0.0
        excess = (cum_2y - MOMENTUM_HEAT_THRESHOLD) / MOMENTUM_HEAT_THRESHOLD
        return -min(excess, 1.0)

    scored = []
    for i, ts in enumerate(codes):
        f1 = pol_z.get(pol_vals[i], 0.0)
        # F2 动量衰减：主题持续越久，动量信号权重越低
        max_dur = max((theme_dur(t) for t in ts_themes.get(ts, [])), default=0)
        f2_decay = max(MOMENTUM_DECAY_FLOOR, 1.0 - MOMENTUM_DECAY_PER_YEAR * (max_dur - 1)) if max_dur > 1 else 1.0
        f2 = (mom_z.get(mom_raw[i], 0.0) * f2_decay) if mom_raw[i] is not None else 0.0
        heat = heat_penalty(ts)
        sc = (FACTOR_WEIGHTS["policy"]   * f1
            + FACTOR_WEIGHTS["momentum"] * f2
            + FACTOR_WEIGHTS["macro"]    * macro_norm
            + FACTOR_WEIGHTS["north"]    * north_sign
            + MOMENTUM_HEAT_WEIGHT       * heat)
        scored.append((ts, sc))

    # CSI 优先 + 相关性去重（ρ > 0.85 视为重叠，只保留分数更高的）
    # 候选顺序：CSI 按分数排，不足再接 SI 按分数排
    CORR_THRESHOLD = 0.85
    csi = sorted([(ts, sc) for ts, sc in scored if ts.endswith(".CSI")], key=lambda x: -x[1])
    si  = sorted([(ts, sc) for ts, sc in scored if ts.endswith(".SI")],  key=lambda x: -x[1])
    candidates = csi + si

    def log_returns(ts: str) -> np.ndarray | None:
        ps = prices.get(ts, [])
        if len(ps) < 20:
            return None
        arr = np.array(ps, dtype=float)
        return np.diff(np.log(arr))

    selected: list[str] = []
    selected_rets: list[np.ndarray] = []
    score_map: dict[str, float] = dict(scored)

    for ts, _ in candidates:
        if len(selected) >= top_n:
            break
        rets = log_returns(ts)
        if rets is None:
            continue
        too_correlated = False
        for existing in selected_rets:
            n = min(len(rets), len(existing))
            if n < 20:
                continue
            rho = float(np.corrcoef(rets[-n:], existing[-n:])[0, 1])
            if rho > CORR_THRESHOLD:
                too_correlated = True
                break
        if not too_correlated:
            selected.append(ts)
            selected_rets.append(rets)

    # 返回 (ts_code, composite_score, hist_vol) — hist_vol 用前一年日/周对数收益的年化标准差
    result = []
    for ts, rets in zip(selected, selected_rets):
        periods_per_year = 252 if ts.endswith(".CSI") else 52
        hist_vol = float(np.std(rets) * np.sqrt(periods_per_year)) if len(rets) > 1 else 0.3
        result.append((ts, score_map[ts], hist_vol))
    return result


# ── 年度收益率 ────────────────────────────────────────────────────────

def annual_ret(conn, ts_code: str, year: int) -> float | None:
    s, e = f"{year}-01-01", f"{year}-12-31"
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(trade_date), MAX(trade_date) FROM index_daily WHERE ts_code=%s AND trade_date BETWEEN %s AND %s", (ts_code, s, e))
        r = cur.fetchone()
        if not r or not r[0]:
            return None
        cur.execute("SELECT close FROM index_daily WHERE ts_code=%s AND trade_date=%s", (ts_code, str(r[0])))
        p0 = cur.fetchone()
        cur.execute("SELECT close FROM index_daily WHERE ts_code=%s AND trade_date=%s", (ts_code, str(r[1])))
        p1 = cur.fetchone()
    if p0 and p1 and p0[0] and p1[0] and float(p0[0]) > 0:
        return float(p1[0]) / float(p0[0]) - 1
    return None


# ── 主回测 ────────────────────────────────────────────────────────────

def invvol_ret(sel: list[tuple[str, float, float]], actual_rets: list[float | None]) -> float:
    """风险平价：按历史波动率倒数加权。sel = [(ts, score, hist_vol), ...]"""
    pairs = [(hv, r) for (_, _, hv), r in zip(sel, actual_rets) if r is not None]
    if not pairs:
        return 0.0
    vols = np.array([max(p[0], 1e-6) for p in pairs])
    rets = np.array([p[1] for p in pairs])
    w = (1.0 / vols) / (1.0 / vols).sum()
    return float(np.dot(w, rets))


def run(top_n: int) -> None:
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT ts_code, index_name FROM theme_index_map")
        name_map = {r[0]: r[1] for r in cur.fetchall()}

    nav_a = INITIAL_CAPITAL   # 纯 CS300
    nav_b = INITIAL_CAPITAL   # 评分卡择时 × CS300
    nav_c = INITIAL_CAPITAL   # 评分卡择时 × 行业指数（风险平价）

    rows = []
    print(f"\n{'='*100}")
    print(f"长周期回测 {BACKTEST_YEARS[0]}-{BACKTEST_YEARS[-1]}  初始资金={INITIAL_CAPITAL:,}元  Top{top_n}行业指数（风险平价）")
    print(f"{'年份':>5} {'仓位':>5} {'基准':>8} {'B评分卡':>8} {'C风险平价':>10}  "
          f"{'基准净值':>10} {'B净值':>10} {'C净值':>10}  选中指数（前3）")
    print(f"{'='*100}")

    for year in BACKTEST_YEARS:
        pos     = load_scorecard_position(conn, year - 1)
        bm_ret  = annual_ret(conn, BENCHMARK, year) or 0.0
        sel     = select_indices(conn, year, top_n)
        ts_list = [ts for ts, _, _ in sel]
        actual  = [annual_ret(conn, ts, year) for ts in ts_list]
        eq_ret  = invvol_ret(sel, actual)

        ret_a = bm_ret
        ret_b = pos * bm_ret
        ret_c = pos * eq_ret

        nav_a *= (1 + ret_a)
        nav_b *= (1 + ret_b)
        nav_c *= (1 + ret_c)

        top3_str = "  ".join(
            f"{ts}({name_map.get(ts,'')[:8]},{r*100:+.0f}%)"
            for ts, r in zip(ts_list[:3], [r for r in actual[:3] if r is not None])
        ) if ts_list else "—"

        rows.append(dict(
            year=year, pos=pos, ret_a=ret_a, ret_b=ret_b, ret_c=ret_c,
            nav_a=nav_a, nav_b=nav_b, nav_c=nav_c,
            sel=ts_list, actual=actual,
        ))

        flag_b = "↑" if ret_b > ret_a else "↓"
        flag_c = "↑" if ret_c > ret_a else "↓"
        print(f"{year:>5} {pos*100:>4.0f}%"
              f" {ret_a*100:>+7.1f}%"
              f" {ret_b*100:>+7.1f}%{flag_b}"
              f" {ret_c*100:>+9.1f}%{flag_c}"
              f"  {nav_a/1e4:>8.1f}万  {nav_b/1e4:>8.1f}万  {nav_c/1e4:>8.1f}万"
              f"  {top3_str}")

    conn.close()

    # ── 统计 ──────────────────────────────────────────────────────────
    rets_a = [r["ret_a"] for r in rows]
    rets_b = [r["ret_b"] for r in rows]
    rets_c = [r["ret_c"] for r in rows]
    n = len(rows)

    def stats(rets, nav_final):
        ann    = (nav_final / INITIAL_CAPITAL) ** (1 / n) - 1
        vol    = float(np.std(rets))
        sharpe = ann / vol if vol > 0 else 0
        curve  = [INITIAL_CAPITAL]
        for r in rets:
            curve.append(curve[-1] * (1 + r))
        peak, mdd = curve[0], 0.0
        for v in curve:
            peak = max(peak, v)
            mdd  = max(mdd, (peak - v) / peak)
        wins = sum(1 for r in rets if r > 0)
        return ann, vol, sharpe, mdd, wins

    ann_a, vol_a, sr_a, mdd_a, w_a = stats(rets_a, nav_a)
    ann_b, vol_b, sr_b, mdd_b, w_b = stats(rets_b, nav_b)
    ann_c, vol_c, sr_c, mdd_c, w_c = stats(rets_c, nav_c)

    print(f"\n{'='*75}")
    print(f"{'指标':<16} {'A.纯CS300':>12} {'B.评分卡×CS300':>14} {'C.风险平价行业':>14}")
    print(f"{'最终资产':16} {nav_a/1e4:>10.1f}万 {nav_b/1e4:>12.1f}万 {nav_c/1e4:>12.1f}万")
    print(f"{'累计收益':16} {(nav_a/INITIAL_CAPITAL-1)*100:>+10.1f}% {(nav_b/INITIAL_CAPITAL-1)*100:>+12.1f}% {(nav_c/INITIAL_CAPITAL-1)*100:>+12.1f}%")
    print(f"{'年化收益':16} {ann_a*100:>+10.1f}% {ann_b*100:>+12.1f}% {ann_c*100:>+12.1f}%")
    print(f"{'年化波动':16} {vol_a*100:>10.1f}% {vol_b*100:>12.1f}% {vol_c*100:>12.1f}%")
    print(f"{'夏普比率':16} {sr_a:>10.2f} {sr_b:>12.2f} {sr_c:>12.2f}")
    print(f"{'最大回撤':16} {-mdd_a*100:>+10.1f}% {-mdd_b*100:>+12.1f}% {-mdd_c*100:>+12.1f}%")
    print(f"{'正收益年数':16} {w_a}/{n:>8} {w_b}/{n:>11} {w_c}/{n:>11}")

    print(f"\n逐年超额：B-A(择时贡献)  C-A(择时+行业)  C-B(行业选择)")
    print(f"{'年份':>5} {'B-A':>8} {'C-A':>8} {'C-B':>8}")
    for r in rows:
        print(f"{r['year']:>5} {(r['ret_b']-r['ret_a'])*100:>+7.1f}% "
              f"{(r['ret_c']-r['ret_a'])*100:>+7.1f}% "
              f"{(r['ret_c']-r['ret_b'])*100:>+7.1f}%")

    _plot(rows, nav_a, nav_b, nav_c, top_n, ann_a, ann_b, ann_c, sr_a, sr_b, sr_c)


def _plot(rows, nav_a, nav_b, nav_c, top_n, ann_a, ann_b, ann_c, sr_a, sr_b, sr_c):
    plt.rcParams["font.family"] = ["STHeiti", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    years  = [r["year"] for r in rows]
    nav_as = [INITIAL_CAPITAL] + [r["nav_a"] for r in rows]
    nav_bs = [INITIAL_CAPITAL] + [r["nav_b"] for r in rows]
    nav_cs = [INITIAL_CAPITAL] + [r["nav_c"] for r in rows]
    xs     = [BACKTEST_YEARS[0] - 1] + years

    fig, axes = plt.subplots(3, 1, figsize=(13, 12),
                             gridspec_kw={"height_ratios": [3, 1, 1]})
    fig.suptitle(
        f"长周期回测 {BACKTEST_YEARS[0]}-{BACKTEST_YEARS[-1]}（初始100万，Top{top_n}行业·风险平价）",
        fontsize=13,
    )

    ax = axes[0]
    ax.plot(xs, [v / 1e4 for v in nav_as], "r--", linewidth=1.8,
            label=f"A. 纯CS300  年化{ann_a*100:+.1f}%  夏普{sr_a:.2f}")
    ax.plot(xs, [v / 1e4 for v in nav_bs], "b-",  linewidth=1.8,
            label=f"B. 评分卡×CS300  年化{ann_b*100:+.1f}%  夏普{sr_b:.2f}")
    ax.plot(xs, [v / 1e4 for v in nav_cs], "g-",  linewidth=2.2,
            label=f"C. 风险平价行业  年化{ann_c*100:+.1f}%  夏普{sr_c:.2f}")
    ax.set_ylabel("资产（万元）")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    ax.set_xticks(xs[::2])

    ax2 = axes[1]
    ba = [(r["ret_b"] - r["ret_a"]) * 100 for r in rows]
    ax2.bar(years, ba, color=["#2ca02c" if v > 0 else "#d62728" for v in ba], alpha=0.8)
    ax2.axhline(0, color="black", linewidth=0.5)
    ax2.set_ylabel("B-A 择时贡献(%)")
    ax2.set_xticks(years[::2])
    ax2.grid(axis="y", alpha=0.3)

    ax3 = axes[2]
    cb = [(r["ret_c"] - r["ret_b"]) * 100 for r in rows]
    ax3.bar(years, cb, color=["#1f77b4" if v > 0 else "#ff7f0e" for v in cb], alpha=0.8)
    ax3.axhline(0, color="black", linewidth=0.5)
    ax3.set_ylabel("C-B 行业贡献(%)")
    ax3.set_xticks(years[::2])
    ax3.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    out = ROOT / "docs" / "assets" / f"backtest_long_top{top_n}.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\n图表已保存: {out}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()
    run(args.top)


if __name__ == "__main__":
    main()
