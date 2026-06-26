#!/usr/bin/env python3
"""Generate annual_sector_signals for every quarter from 2006-Q1 to 2026-Q1.

政策维度数据源：
  - cewc_annual (年度 CEWC 公报，按 apply_year 对应)
  - npr_policy  (发改委政策文件，取 as_of_date 前 6 个月内的文件)

输出：annual_sector_signals，每季度每行业一行，(as_of_date, theme) 唯一。
幂等：ON DUPLICATE KEY UPDATE，中断后重跑自动续传（跳过已有完整数据的季度）。

Usage:
  python scripts/generate_sector_signals_quarterly.py           # 全量
  python scripts/generate_sector_signals_quarterly.py --dry-run # 只打印，不入库
  python scripts/generate_sector_signals_quarterly.py --from 2015-01-01  # 从指定季度起
  python scripts/generate_sector_signals_quarterly.py --quarter 2020-07-01  # 单季度
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymysql
from dotenv import load_dotenv

from agents.annual_direction.llm_client import LLMError, chat

MODEL_VERSION = os.getenv("LLM_MODEL", "glm-5.1")
PROMPT_VERSION = "v1.2-canonical"
LLM_SLEEP = 1.5        # 每次 LLM 调用后等待
NPR_LOOKBACK_DAYS = 180  # 取 as_of_date 前 N 天的 npr_policy
NPR_MAX_DOCS = 12       # 最多放入 Prompt 的政策文件数
NPR_CONTENT_CHARS = 150 # 每篇正文截取字符数

# 固定主题列表，LLM 只能从此选择，保证跨季度可比性
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

_THEME_LIST_STR = "\n".join(f"  - {t}" for t in CANONICAL_THEMES)

SYSTEM_PROMPT = f"""你是 A 股行业配置分析师。根据给定时间点（as_of_date）的政策信息，从下方固定主题列表中选出当期政策明确支持的方向，并评估信号强度。

【固定主题列表（theme 必须严格使用列表中的原文，不得自创）】
{_THEME_LIST_STR}

输出要求：
- 从上述列表中选取 5~10 个与当期政策匹配的主题（不匹配的主题不要输出）
- 信号强度判定标准：强=政策明确点名、有量化目标或专项部署；中=有提及但泛化；弱=间接相关
- 每个方向：theme（从列表原文选取）、signal_strength（强/中/弱）、policy_basis（政策原文片段，≤80字）、rationale（推理，≤60字）
- 仅基于提供的文本，不引入外部信息
- 输出 JSON 代码块：

