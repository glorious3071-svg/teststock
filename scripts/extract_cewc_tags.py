#!/usr/bin/env python3
"""Extract CEWC tags via LLM and write to cewc_tags table.

For each apply_year in cewc_full_text, call LLM (glm-5.1 via MaaS) with the
full text and a structured prompt, parse the JSON array response, and INSERT
each tag row (NOT upsert — keeps multi-version history for prompt iteration).

用法：
    python3 scripts/extract_cewc_tags.py --year 2009 --prompt-version v1   # 单年
    python3 scripts/extract_cewc_tags.py --prompt-version v1               # 全部
    python3 scripts/extract_cewc_tags.py --year 2008 --dry-run             # 仅打印不入库
    python3 scripts/extract_cewc_tags.py --years 2006,2007,2008 --prompt-version v1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from agents.annual_direction.llm_client import chat, llm_config

SCHEMA_FILE = ROOT / "sql" / "cewc_tags_schema.sql"
FAILURES_LOG = ROOT / "data" / "cewc_tags_failures.log"
REQUEST_SLEEP = 2.0   # MaaS 限速保护


SYSTEM_PROMPT = """你是一名严谨的中国宏观政策分析专家。\
你的任务是从中央经济工作会议公报中提取结构化标签。\
所有输出必须严格遵守要求的 JSON 数组格式。"""


USER_PROMPT_TEMPLATE = """请阅读下面这份「中央经济工作会议」公报，提取多维度标签。

## 公报指导年份
{apply_year} 年

## 公报全文
{raw_text}

## 标签类别（tag_category，可在合理时扩展，但优先使用以下 6 类）
| category | 含义 | name 取值建议 | value 写什么 |
|---|---|---|---|
| policy_stance | 政策基调 | monetary / fiscal / property / regulatory / industry | 三态枚举（从紧/稳健/适度宽松/积极/中性/收紧/放松） |
| primary_focus | 首要任务/年度主线 | 用 1-3 个英文/拼音单词标识 | 完整原文短语 |
| structural_reform | 结构性改革重点 | supply_side / demand_side / industry_upgrade / state_reform / opening_up 等 | 描述具体方向 |
| risk_warning | 风险警示/防范点 | property / local_debt / inflation / deflation / capital_flow / financial / employment 等 | 描述具体风险 |
| numeric_target | 数值目标 | gdp / cpi / m2 / fiscal_deficit / urban_employment 等 | 写百分比或数字（如 "5.5%"） |
| key_phrase | 关键提法/新词 | 用 snake_case 提法标识（如 high_quality_dev, new_productive_forces, dual_circulation） | 完整原话短语 |

## 输出要求
- 仅返回一个 JSON 数组，**不要**用 markdown 代码块包裹
- 每条 tag 包含 5 个字段：
    - tag_category: 上表中的某个类别（或合理扩展）
    - tag_name: 英文/snake_case 标签名
    - tag_value: 标签值（中文）
    - confidence: 0.0-1.0 的置信度
    - evidence: 不超过 100 字的原文片段（断章取义为依据）
- 一篇公报抽 8-20 条标签
- 严格基于公报内容，不要凭空补充上下文
- 不要返回解释性文字

