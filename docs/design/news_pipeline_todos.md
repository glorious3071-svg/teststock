# 财经资讯采集流水线 — TODO 与准入准出标准

> 目标：每日自动采集财经/产业/国际资讯，纯文本入库 `news_article`；LLM 抽取结果写入 `news_extraction`，供 CSI 行业指数预测使用。

---

## Phase 0 — 基础设施

| 项 | 准入标准 | 准出标准 |
|----|----------|----------|
| P0-1 SQL Schema | 无 | `sql/news_pipeline_schema.sql` 含 `news_article`、`collect_run`、`news_extraction`；字段与框架设计一致 |
| P0-2 DB 连接 | P0-1 完成 | `db/connection.py` 统一 `mysql_config()` / `get_connection()`；所有模块复用 |
| P0-3 采集基类 | P0-2 完成 | `collectors/base.py` 定义 `RawArticle`、`CollectResult`、`BaseCollector.run()`；含运行日志写入 |
| P0-4 去重工具 | P0-3 完成 | `collectors/dedup.py` 提供 `content_hash()`、`normalize_text()`、`html_to_text()` |

**验收命令：**

```bash
mysql -u teststock -pteststock teststock < sql/news_pipeline_schema.sql
python -c "from db.connection import get_connection; get_connection().close(); print('ok')"
```

---

## Phase 1 — 快讯采集（P0 源）

| 项 | 准入标准 | 准出标准 |
|----|----------|----------|
| P1-1 东财快讯 | Phase 0 完成 | `EastmoneyFlashCollector` 写入 `news_article`；重复运行 `inserted=0` |
| P1-2 新浪快讯 | P1-1 通过 | `SinaFlashCollector` 同上 |
| P1-3 同花顺快讯 | P1-2 通过 | `ThsFlashCollector` 同上 |
| P1-4 镜像兼容 | P1-3 通过 | 可选写入 `news_flash`（`mirror_legacy=True`） |

**验收命令：**

```bash
python scripts/run_daily_news.py --tier flash --dry-run
python scripts/run_daily_news.py --tier flash
python scripts/run_daily_news.py --tier flash   # 第二次 skipped_dup > 0
```

---

## Phase 2 — 日报源（政策/研报/央视）

| 项 | 准入标准 | 准出标准 |
|----|----------|----------|
| P2-1 新闻联播 | Phase 1 完成 | `CctvDailyCollector` 增量拉取当日/缺失日；`category=policy` |
| P2-2 行业研报 | P2-1 通过 | `IndustryResearchCollector` 拉最近 N 页行业研报；`category=research` |
| P2-3 发改委政策 | P2-2 通过 | `NdrcPolicyCollector` 每 section 首页增量；`category=policy` |

**验收命令：**

```bash
python scripts/run_daily_news.py --tier daily
```

---

## Phase 3 — 调度编排

| 项 | 准入标准 | 准出标准 |
|----|----------|----------|
| P3-1 统一入口 | Phase 2 完成 | `scripts/run_daily_news.py` 支持 `--tier flash|daily|all`、`--since`、`--dry-run` |
| P3-2 注册表 | P3-1 通过 | `collectors/registry.py` 集中管理 collector 列表 |
| P3-3 依赖声明 | P3-1 通过 | `akshare` 写入 `requirements.txt` |

**验收命令：**

```bash
python scripts/run_daily_news.py --tier all --dry-run
python scripts/run_daily_news.py --list
```

---

## Phase 4 — 国际资讯

| 项 | 准入标准 | 准出标准 |
|----|----------|----------|
| P4-1 国际源 | Phase 3 完成 | `IntlNewsCollector` 拉百度经济新闻等；`category=intl`，`lang=zh` |

**验收命令：**

```bash
python scripts/run_daily_news.py --collector intl_cls
```

---

## Phase 5 — LLM 抽取

| 项 | 准入标准 | 准出标准 |
|----|----------|----------|
| P5-1 抽取脚本 | Phase 4 完成；`.env` 配 LLM | `scripts/run_news_extraction.py` 从未处理文章批量抽取 |
| P5-2 题材约束 | P5-1 通过 | 输出 `themes` 限定为 `CANONICAL_THEMES` 17 项 |
| P5-3 幂等 | P5-2 通过 | 同一 `article_id` 不重复抽取（除非 `--force`） |

**验收命令：**

```bash
python scripts/run_news_extraction.py --limit 5 --dry-run
python scripts/run_news_extraction.py --limit 10
```

---

## Phase 6 — 自动化调度

| 项 | 准入标准 | 准出标准 |
|----|----------|----------|
| P6-1 launchd | Phase 3 完成 | `scripts/launchd/*.plist` 模板；flash 30min + daily 20:30 |
| P6-2 文档 | Phase 5 完成 | README 新闻流水线章节 |

**验收命令：**

```bash
launchctl load ~/Library/LaunchAgents/ai.jingxuan.teststock-news-flash.plist
launchctl list | grep teststock-news
```
