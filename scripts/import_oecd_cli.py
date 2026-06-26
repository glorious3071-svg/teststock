#!/usr/bin/env python3.11
"""拉取 OECD CLI（Composite Leading Indicator）月频数据，入库 oecd_cli_monthly。

数据源：OECD SDMX REST v2 公开 API
  https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1/{REF_AREA}.M.LI...AA...H?...
  - 4.1 是当前 active vintage（4.0 已冻结，最新数据停在 2024-01）
  - 1955-01 起，月频，每经济体 770-857 行（含 2026-05 最新数据）
  - 字段：REF_AREA, TIME_PERIOD (YYYY-MM), OBS_VALUE, METHODOLOGY
  - 阈值 100 = 长期趋势线；> 100 扩张倾向、< 100 收缩倾向

经济体范围（评分卡 spec §六 行 178）：
  USA / G4E / CHN / JPN / G7  共 5 个

入库规则：
  - OBS_VALUE NaN / 缺失行跳过
  - 同 (ref_area, period) 重复保留 LAST
  - 幂等：ON DUPLICATE KEY UPDATE

用法：
  python3.11 scripts/import_oecd_cli.py [--dry-run] [--areas USA,CHN,...]
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

OECD_SDMX_BASE = (
    "https://sdmx.oecd.org/public/rest/data/"
    "OECD.SDD.STES,DSD_STES@DF_CLI,4.1"
)
# CLI 全量 filter：FREQ=M  MEASURE=LI  ADJUSTMENT=AA  METHODOLOGY=H（标准化幅度调整）
OECD_FILTER_TAIL = ".M.LI...AA...H"
HTTP_TIMEOUT_SECONDS = 90
REQUEST_SLEEP_SECONDS = 1.5  # 限速，避免 OECD 429
USER_AGENT = "Mozilla/5.0 (compatible; teststock-oecd-cli-fetcher/1.0)"
CSV_ACCEPT = "application/vnd.sdmx.data+csv"

# 评分卡 spec §六 行 178：USA / G4E / CHN / JPN / G7 五经济体
DEFAULT_AREAS = ["USA", "CHN", "JPN", "G4E", "G7"]


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


def fetch_oecd_cli_csv(ref_area: str) -> pd.DataFrame:
    """从 OECD SDMX 拉取单经济体 CLI 历史 CSV 并解析为 DataFrame。"""
    url = (
        f"{OECD_SDMX_BASE}/{ref_area}{OECD_FILTER_TAIL}"
        f"?dimensionAtObservation=AllDimensions"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": CSV_ACCEPT}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        raw = resp.read()
    return pd.read_csv(io.BytesIO(raw))


def normalize(raw: pd.DataFrame, ref_area: str) -> pd.DataFrame:
    """OECD SDMX 原始 → 标准列；过滤 OBS_VALUE 缺失；同 period 去重保留 LAST。"""
    df = raw.copy()
    # SDMX 列名固定大写：REF_AREA, TIME_PERIOD, OBS_VALUE, METHODOLOGY
    df["period"] = pd.to_datetime(df["TIME_PERIOD"]).dt.date
    df["cli_value"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    df["ref_area"] = ref_area  # 用入参覆盖（避免列大小写差异）
    df["methodology"] = df.get("METHODOLOGY", "H").fillna("H").astype(str)

    df = df.dropna(subset=["cli_value"])
    df = df.sort_values("period").drop_duplicates("period", keep="last")
    return df[["ref_area", "period", "cli_value", "methodology"]].reset_index(drop=True)


UPSERT_SQL = """
INSERT INTO oecd_cli_monthly (ref_area, period, cli_value, methodology)
VALUES (%s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    cli_value   = VALUES(cli_value),
    methodology = VALUES(methodology);
"""


def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if pd.isna(value):
        return None
    return value


def upsert(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    cleaned = [tuple(_clean(v) for v in row) for row in rows]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, cleaned)
    conn.commit()
    return len(cleaned)


def summary(df: pd.DataFrame, ref_area: str) -> None:
    if df.empty:
        print(f"  {ref_area:5} 0 条  (空数据)")
        return
    print(
        f"  {ref_area:5} {len(df):4} 条  "
        f"{df['period'].min()} ~ {df['period'].max()}  "
        f"cli: min={df['cli_value'].min():.2f} "
        f"max={df['cli_value'].max():.2f} "
        f"median={df['cli_value'].median():.2f}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印 summary，不写库",
    )
    parser.add_argument(
        "--areas", type=str, default=",".join(DEFAULT_AREAS),
        help=f"REF_AREA 逗号分隔，默认 {','.join(DEFAULT_AREAS)}",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    areas = [a.strip().upper() for a in args.areas.split(",") if a.strip()]
    if not areas:
        print("错误：--areas 至少需指定一个 REF_AREA", file=sys.stderr)
        return 1

    print(f"拉取 OECD CLI: {len(areas)} 个经济体 → {areas}")

    all_rows: list[tuple] = []
    failed_areas: list[str] = []

    for idx, ref_area in enumerate(areas):
        if idx > 0:
            time.sleep(REQUEST_SLEEP_SECONDS)
        try:
            raw = fetch_oecd_cli_csv(ref_area)
        except Exception as exc:
            print(f"  ✗ {ref_area} 拉取失败: {exc}", file=sys.stderr)
            failed_areas.append(ref_area)
            continue
        df = normalize(raw, ref_area)
        summary(df, ref_area)
        all_rows.extend(df.itertuples(index=False, name=None))

    if failed_areas:
        print(f"\n  ⚠ 失败经济体: {failed_areas}", file=sys.stderr)

    if not all_rows:
        print("\n所有经济体均拉取失败，未入库", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"\n[dry-run] 共 {len(all_rows)} 条，未入库")
        return 0

    conn = pymysql.connect(**mysql_config())
    try:
        n = upsert(conn, all_rows)
    finally:
        conn.close()
    print(f"\n入库完成：upsert {n} 条")
    return 0 if not failed_areas else 1


if __name__ == "__main__":
    sys.exit(main())
