#!/usr/bin/env python3
"""Generate annual_sector_signals from CEWC policy text via LLM.

Usage:
  python scripts/generate_sector_signals.py --year 2026
  python scripts/generate_sector_signals.py --year 2026 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from agents.annual_direction.llm_client import LLMError, chat

MODEL_VERSION = os.getenv("LLM_MODEL", "glm-5.1")
PROMPT_VERSION = "v1.0"

SYSTEM_PROMPT = """你是 A 股行业配置分析师。根据中央经济工作会议（CEWC）公报及结构性政策信息，
识别当年度重点关注的行业/板块方向，并评估政策信号强度。

输出要求：
- 识别 5~10 个行业/板块方向
- 每个方向给出：theme（板块名）、signal_strength（强/中/弱）、policy_basis（政策依据原文片段）、rationale（推理说明）
- 仅基于文本内容推断，不引入文本之外的信息
- 以 JSON 代码块输出，格式如下：

```json
{
  "signals": [
    {
      "theme": "科技/AI",
      "signal_strength": "强",
      "policy_basis": "以科技创新引领新质生产力发展，建设现代化产业体系",
      "rationale": "CEWC 将科技创新列为首要结构性任务，AI/半导体/机器人等新质生产力方向获政策优先支持。"
    }
  ]
}
```"""


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


def load_cewc(conn, apply_year: int) -> dict | None:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM cewc_annual WHERE apply_year = %s", (apply_year,))
        return cur.fetchone()


def build_user_prompt(cewc: dict) -> str:
    lines = [
        f"## 中央经济工作会议数据（apply_year={cewc['apply_year']}）",
        f"会议时间：{cewc.get('meeting_start')} ~ {cewc.get('meeting_end')}",
        f"主题：{cewc.get('theme')}",
        f"基调：{cewc.get('tone')}",
        f"财政政策：{cewc.get('fiscal_policy')}",
        f"货币政策：{cewc.get('monetary_policy')}",
        f"关键词：{cewc.get('keywords')}",
        f"首要任务：{cewc.get('primary_task')}",
        f"摘要：{cewc.get('summary')}",
    ]
    if cewc.get("raw_text"):
        lines += ["", "## 公报原文", cewc["raw_text"]]
    lines += ["", "请根据以上内容，识别年度重点关注的行业/板块方向并输出 JSON。"]
    return "\n".join(lines)


def parse_signals(text: str) -> list[dict]:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
            signals = data.get("signals", [])
            if isinstance(signals, list) and signals:
                return signals
        except json.JSONDecodeError:
            continue
    return []


def upsert_signals(conn, apply_year: int, as_of_date: str, signals: list[dict]) -> int:
    sql = """
        INSERT INTO annual_sector_signals
            (apply_year, as_of_date, theme, signal_strength, policy_basis, rationale,
             model_version, prompt_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            signal_strength = VALUES(signal_strength),
            policy_basis    = VALUES(policy_basis),
            rationale       = VALUES(rationale),
            model_version   = VALUES(model_version),
            prompt_version  = VALUES(prompt_version),
            updated_at      = CURRENT_TIMESTAMP
    """
    rows = [
        (
            apply_year,
            as_of_date,
            s.get("theme", ""),
            s.get("signal_strength", "中"),
            s.get("policy_basis", "")[:500],
            s.get("rationale", ""),
            MODEL_VERSION,
            PROMPT_VERSION,
        )
        for s in signals
        if s.get("theme")
    ]
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 解析 CEWC → annual_sector_signals")
    parser.add_argument("--year", type=int, default=date.today().year, help="apply_year（默认当前年）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不写库")
    args = parser.parse_args()

    apply_year = args.year
    as_of_date = f"{apply_year}-01-01"

    conn = pymysql.connect(**mysql_config())
    try:
        cewc = load_cewc(conn, apply_year)
        if not cewc:
            print(f"cewc_annual 中无 apply_year={apply_year} 的数据")
            return 1

        print(f"CEWC {apply_year}：{cewc.get('theme')} | 财政={cewc.get('fiscal_policy')} 货币={cewc.get('monetary_policy')}")

        user_prompt = build_user_prompt(cewc)
        print(f"\n调用 LLM（model={MODEL_VERSION}）...")
        try:
            response = chat([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ])
        except LLMError as e:
            print(f"LLM 调用失败: {e}")
            return 1

        print("\n--- LLM 原始输出 ---")
        print(response)
        print("---")

        signals = parse_signals(response)
        if not signals:
            print("未能从输出中解析到 signals JSON，退出")
            return 1

        print(f"\n解析到 {len(signals)} 个行业信号：")
        for s in signals:
            print(f"  [{s.get('signal_strength','?')}] {s.get('theme')} — {s.get('policy_basis','')[:60]}")

        if args.dry_run:
            print("\n[dry-run] 不写入数据库")
            return 0

        n = upsert_signals(conn, apply_year, as_of_date, signals)
        print(f"\nupserted {n} 行 → annual_sector_signals (as_of_date={as_of_date})")

    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
