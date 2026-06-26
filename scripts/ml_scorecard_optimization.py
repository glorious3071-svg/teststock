#!/usr/bin/env python3.11
"""ML 优化评分卡 — 用 18 年月度特征预测 CS300 次 N 月收益

X：每月末 23 个评分卡相关特征（数值 + 类别 one-hot）
Y：CS300 次 1/3/6/12 月累计收益率（%）

模型：Ridge / RandomForest / GradientBoosting
评估：TimeSeriesSplit 5 折 walk-forward；R²、IC(Spearman)、特征重要性

输出：
  - 训练/测试 IC 对比
  - 特征重要性排序
  - 与原评分卡的 IC 对比（看 ML 是否真的胜过手工规则）
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

from backtest.scorecard import ScorecardInputs, evaluate_scorecard

load_dotenv(ROOT / ".env")

OUT_CSV = ROOT / "data" / "ml_feature_dataset.csv"
RESULT_CSV = ROOT / "data" / "ml_model_results.csv"


def db():
    return pymysql.connect(host='127.0.0.1', user='teststock', password='teststock',
                            database='teststock', charset='utf8mb4')


def load_monthly_features(cur, snap: date) -> dict:
    """每月底所有评分卡相关原始特征"""
    snap_str = snap.strftime('%Y-%m-%d')
    snap_m = snap.strftime('%Y%m')
    one_year_ago = (snap - pd.Timedelta(days=365)).strftime('%Y-%m-%d')

    f = {'snapshot': snap_str}

    # 估值
    cur.execute("SELECT pe_ttm, pb FROM index_dailybasic WHERE ts_code='000300.SH' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    f['pe'], f['pb'] = (float(r[0]), float(r[1])) if r else (np.nan, np.nan)

    # 流动性
    cur.execute("SELECT rate_3m, rate_1y FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    shibor_3m_now = float(r[0]) if r else np.nan
    f['shibor_3m'] = shibor_3m_now
    f['shibor_1y'] = float(r[1]) if r else np.nan
    cur.execute("SELECT rate_3m FROM shibor_daily WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (one_year_ago,))
    r = cur.fetchone()
    f['rate_bp_12m'] = (shibor_3m_now - float(r[0])) * 100 if r else np.nan

    cur.execute("""SELECT COALESCE(SUM(rrr_change_pp), 0) FROM cn_rrr_changes
                   WHERE effective_date > %s AND effective_date <= %s
                     AND inst_type IN ('large','all')""", (one_year_ago, snap_str))
    f['rrr_pp_12m'] = float(cur.fetchone()[0])

    # 基本面 PMI
    cur.execute("SELECT pmi_mfg, pmi_production, pmi_new_order, pmi_non_mfg FROM cn_pmi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 6",
                (snap_m,))
    pmis = cur.fetchall()
    f['pmi_mfg'] = float(pmis[0][0]) if pmis else np.nan
    f['pmi_non_mfg'] = float(pmis[0][3]) if pmis and pmis[0][3] is not None else np.nan
    f['pmi_prod_minus_order'] = (float(pmis[0][1]) - float(pmis[0][2])) if pmis and pmis[0][1] is not None and pmis[0][2] is not None else np.nan
    if len(pmis) >= 3:
        f['pmi_3m_avg'] = sum(float(p[0]) for p in pmis[:3]) / 3
    else:
        f['pmi_3m_avg'] = np.nan
    f['pmi_resume_expansion'] = 1 if (len(pmis) >= 2 and pmis[1][0] is not None and pmis[0][0] is not None
                                       and float(pmis[1][0]) < 50 <= float(pmis[0][0])) else 0
    # pmi_below_52_consecutive
    consec = 0
    cur.execute("SELECT pmi_mfg FROM cn_pmi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 24", (snap_m,))
    for v, in cur.fetchall():
        if v is not None and float(v) < 52:
            consec += 1
        else:
            break
    f['pmi_below_52_months'] = consec

    # PPI
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (snap_m,))
    r = cur.fetchone()
    f['ppi_yoy'] = float(r[0]) if r and r[0] is not None else np.nan
    cur.execute("SELECT ppi_yoy FROM cn_ppi_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1",
                (f'{int(snap_m[:4])-1}{snap_m[4:]}',))
    r = cur.fetchone()
    prior_ppi = float(r[0]) if r and r[0] is not None else np.nan
    if pd.notna(prior_ppi) and pd.notna(f['ppi_yoy']):
        f['ppi_change_diff'] = f['ppi_yoy'] - prior_ppi  # 数值化
        f['ppi_turn_negative'] = 1 if (prior_ppi >= 0 and f['ppi_yoy'] < 0) else 0
        f['ppi_turn_positive'] = 1 if (prior_ppi < 0 and f['ppi_yoy'] >= 0) else 0
    else:
        f['ppi_change_diff'] = np.nan
        f['ppi_turn_negative'] = 0
        f['ppi_turn_positive'] = 0

    # 情绪
    cur.execute("SELECT new_fund_billion, new_fund_count FROM cn_fund_new_monthly WHERE month <= %s ORDER BY month DESC LIMIT 1", (snap_m,))
    r = cur.fetchone()
    f['new_fund_billion'] = float(r[0]) if r else np.nan
    f['new_fund_count'] = float(r[1]) if r else np.nan

    cur.execute("SELECT SUM(rzrqye) FROM margin_daily WHERE trade_date <= %s GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    mc = float(r[0]) if r and r[0] is not None else np.nan
    cur.execute("SELECT SUM(rzrqye) FROM margin_daily WHERE trade_date <= %s GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1", (one_year_ago,))
    r = cur.fetchone()
    mp = float(r[0]) if r and r[0] is not None else np.nan
    f['margin_yoy'] = (mc / mp - 1) * 100 if pd.notna(mc) and pd.notna(mp) and mp > 0 else np.nan
    f['margin_log'] = np.log(mc) if pd.notna(mc) and mc > 0 else np.nan

    # 外部
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    spx_cur = float(r[0]) if r and r[0] is not None else np.nan
    cur.execute("SELECT close FROM us_index_daily WHERE ts_code='SPX.US' AND trade_date <= %s ORDER BY trade_date DESC LIMIT 1",
                ((snap - pd.offsets.MonthBegin(1) - pd.Timedelta(days=1)).strftime('%Y-%m-%d'),))
    r = cur.fetchone()
    spx_prior = float(r[0]) if r and r[0] is not None else np.nan
    f['us_monthly_pct'] = (spx_cur / spx_prior - 1) * 100 if pd.notna(spx_cur) and pd.notna(spx_prior) and spx_prior > 0 else np.nan

    cur.execute("SELECT rate_after_pct FROM global_cb_rate_events WHERE cb_code='FED' AND effective_date <= %s ORDER BY effective_date DESC LIMIT 1", (snap_str,))
    r = cur.fetchone()
    f['fed_rate'] = float(r[0]) if r and r[0] is not None else np.nan

    # global_recession 投票
    snap_month_start = snap.replace(day=1).strftime('%Y-%m-%d')
    votes = 0
    for c in ('USA', 'G4E', 'CHN', 'JPN', 'G7'):
        cur.execute("SELECT cli_value FROM oecd_cli_monthly WHERE ref_area=%s AND period <= %s ORDER BY period DESC LIMIT 3",
                    (c, snap_month_start))
        vals = [float(v[0]) for v in cur.fetchall() if v[0] is not None]
        if len(vals) >= 3 and vals[0] < 100 and vals[0] < vals[1] < vals[2]:
            votes += 1
    f['recession_votes'] = votes

    six_mo_ago = (snap - pd.Timedelta(days=180)).strftime('%Y-%m-%d')
    cur.execute("""SELECT COUNT(DISTINCT cb_code) FROM global_cb_rate_events
                   WHERE direction='cut' AND effective_date BETWEEN %s AND %s
                     AND cb_code IN ('FED','ECB','BOE','BOJ','PBOC')""", (six_mo_ago, snap_str))
    f['cb_cuts_6m'] = int(cur.fetchone()[0])

    # 政策（类别 → 离散）
    snap_year = snap.year
    cewc_apply = snap_year + 1 if snap.month == 12 else snap_year
    cur.execute("SELECT fiscal_policy, monetary_policy FROM cewc_annual WHERE apply_year=%s", (cewc_apply,))
    r = cur.fetchone()
    if r:
        fiscal, monetary = r[0] or '', r[1] or ''
        f['cewc_loose'] = 1 if '宽松' in monetary else 0
        f['cewc_tight'] = 1 if '紧' in monetary else 0
        f['cewc_expansionary'] = 1 if '积极' in fiscal else 0
    else:
        f['cewc_loose'] = f['cewc_tight'] = f['cewc_expansionary'] = 0

    cur.execute("""SELECT direction FROM stamp_duty_events WHERE event_type='stamp_duty' AND effective_date <= %s
                   AND effective_date >= %s
                   ORDER BY effective_date DESC LIMIT 1""", (snap_str, (snap - pd.Timedelta(days=365)).strftime('%Y-%m-%d')))
    r = cur.fetchone()
    f['stamp_duty_loosen_1y'] = 1 if r and r[0] == 'loosen' else 0
    f['stamp_duty_tighten_1y'] = 1 if r and r[0] == 'tighten' else 0

    cur.execute("""SELECT direction FROM national_team_actions WHERE effective_date <= %s
                   AND effective_date >= %s
                   ORDER BY effective_date DESC LIMIT 1""", (snap_str, (snap - pd.Timedelta(days=365)).strftime('%Y-%m-%d')))
    r = cur.fetchone()
    f['national_entry_1y'] = 1 if r and r[0] == 'entry' else 0

    return f


def build_dataset():
    """生成月度特征 + 次 N 月收益"""
    conn = db()
    cur = conn.cursor()

    # 月末日期序列
    month_ends = []
    for y in range(2008, 2026):
        for m in range(1, 13):
            if m == 12:
                nx = date(y + 1, 1, 1)
            else:
                nx = date(y, m + 1, 1)
            me = nx - pd.Timedelta(days=1)
            month_ends.append(me.date() if hasattr(me, 'date') else me)

    print(f'生成 {len(month_ends)} 月特征...')
    rows = []
    for i, me in enumerate(month_ends, 1):
        try:
            rows.append(load_monthly_features(cur, me))
        except Exception as e:
            print(f'  {me}: ERR {e}')
        if i % 36 == 0:
            print(f'  {i}/{len(month_ends)}')

    df = pd.DataFrame(rows)
    df['snapshot'] = pd.to_datetime(df['snapshot'])

    # 加入 Y: 次 N 月 CS300 收益
    cs = pd.read_sql(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    cs['close'] = cs['close'].astype(float)
    cs_monthly = cs['close'].resample('ME').last()
    cs_monthly.index = cs_monthly.index.to_period('M').to_timestamp('M')

    for n in (1, 3, 6, 12):
        cs_ret = cs_monthly.pct_change(n).shift(-n) * 100  # 次 N 月累计收益
        cs_ret_df = cs_ret.reset_index()
        cs_ret_df.columns = ['snapshot', f'ret_{n}m']
        # 对齐
        df['snapshot_me'] = df['snapshot'].dt.to_period('M').dt.to_timestamp('M')
        df = df.merge(cs_ret_df.rename(columns={'snapshot': 'snapshot_me'}), on='snapshot_me', how='left')
    df = df.drop(columns=['snapshot_me'])
    conn.close()
    df.to_csv(OUT_CSV, index=False)
    print(f'\n保存 {len(df)} 月 × {len(df.columns)} 列 → {OUT_CSV}')
    return df


def spearman(a, b):
    a = pd.Series(a).rank()
    b = pd.Series(b).rank()
    return a.corr(b)


def evaluate_models(df: pd.DataFrame, target_col: str):
    """walk-forward CV，对比 Ridge / RF / GBM 与「原评分卡」"""
    feat_cols = [c for c in df.columns
                 if c not in ('snapshot', 'ret_1m', 'ret_3m', 'ret_6m', 'ret_12m')]
    sub = df.dropna(subset=feat_cols + [target_col]).sort_values('snapshot').reset_index(drop=True)
    X = sub[feat_cols].values
    y = sub[target_col].values
    print(f'  特征数 {len(feat_cols)}, 样本数 {len(sub)}')

    models = {
        'Ridge α=1':       Ridge(alpha=1.0),
        'Ridge α=10':      Ridge(alpha=10.0),
        'RandomForest':    RandomForestRegressor(n_estimators=200, max_depth=4, random_state=42, n_jobs=-1),
        'GBM':             GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42),
    }

    tscv = TimeSeriesSplit(n_splits=5)
    results = {}
    for name, model in models.items():
        oof_pred = np.zeros(len(sub)) * np.nan
        oof_true = np.zeros(len(sub)) * np.nan
        for fold, (tr, te) in enumerate(tscv.split(X)):
            scaler = StandardScaler()
            Xtr = scaler.fit_transform(X[tr])
            Xte = scaler.transform(X[te])
            model.fit(Xtr, y[tr])
            oof_pred[te] = model.predict(Xte)
            oof_true[te] = y[te]
        mask = ~np.isnan(oof_pred)
        ic = spearman(oof_pred[mask], oof_true[mask])
        # 方向命中率
        hits = sum(1 for p, t in zip(oof_pred[mask], oof_true[mask])
                   if (p > 0 and t > 0) or (p < 0 and t < 0))
        hit_rate = hits / mask.sum() * 100
        results[name] = {'IC': ic, 'hit_rate': hit_rate, 'n': mask.sum()}

    return results, feat_cols, models


def feature_importance_rf(df: pd.DataFrame, target_col: str):
    """对全数据训练一次 RF，看特征重要性"""
    feat_cols = [c for c in df.columns
                 if c not in ('snapshot', 'ret_1m', 'ret_3m', 'ret_6m', 'ret_12m')]
    sub = df.dropna(subset=feat_cols + [target_col])
    X = sub[feat_cols].values
    y = sub[target_col].values
    rf = RandomForestRegressor(n_estimators=300, max_depth=5, random_state=42, n_jobs=-1)
    rf.fit(StandardScaler().fit_transform(X), y)
    imp = sorted(zip(feat_cols, rf.feature_importances_), key=lambda x: -x[1])
    return imp


def main():
    if OUT_CSV.exists():
        df = pd.read_csv(OUT_CSV)
        df['snapshot'] = pd.to_datetime(df['snapshot'])
        print(f'读缓存：{len(df)} 月')
    else:
        df = build_dataset()

    print('\n=== 各模型 IC + 命中率（walk-forward 5 折 CV）===')
    print(f'{"target":<10}{"模型":<22}{"IC":>9}{"命中率":>9}{"n":>6}')
    print('-' * 60)
    summary = []
    for target in ('ret_1m', 'ret_3m', 'ret_6m', 'ret_12m'):
        results, _, _ = evaluate_models(df, target)
        # 同时算「原评分卡」IC（用 monthly_scorecard_series.csv 的 total_score）
        score_df = pd.read_csv(ROOT / 'data' / 'monthly_scorecard_series.csv')
        score_df['snapshot'] = pd.to_datetime(score_df['snapshot'])
        score_df['snapshot_me'] = score_df['snapshot'].dt.to_period('M').dt.to_timestamp('M')
        merged = df.copy()
        merged['snapshot_me'] = merged['snapshot'].dt.to_period('M').dt.to_timestamp('M')
        merged = merged.merge(score_df[['snapshot_me', 'total_score']], on='snapshot_me', how='left')
        m = merged.dropna(subset=['total_score', target])
        score_ic = spearman(-m['total_score'].values, m[target].values)  # 评分越低越加仓，所以取负
        score_hit = sum(1 for s, t in zip(-m['total_score'], m[target])
                        if (s > 0 and t > 0) or (s < 0 and t < 0)) / len(m) * 100
        print(f"{target:<10}{'原评分卡 (-total_score)':<22}{score_ic:>+9.3f}{score_hit:>8.0f}%{len(m):>6}")
        for name, r in results.items():
            print(f"{target:<10}{name:<22}{r['IC']:>+9.3f}{r['hit_rate']:>8.0f}%{r['n']:>6}")
            summary.append({'target': target, 'model': name, 'IC': r['IC'], 'hit_rate': r['hit_rate'], 'n': r['n']})
        print()

    pd.DataFrame(summary).to_csv(RESULT_CSV, index=False)

    print('\n=== RF 特征重要性 (target=ret_3m, top 20) ===')
    imp = feature_importance_rf(df, 'ret_3m')
    for name, score in imp[:20]:
        bar = '█' * int(score * 100)
        print(f'  {name:<28} {score:.4f}  {bar}')


if __name__ == '__main__':
    main()
