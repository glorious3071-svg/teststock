#!/usr/bin/env python3
"""用 LLM 为每个申万指数打上关联题材标签。

逻辑：每个 .SI 指数 → 关联的 canonical theme 列表 + 强弱。
保证每个指数至少有一个 theme，不存在孤立指数。
分批调用 LLM（每批 15 个指数），用管道分隔格式避免 JSON 截断。

Usage:
  python scripts/build_theme_index_map.py
  python scripts/build_theme_index_map.py --dry-run
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from agents.annual_direction.llm_client import LLMError, chat

CANONICAL_THEMES = [
    "农业/三农",
    "节能环保/绿色低碳",
    "科技创新/自主创新",
    "基建/城镇化",
    "消费/内需",
    "汽车/交通装备",
    "新能源/光伏储能",
    "半导体/数字经济",
    "医药/医疗健康",
    "金融/资本市场",
    "房地产/城投化债",
    "军工/国防",
    "煤炭/钢铁/资源品",
    "对外开放/出海",
    "民营经济",
    "先进制造/产业升级",
    "人工智能/大数据",
]

BATCH_SIZE = 15   # 每批指数数量
LLM_SLEEP  = 1.0  # 批次间等待

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS theme_index_map (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    ts_code     VARCHAR(20)   NOT NULL COMMENT '申万指数代码',
    index_name  VARCHAR(100)  NOT NULL COMMENT '指数名称',
    theme       VARCHAR(50)   NOT NULL COMMENT '关联题材',
    relevance   ENUM('强','中','弱') NOT NULL COMMENT '关联强度',
    reason      VARCHAR(200)  NULL     COMMENT 'LLM 理由',
    created_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_code_theme (ts_code, theme),
    KEY idx_theme (theme),
    KEY idx_ts_code (ts_code),
    KEY idx_relevance (theme, relevance)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='申万指数与投资题材关联映射（每个指数至少一个题材）';
"""

_THEME_LIST = "\n".join(f"  {t}" for t in CANONICAL_THEMES)

SYSTEM_PROMPT = f"""你是 A 股指数分析专家，熟悉申万行业分类体系及每个指数的成份股构成。

任务：为每个申万指数打上它所关联的投资主题标签，并标注关联强度。

【可用主题列表（必须一字不差使用）】
{_THEME_LIST}

规则：
- 每个指数必须至少关联 1 个主题，不允许出现孤立指数
- 一个指数可关联多个主题（如"申万电子"可同时关联"科技创新/自主创新"和"半导体/数字经济"）
- 关联强度：强=成份股直接受该主题驱动；中=部分受益；弱=边际相关
- reason 不超过 20 字

输出格式：每行一条映射，竖线分隔，无表头，无其他文字：
ts_code|theme|relevance|reason
例：
801010.SI|农业/三农|强|农林牧渔是三农政策核心受益行业
801010.SI|消费/内需|弱|农产品消费属内需链条
801011.SI|农业/三农|强|林业属大农业范畴"""


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def load_indices(conn, suffix: str) -> list[tuple[str, str]]:
    """返回指定后缀的 (ts_code, index_name) 列表。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ts_code FROM index_daily WHERE ts_code LIKE %s ORDER BY ts_code",
            (f"%.{suffix}",),
        )
        codes = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT ts_code, indx_csname FROM etf_benchmark_index")
        ebi = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT index_ts_code, index_name FROM passive_etf WHERE index_ts_code IS NOT NULL")
        petf = {r[0]: r[1] for r in cur.fetchall()}
    return [(code, ebi.get(code) or petf.get(code) or code) for code in codes]


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def build_user_prompt(indices: list[tuple[str, str]]) -> str:
    index_block = "\n".join(f"{code}  {name}" for code, name in indices)
    return f"请为以下 {len(indices)} 个申万指数打标签：\n{index_block}"


def parse_pipe_rows(text: str, valid_codes: set[str], theme_set: set[str]) -> list[tuple]:
    """解析管道分隔格式，返回 (ts_code, theme, relevance, reason) 列表。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("ts_code") or line.startswith("#") or line.startswith("例"):
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        code, theme, relevance = parts[0].strip(), parts[1].strip(), parts[2].strip()
        reason = parts[3].strip()[:200] if len(parts) > 3 else ""
        if code not in valid_codes:
            continue
        if theme not in theme_set:
            continue
        if relevance not in ("强", "中", "弱"):
            continue
        rows.append((code, theme, relevance, reason))
    return rows


