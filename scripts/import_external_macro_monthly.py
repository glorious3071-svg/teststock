#!/usr/bin/env python3.11
"""把外部宏观月度衍生特征 (vix/gold/fed/us10y/spx) 落库到 external_macro_monthly

逐月从原始日频表聚合:
  - vix: cboe_vix_daily 月均 / 最大 / 最小
  - gold: gold_daily (GC.FOREIGN) 月末 close + 12 月 YoY
  - fed: global_cb_rate_events (cb_code='FED') 月末持有的 rate
  - us10y: us_tycr_daily 月末 y10
  - spx: us_index_daily (SPX.US) 月末 close + 12 月 YoY

触发标记:
  - gold_yoy > 25 → trig_gold_yoy25
  - vix_30d_avg > 30 → trig_vix_30plus
  - fed_rate >= 4.5 → trig_fed_45plus
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "sql" / "external_macro_monthly_schema.sql"


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


def apply_schema(conn: pymysql.connections.Connection) -> None:
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def build_monthly(conn: pymysql.connections.Connection) -> pd.DataFrame:
    # VIX
    vix = pd.read_sql(
        "SELECT trade_date, close FROM cboe_vix_daily ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    vix['close'] = vix['close'].astype(float)
    vix_m_avg = vix['close'].resample('ME').mean()
    vix_m_max = vix['close'].resample('ME').max()
    vix_m_min = vix['close'].resample('ME').min()

    # Gold
    gold = pd.read_sql(
        "SELECT trade_date, close FROM gold_daily WHERE symbol='GC.FOREIGN' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    gold['close'] = gold['close'].astype(float)
    gold_m = gold['close'].resample('ME').last()
    gold_yoy = gold_m.pct_change(12) * 100

    # FED rate
    fed = pd.read_sql(
        "SELECT effective_date AS dt, rate_after_pct FROM global_cb_rate_events "
        "WHERE cb_code='FED' ORDER BY effective_date",
        conn, parse_dates=['dt'], index_col='dt',
    )
    fed['rate_after_pct'] = fed['rate_after_pct'].astype(float)
    fed_m = fed['rate_after_pct'].resample('ME').last().ffill()

    # US 10Y
    us10y = pd.read_sql(
        "SELECT trade_date, y10 FROM us_tycr_daily WHERE y10 IS NOT NULL ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    us10y['y10'] = us10y['y10'].astype(float)
    us10y_m = us10y['y10'].resample('ME').last()

    # SPX
    spx = pd.read_sql(
        "SELECT trade_date, close FROM us_index_daily WHERE ts_code='SPX.US' ORDER BY trade_date",
        conn, parse_dates=['trade_date'], index_col='trade_date',
    )
    spx['close'] = spx['close'].astype(float)
    spx_m = spx['close'].resample('ME').last()
    spx_yoy = spx_m.pct_change(12) * 100

    # 合并到统一月末索引（取所有源的最大公共范围）
    idx_start = max(vix_m_avg.index.min(), pd.Timestamp('1990-01-31'))
    idx_end = min(vix_m_avg.index.max(), gold_m.index.max() if not gold_m.empty else vix_m_avg.index.max(),
                   us10y_m.index.max(), spx_m.index.max())
    full_idx = pd.date_range(idx_start, idx_end, freq='ME')

    df = pd.DataFrame(index=full_idx)
    df['vix_30d_avg'] = vix_m_avg.reindex(full_idx).round(2)
    df['vix_30d_max'] = vix_m_max.reindex(full_idx).round(2)
    df['vix_30d_min'] = vix_m_min.reindex(full_idx).round(2)
    df['gold_close'] = gold_m.reindex(full_idx).round(2)
    df['gold_yoy_pct'] = gold_yoy.reindex(full_idx).round(2)
    df['fed_rate_level'] = fed_m.reindex(full_idx).round(2)
    df['us10y_yield'] = us10y_m.reindex(full_idx).round(2)
    df['spx_close'] = spx_m.reindex(full_idx).round(2)
    df['spx_yoy_pct'] = spx_yoy.reindex(full_idx).round(2)

    # 触发标记
    df['trig_gold_yoy25'] = (df['gold_yoy_pct'] > 25).fillna(False).astype(int)
    df['trig_vix_30plus'] = (df['vix_30d_avg'] > 30).fillna(False).astype(int)
    df['trig_fed_45plus'] = (df['fed_rate_level'] >= 4.5).fillna(False).astype(int)

    # month / year / month_num
    df['month'] = df.index.strftime('%Y%m')
    df['cal_year'] = df.index.year
    df['cal_month'] = df.index.month
    return df.reset_index(drop=True)


def upsert(conn: pymysql.connections.Connection, df: pd.DataFrame) -> int:
    cols = ['month', 'cal_year', 'cal_month',
            'vix_30d_avg', 'vix_30d_max', 'vix_30d_min',
            'gold_close', 'gold_yoy_pct', 'fed_rate_level',
            'us10y_yield', 'spx_close', 'spx_yoy_pct',
            'trig_gold_yoy25', 'trig_vix_30plus', 'trig_fed_45plus']
    placeholders = ','.join(['%s'] * len(cols))
    update_cols = [c for c in cols if c not in ('month', 'cal_year', 'cal_month')]
    update_clause = ', '.join(f'{c}=VALUES({c})' for c in update_cols)
    sql = f"""
        INSERT INTO external_macro_monthly ({','.join(cols)})
        VALUES ({placeholders})
        ON DUPLICATE KEY UPDATE {update_clause}
    """

    def nullify(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return v

    rows = []
    for r in df.itertuples(index=False):
        row = []
        for c in cols:
            v = getattr(r, c)
            if c.startswith('trig_'):
                row.append(int(v) if pd.notna(v) else 0)
            else:
                row.append(nullify(v))
        rows.append(tuple(row))

    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import external macro monthly features")
    parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        print("Apply schema...")
        apply_schema(conn)
        print("Build monthly features from raw tables...")
        df = build_monthly(conn)
        print(f"  生成 {len(df)} 行月度数据，时间范围 {df['month'].min()} ~ {df['month'].max()}")
        # 写 CSV 备份
        out_csv = DATA_DIR / "external_macro_monthly.csv"
        df.to_csv(out_csv, index=False)
        print(f"  CSV 备份: {out_csv}")
        # Upsert
        n = upsert(conn, df)
        print(f"  Upserted external_macro_monthly: {n} rows")

        # 校验
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MIN(month), MAX(month), SUM(trig_gold_yoy25), SUM(trig_vix_30plus), SUM(trig_fed_45plus) FROM external_macro_monthly")
            n, mn, mx, g, v, f = cur.fetchone()
            print(f'\n入库校验:')
            print(f'  共 {n} 月，范围 {mn} ~ {mx}')
            print(f'  trig_gold_yoy25: {g} 月触发')
            print(f'  trig_vix_30plus: {v} 月触发')
            print(f'  trig_fed_45plus: {f} 月触发')
    finally:
        conn.close()


if __name__ == "__main__":
    main()
