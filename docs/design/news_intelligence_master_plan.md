# 新闻智能 + 年度 CSI — 完整需求清单

> 汇总全部讨论；**2026-06-30 01:23 全链路验收通过**（`run_full_news_intelligence.py` + `verify_news_processing.py`）。

---

## A. 采集层

| ID | 需求 | 状态 |
|----|------|------|
| A1 | 快讯/日报/国际 launchd | ✅ |
| A2 | content_hash 去重 | ✅ |

---

## B. 检索与预筛（L0b）

| ID | 需求 | 状态 |
|----|------|------|
| B1 | `theme_keywords` 83 词 | ✅ |
| B2 | FULLTEXT ngram `ft_title_body` | ✅ |
| B3 | `news/retrieval/prefilter.py` | ✅ |
| B4 | `prefilter_themes` / `prefilter_score` 10527 篇 | ✅ |
| B5 | flash 无命中跳过 LLM；policy/research 强制 | ✅ |

---

## C. 加工管线（L1–L3）

| ID | 需求 | 状态 | 验收数据 |
|----|------|------|----------|
| C1 | event/daily/weekly/mention 表 | ✅ | schema PASS |
| C2 | 全量聚类 | ✅ | 9571 events / 10550 members |
| C3 | mention_counter → salience | ✅ | 217 counters, 339 multi-mention |
| C4 | category 权重 | ✅ | 单元测试 |
| C5 | event LLM + event_id | ✅ | 8575 extractions linked |
| C6 | theme_news_daily | ✅ | 2170 行 |
| C7 | theme_news_weekly | ✅ | rollup 完成 |
| C8 | launchd 21:00 processing | ✅ | 已 install |
| C9 | theme_news_signals daily rollup | ✅ | aggregate --live |

---

## D. 年度 CSI Phase 2

| ID | 需求 | 状态 |
|----|------|------|
| D1 | policy duration_mult | ✅ `csi/enhanced.py` |
| D2 | heat_penalty | ✅ |
| D3 | 相关去重 ρ>0.85 | ✅ |
| D4 | index_scorecard 接入 | ✅ `csi/index_scorecard.py` |
| D5 | 消融 JSON 报告 | ✅ `data/backtests/csi_news_ablation.json` |
| D6 | validate_csi_rank | ✅ |
| D7 | ETF 映射 | ✅ `map_csi_to_etf.py` |
| D8 | Agent context csi_top10 | ✅ `context.py` |

---

## E. 验证

| ID | 需求 | 状态 |
|----|------|------|
| E1 | processing + retrieval 单元测试 | ✅ |
| E2 | verify_news_processing | ✅ ALL PASS |
| E3 | run_full_news_intelligence | ✅ COMPLETE |
| E4 | README | ✅ |

---

## 执行命令

```bash
python scripts/run_full_news_intelligence.py      # 全链路
python scripts/run_news_daily_processing.py       # 日批
python scripts/backtest_news_salience.py --live   # 题材消融
python scripts/validate_csi_ablation.py --live    # CSI 消融
bash scripts/install_news_launchd.sh              # 三任务调度
```

---

## 已知限制（非阻塞）

- LLM 实网仍超时 → 日批默认 `--mock`；`.env` 配通后改 `--no-mock`
- 历史年份新闻回填 2015–2024 未做 → 长期回测待 backfill_news
- 向量语义去重 → 后置可选（当前标题聚类 + 183 cross-source events）
