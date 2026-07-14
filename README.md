# teststock

A 股 ETF 战略配置与回测项目：宏观数据入库、年初定方向 Agent、简单回测引擎。

## 环境

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-db.txt
cp .env.example .env   # 填入 Tushare / MySQL / LLM 配置
```

## MySQL（Podman）

```bash
podman run -d --name teststock-mysql -p 3306:3306 \
  -e MYSQL_DATABASE=teststock -e MYSQL_USER=teststock -e MYSQL_PASSWORD=teststock \
  -e MYSQL_ROOT_PASSWORD=teststock_root \
  -v teststock-mysql-data:/var/lib/mysql mysql:8.0
```

按 `sql/` 下 schema 建表后，运行 `scripts/import_*.py` 导入数据。

## 年初定方向 Agent

```bash
python scripts/run_annual_direction.py run 2012
```

MCP 服务（Cursor）：

```bash
python -m mcp_servers.annual_direction
```

## 回测

```bash
python scripts/run_annual_backtest.py 2011 --cash 1000000
```

## 财经资讯采集流水线

每日采集财经/产业/国际资讯，纯文本写入 `news_article`；LLM 抽取结果写入 `news_extraction`（供 CSI 行业指数预测）。

```bash
# 建表（首次）
mysql -u teststock -pteststock teststock < sql/news_pipeline_schema.sql

# 快讯（建议每 30 分钟）
python scripts/run_daily_news.py --tier flash

# 日报（央视/研报/发改委/国际，建议每天 20:30）
python scripts/run_daily_news.py --tier daily

# 全量
python scripts/run_daily_news.py --tier all

# LLM 抽取（需配置 .env 中 LLM_*）
python scripts/run_news_extraction.py --limit 20

# 安装 macOS 定时任务（含 21:00 加工）
bash scripts/install_news_launchd.sh

# 全链路：预筛 → 聚类 → 抽取 → daily/weekly → CSI
python scripts/run_full_news_intelligence.py
python scripts/verify_news_processing.py
```

设计文档：`docs/design/news_intelligence_master_plan.md`  
采集准入：`docs/design/news_pipeline_todos.md`。

## 年度 CSI 指数推荐

结合政策信号、新闻语义、动量/估值，每年输出 CSI 指数排行。

```bash
# 一键：新闻抽取 + 题材聚合 + CSI 排行 + 验证
python scripts/run_annual_csi_recommendation.py --year 2026

# 分步
python scripts/aggregate_theme_news_signals.py --year 2026 --live
python scripts/rank_annual_csi.py --year 2026 --top 30 --save
python scripts/validate_csi_rank.py --year 2026

# 历史回测验证（2015-2024）
python scripts/validate_csi_rank.py --from 2015 --to 2024 --regenerate
```

设计文档：`docs/design/annual_csi_recommendation.md`

## CSI 研报增强框架自动化

新框架使用日行情/估值、指数成分、宏观环境、上一年 H2 行业研报元数据，定期重建特征并跑年度验证。

```bash
# 只检查 MySQL 覆盖情况
python scripts/run_csi_research_pipeline.py --summary-only

# 全流程：市场数据同步 -> H2 行业研报同步 -> 新框架回测与质量门禁
python scripts/run_csi_research_pipeline.py

# 只刷新研报和回测，不跑市场同步
python scripts/run_csi_research_pipeline.py --skip-market-sync

# Codex 自动化调度
# 已通过 ~/.codex/automations/teststock-csi-research-pipeline/automation.toml
# 配置为周度运行；市场数据由已有 teststock-daily-market-data-sync 自动化负责。
```

核心产物写入 `data/ml/regime_research_csi_strategy_*.csv/json`，`data/` 不入库，可由脚本复跑生成。
