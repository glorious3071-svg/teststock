#!/usr/bin/env python3.11
"""import_enterprise_boom.py — 企业景气/信心指数落库

数据源：akshare macro_china_enterprise_boom_index（国家统计局企业景气调查）
目标表：cn_enterprise_boom_quarterly（季度，2005Q1起）
"""

from __future__ import annotations

import os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv(ROOT / '.env')

SCHEMA_FILE = Path(__file__).resolve().parent.parent / 'sql' / 'cn_enterprise_boom_quarterly_schema.sql'


def mysql_config() -> dict:
    return {
        'host': os.getenv('MYSQL_HOST', '127.0.0.1'),
        'port': int(os.getenv('MYSQL_PORT', '3306')),
        'user': os.getenv('MYSQL_USER', 'teststock'),
        'password': os.getenv('MYSQL_PASSWORD', 'teststock'),
        'database': os.getenv('MYSQL_DATABASE', 'teststock'),
        'charset': 'utf8mb4',
    }


def parse_quarter(q: str):
    """'2024年第2季度' → (date(2024-06-30), 2024, 2)"""
    m = re.match(r'(\d{4})年第(\d)季度', str(q))
    if not m:
        return None, None, None
    y, qt = int(m.group(1)), int(m.group(2))
    end_month = qt * 3
    import calendar
    last_day = calendar.monthrange(y, end_month)[1]
    from datetime import date
    return date(y, end_month, last_day), y, qt


def to_float(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def fetch_from_akshare() -> pd.DataFrame:
    import akshare as ak
    raw = ak.macro_china_enterprise_boom_index()
    rows = []
    for _, r in raw.iterrows():
        q_date, y, qt = parse_quarter(r.get('季度', ''))
        if q_date is None:
            continue
        rows.append({
            'quarter_str': str(r.get('季度', '')),
            'quarter_date': q_date,
            'cal_year': y,
            'cal_quarter': qt,
            'boom_index': to_float(r.get('企业景气指数-指数')),
            'boom_yoy': to_float(r.get('企业景气指数-同比')),
            'boom_qoq': to_float(r.get('企业景气指数-环比')),
            'confidence_index': to_float(r.get('企业家信心指数-指数')),
            'confidence_yoy': to_float(r.get('企业家信心指数-同比')),
            'confidence_qoq': to_float(r.get('企业家信心指数-环比')),
        })
    df = pd.DataFrame(rows)
    df['quarter_date'] = pd.to_datetime(df['quarter_date'])
    return df.sort_values('quarter_date').reset_index(drop=True)


def apply_schema(conn: pymysql.connections.Connection) -> None:
    sql = SCHEMA_FILE.read_text(encoding='utf-8')
    with conn.cursor() as cur:
        for stmt in sql.split(';'):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def upsert(conn: pymysql.connections.Connection, df: pd.DataFrame) -> int:
    sql = """
        INSERT INTO cn_enterprise_boom_quarterly
            (quarter_str, quarter_date, cal_year, cal_quarter,
             boom_index, boom_yoy, boom_qoq,
             confidence_index, confidence_yoy, confidence_qoq)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            quarter_str       = VALUES(quarter_str),
            boom_index        = VALUES(boom_index),
            boom_yoy          = VALUES(boom_yoy),
            boom_qoq          = VALUES(boom_qoq),
            confidence_index  = VALUES(confidence_index),
            confidence_yoy    = VALUES(confidence_yoy),
            confidence_qoq    = VALUES(confidence_qoq)
    """
    rows = [
        (r.quarter_str, r.quarter_date.strftime('%Y-%m-%d') if hasattr(r.quarter_date, 'strftime') else str(r.quarter_date),
         int(r.cal_year), int(r.cal_quarter),
         (None if (r.boom_index is None or (isinstance(r.boom_index, float) and pd.isna(r.boom_index))) else float(r.boom_index)),
         (None if (r.boom_yoy is None or (isinstance(r.boom_yoy, float) and pd.isna(r.boom_yoy))) else float(r.boom_yoy)),
         (None if (r.boom_qoq is None or (isinstance(r.boom_qoq, float) and pd.isna(r.boom_qoq))) else float(r.boom_qoq)),
         (None if (r.confidence_index is None or (isinstance(r.confidence_index, float) and pd.isna(r.confidence_index))) else float(r.confidence_index)),
         (None if (r.confidence_yoy is None or (isinstance(r.confidence_yoy, float) and pd.isna(r.confidence_yoy))) else float(r.confidence_yoy)),
         (None if (r.confidence_qoq is None or (isinstance(r.confidence_qoq, float) and pd.isna(r.confidence_qoq))) else float(r.confidence_qoq)))
        for r in df.itertuples(index=False)
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def validate(conn: pymysql.connections.Connection) -> dict:
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*), MIN(quarter_date), MAX(quarter_date) FROM cn_enterprise_boom_quarterly')
    n, mn, mx = cur.fetchone()
    cur.execute('SELECT COUNT(*) FROM cn_enterprise_boom_quarterly WHERE boom_index IS NULL')
    null_boom = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM cn_enterprise_boom_quarterly WHERE confidence_index IS NULL')
    null_conf = cur.fetchone()[0]
    # 检查季度连续性
    cur.execute('SELECT cal_year, cal_quarter FROM cn_enterprise_boom_quarterly ORDER BY quarter_date')
    quarters = cur.fetchall()
    gaps = []
    for i in range(1, len(quarters)):
        py, pq = quarters[i-1]
        cy, cq = quarters[i]
        # 相邻两行期望差1季度
        expected_gap = (cy * 4 + cq) - (py * 4 + pq)
        if expected_gap != 1:
            gaps.append((f'{py}Q{pq}', f'{cy}Q{cq}', expected_gap))
    return {
        'total_rows': n,
        'date_range': f'{mn} ~ {mx}',
        'null_boom': null_boom,
        'null_conf': null_conf,
        'gaps': gaps,
    }


def main() -> None:
    conn = pymysql.connect(**mysql_config())
    try:
        print('建表（如不存在）...')
        apply_schema(conn)

        print('从 akshare 拉取企业景气/信心指数...')
        df = fetch_from_akshare()
        print(f'  拉取 {len(df)} 季度  ({df["quarter_date"].min().date()} ~ {df["quarter_date"].max().date()})')

        print('Upsert 入库...')
        n = upsert(conn, df)
        print(f'  写入 {n} 行')

        print('\n数据完整性验证...')
        v = validate(conn)
        print(f'  总行数: {v["total_rows"]}')
        print(f'  时间范围: {v["date_range"]}')
        print(f'  boom_index 缺失: {v["null_boom"]} 行')
        print(f'  confidence_index 缺失: {v["null_conf"]} 行')
        if v['gaps']:
            print(f'  ⚠ 季度不连续: {v["gaps"]}')
        else:
            print(f'  ✓ 季度连续性：无缺口')

        print('\n完成。')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