```json
{{
  "signals": [
    {{
      "theme": "科技创新/自主创新",
      "signal_strength": "强",
      "policy_basis": "以科技创新引领新质生产力发展",
      "rationale": "CEWC 将科技创新列为首要结构性任务。"
    }}
  ]
}}
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


def quarter_dates(start: date, end: date) -> list[date]:
    """Generate quarterly dates (Jan/Apr/Jul/Oct 1st) inclusive."""
    quarters = []
    d = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
    while d <= end:
        if d >= start:
            quarters.append(d)
        # Advance one quarter
        m = d.month + 3
        y = d.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        d = date(y, m, 1)
    return quarters


def load_cewc(conn, apply_year: int) -> dict | None:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM cewc_annual WHERE apply_year = %s", (apply_year,))
        return cur.fetchone()


def load_npr_policy(conn, as_of_date: date) -> list[dict]:
    """最近 NPR_LOOKBACK_DAYS 天内的政策文件，最多 NPR_MAX_DOCS 篇。"""
    since = as_of_date - timedelta(days=NPR_LOOKBACK_DAYS)
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("""
            SELECT title, pubtime, ptype, content_html
            FROM npr_policy
            WHERE pubtime >= %s AND pubtime < %s
            ORDER BY pubtime DESC
            LIMIT %s
        """, (since.isoformat(), as_of_date.isoformat(), NPR_MAX_DOCS))
        return cur.fetchall()


def build_prompt(as_of_date: date, cewc: dict | None, npr_docs: list[dict]) -> str:
    lines = [f"## 分析基准日: {as_of_date.isoformat()}"]

    if cewc:
        lines += [
            "",
            f"## 中央经济工作会议（apply_year={cewc['apply_year']}，会议于{cewc.get('meeting_end')}结束）",
            f"主题: {cewc.get('theme')}",
            f"基调: {cewc.get('tone')}",
            f"财政政策: {cewc.get('fiscal_policy')}",
            f"货币政策: {cewc.get('monetary_policy')}",
            f"首要任务: {cewc.get('primary_task')}",
            f"关键词: {cewc.get('keywords')}",
        ]
        if cewc.get("raw_text"):
            lines += ["", "### 公报原文", cewc["raw_text"]]
    else:
        lines.append("\n## 中央经济工作会议：暂无数据")

    if npr_docs:
        lines += ["", f"## 近期发改委政策文件（过去{NPR_LOOKBACK_DAYS}天，共{len(npr_docs)}篇）"]
        for doc in npr_docs:
            snippet = ""
            if doc.get("content_html"):
                snippet = " | " + doc["content_html"][:NPR_CONTENT_CHARS].replace("\n", " ")
            lines.append(f"- [{doc.get('ptype','')}] {doc.get('pubtime',''):%Y-%m-%d} {doc.get('title','')}{snippet}")
    else:
        lines.append("\n## 发改委政策文件：该时段暂无")

    lines.append("\n请根据以上政策信息识别重点关注行业/板块并输出 JSON。")
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


def already_done(conn, as_of_date: date) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM annual_sector_signals WHERE as_of_date = %s",
            (as_of_date.isoformat(),),
        )
        return cur.fetchone()[0] > 0


def upsert_signals(conn, apply_year: int, as_of_date: date, signals: list[dict]) -> int:
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
            as_of_date.isoformat(),
            s.get("theme", "")[:50],
            s.get("signal_strength", "中"),
            (s.get("policy_basis") or "")[:500],
            s.get("rationale") or "",
            MODEL_VERSION,
            PROMPT_VERSION,
        )
        for s in signals if s.get("theme")
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="按季度生成行业信号（政策维度）")
    parser.add_argument("--from", dest="from_date", default="2006-01-01", help="起始季度日期")
    parser.add_argument("--to", dest="to_date", default="2026-01-01", help="结束季度日期")
    parser.add_argument("--quarter", help="仅跑单个季度（如 2020-07-01）")
    parser.add_argument("--dry-run", action="store_true", help="不写库")
    parser.add_argument("--force", action="store_true", help="强制重跑（覆盖已有数据）")
    args = parser.parse_args()

    if args.quarter:
        dates = [date.fromisoformat(args.quarter)]
    else:
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date)
        dates = quarter_dates(start, end)

    print(f"共 {len(dates)} 个季度点: {dates[0]} ~ {dates[-1]}")

    conn = pymysql.connect(**mysql_config())
    total_signals = skipped = 0

    try:
        for i, qdate in enumerate(dates, 1):
            apply_year = qdate.year
            label = f"[{i}/{len(dates)}] {qdate}"

            if not args.force and already_done(conn, qdate):
                print(f"{label} 已有数据，跳过")
                skipped += 1
                continue

            cewc = load_cewc(conn, apply_year)
            npr_docs = load_npr_policy(conn, qdate)

            print(f"{label}  CEWC={apply_year}{'✓' if cewc else '✗'}  "
                  f"npr={len(npr_docs)}篇", end="  ", flush=True)

            prompt = build_prompt(qdate, cewc, npr_docs)

            try:
                response = chat([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ])
            except LLMError as e:
                print(f"LLM FAIL: {e}")
                continue

            signals = parse_signals(response)
            if not signals:
                print("解析失败")
                continue

            print(f"→ {len(signals)} 个信号", end="")

            if args.dry_run:
                print(" [dry-run]")
                for s in signals:
                    print(f"    [{s.get('signal_strength')}] {s.get('theme')}")
            else:
                n = upsert_signals(conn, apply_year, qdate, signals)
                total_signals += n
                print(f" → 写入 {n} 行")

            time.sleep(LLM_SLEEP)

    finally:
        conn.close()

    print(f"\n完成: 写入 {total_signals} 行信号，跳过 {skipped} 个已有季度")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