def upsert_rows(conn, rows: list[tuple], name_map: dict[str, str]) -> int:
    sql = """
        INSERT INTO theme_index_map (ts_code, index_name, theme, relevance, reason)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            relevance  = VALUES(relevance),
            reason     = VALUES(reason),
            updated_at = CURRENT_TIMESTAMP
    """
    data = [(code, name_map.get(code, code)[:100], theme, rel, reason)
            for code, theme, rel, reason in rows]
    if not data:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, data)
    conn.commit()
    return len(data)


def main() -> int:
    import argparse
    from collections import defaultdict

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--suffix", default="all", help="SI / CSI / SH / SZ / all")
    args = parser.parse_args()

    conn = pymysql.connect(**mysql_config())
    try:
        if not args.dry_run:
            ensure_table(conn)

        # 加载指定后缀的指数，跳过已在表中的
        suffixes = ["SI", "CSI", "SH", "SZ"] if args.suffix == "all" else [args.suffix.upper()]
        all_indices: list[tuple[str, str]] = []
        for sfx in suffixes:
            all_indices.extend(load_indices(conn, sfx))

        # 只处理尚未在 theme_index_map 中的指数
        if not args.dry_run:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT ts_code FROM theme_index_map")
                already_done = {r[0] for r in cur.fetchall()}
            indices = [(c, n) for c, n in all_indices if c not in already_done]
            print(f"共 {len(all_indices)} 个指数，已映射 {len(already_done)} 个，待处理 {len(indices)} 个")
        else:
            indices = all_indices
            print(f"共 {len(indices)} 个指数（dry-run 全量）")

        if not indices:
            print("无需处理的指数，退出")
            return 0

        valid_codes = {c for c, _ in indices}
        name_map = {c: n for c, n in indices}
        theme_set = set(CANONICAL_THEMES)
        batches = [indices[i:i+BATCH_SIZE] for i in range(0, len(indices), BATCH_SIZE)]
        print(f"标准题材: {len(CANONICAL_THEMES)} 个，分 {len(batches)} 批（每批 {BATCH_SIZE}）\n")

        all_rows: list[tuple] = []

        for bi, batch in enumerate(batches, 1):
            print(f"[{bi}/{len(batches)}] {batch[0][0]}~{batch[-1][0]}  ", end="", flush=True)
            try:
                response = chat([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": build_user_prompt(batch)},
                ])
            except LLMError as e:
                print(f"FAIL: {e}")
                continue

            batch_codes = {c for c, _ in batch}
            rows = parse_pipe_rows(response, batch_codes, theme_set)

            # 检查孤立指数（该批内没有任何映射的指数）
            covered = {r[0] for r in rows}
            orphans = batch_codes - covered
            print(f"{len(rows)} 条关联，孤立={len(orphans)}")
            if orphans:
                print(f"  孤立指数: {sorted(orphans)}")

            all_rows.extend(rows)
            time.sleep(LLM_SLEEP)

        # 汇总统计
        covered_codes = {r[0] for r in all_rows}
        uncovered = valid_codes - covered_codes
        print(f"\n=== 汇总 ===")
        print(f"覆盖指数: {len(covered_codes)}/{len(indices)}，未覆盖: {len(uncovered)}")
        print(f"总关联关系: {len(all_rows)}")

        by_theme: dict[str, dict] = defaultdict(lambda: {"强": 0, "中": 0, "弱": 0})
        for _, theme, rel, _ in all_rows:
            by_theme[theme][rel] += 1

        print("\n各题材覆盖指数数:")
        for theme in CANONICAL_THEMES:
            cnt = by_theme.get(theme, {})
            total = sum(cnt.values())
            bar = "█" * cnt.get("强", 0) + "▒" * cnt.get("中", 0) + "░" * cnt.get("弱", 0)
            print(f"  {theme:<18} 强={cnt.get('强',0):2d} 中={cnt.get('中',0):2d} 弱={cnt.get('弱',0):2d}  {bar}")

        if args.dry_run:
            print("\n[dry-run] 不写库")
            return 0

        n = upsert_rows(conn, all_rows, name_map)
        print(f"\n写入 theme_index_map: {n} 行")
        if uncovered:
            print(f"警告：{len(uncovered)} 个指数无题材映射: {sorted(uncovered)[:15]}")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