## 示例输出格式
[
  {{"tag_category":"policy_stance","tag_name":"monetary","tag_value":"适度宽松","confidence":0.95,"evidence":"实施适度宽松的货币政策"}},
  {{"tag_category":"primary_focus","tag_name":"growth","tag_value":"保增长扩内需","confidence":0.9,"evidence":"把保增长作为首要任务"}}
]
"""


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


def fetch_years(cur, years: list[int] | None) -> list[tuple[int, str]]:
    if years:
        cur.execute(
            "SELECT apply_year, raw_text FROM cewc_full_text WHERE apply_year IN %s ORDER BY apply_year",
            (tuple(years),),
        )
    else:
        cur.execute(
            "SELECT apply_year, raw_text FROM cewc_full_text ORDER BY apply_year"
        )
    return [(int(y), txt) for y, txt in cur.fetchall()]


JSON_ARRAY_RE = re.compile(r"\[\s*\{.*?\}\s*\]", re.S)


def extract_json_array(text: str) -> list[dict] | None:
    """从 LLM 响应里提取 JSON 数组。先直解析，失败再正则提取。"""
    raw = text.strip()
    # 去掉可能的 markdown 代码块
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        pass
    m = JSON_ARRAY_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def log_failure(year: int, prompt_version: str, model_version: str,
                raw_resp: str, error: str) -> None:
    FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(FAILURES_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== {year} @ {prompt_version} / {model_version} =====\n")
        f.write(f"ERROR: {error}\n")
        f.write(f"RAW RESPONSE:\n{raw_resp}\n")


def insert_tags(cur, year: int, tags: list[dict],
                model_version: str, prompt_version: str) -> int:
    sql = """
        INSERT INTO cewc_tags
            (apply_year, tag_category, tag_name, tag_value,
             confidence, evidence, model_version, prompt_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    rows = []
    for t in tags:
        cat = (t.get("tag_category") or "").strip()
        name = (t.get("tag_name") or "").strip()
        if not cat or not name:
            continue
        val = t.get("tag_value")
        if isinstance(val, (int, float)):
            val = str(val)
        elif val is not None:
            val = str(val)[:500]
        evidence = t.get("evidence")
        evidence = str(evidence)[:1000] if evidence else None
        conf = t.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        rows.append((year, cat[:50], name[:100], val,
                     conf, evidence, model_version, prompt_version))
    if not rows:
        return 0
    cur.executemany(sql, rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract CEWC tags via LLM")
    parser.add_argument("--year", type=int, default=None, help="单年提取")
    parser.add_argument("--years", default=None, help="逗号分隔多年，如 2006,2007")
    parser.add_argument("--prompt-version", default="v1", help="prompt 版本号")
    parser.add_argument("--dry-run", action="store_true", help="只打印不入库")
    args = parser.parse_args()

    if args.year:
        target_years = [args.year]
    elif args.years:
        target_years = [int(y) for y in args.years.split(",")]
    else:
        target_years = None

    cfg = llm_config()
    model_version = cfg["model"]
    print(f"LLM: model={model_version}, prompt_version={args.prompt_version}, dry_run={args.dry_run}")

    conn = pymysql.connect(**mysql_config())
    try:
        if not args.dry_run:
            apply_schema(conn)

        with conn.cursor() as cur:
            samples = fetch_years(cur, target_years)
            print(f"待处理 {len(samples)} 年: {[y for y, _ in samples]}")

            total_inserted = 0
            for i, (year, raw_text) in enumerate(samples):
                prompt = USER_PROMPT_TEMPLATE.format(
                    apply_year=year, raw_text=raw_text)
                if i > 0:
                    time.sleep(REQUEST_SLEEP)
                try:
                    resp = chat(
                        [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user",   "content": prompt}],
                        temperature=0.2,
                    )
                except Exception as e:
                    print(f"[{year}] LLM 调用失败: {e}")
                    log_failure(year, args.prompt_version, model_version, "", str(e))
                    continue

                tags = extract_json_array(resp)
                if tags is None:
                    print(f"[{year}] JSON 解析失败，已落 failures.log（resp 前 200 字: {resp[:200]!r}）")
                    log_failure(year, args.prompt_version, model_version,
                                resp, "JSON parse failed")
                    continue

                print(f"[{year}] LLM 提取 {len(tags)} 条标签")
                if args.dry_run:
                    for t in tags:
                        print(f"    {t}")
                    continue
                n = insert_tags(cur, year, tags,
                                model_version, args.prompt_version)
                total_inserted += n
                print(f"        入库 {n} 条")
            if not args.dry_run:
                conn.commit()
                print(f"\n=== 总计入库 {total_inserted} 条 ===")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
