# 新闻加工管线 — 最终方案

> 汇总讨论结论：去重与信号增强分离、混合调度、事件簇 + 传播强度模型。

---

## 一、问题与原则

| 原则 | 说明 |
|------|------|
| **去重 ≠ 降权** | 入库/聚类去重只为省 LLM；强度在聚合层随 mention/sources/duration **增强** |
| **分层对象** | `news_article` → `news_event`（事件簇）→ `theme_news_daily` → `theme_news_signals` |
| **混合调度** | L0 写入时；L1–L3 日批；年度 CSI 只 rollup，不重跑 LLM |
| **可复现** | 日表按 `signal_date` 版本化；年度 `as_of_date` 截断 |

---

## 二、架构

```
采集（持续，已有 launchd）
  └─ L0 入库 content_hash 去重 + news_mention_counter.mention_count++

日批 21:00（新增 launchd）
  ├─ L1 事件聚类（72h 滑动窗，标题相似度 + event_fingerprint）
  ├─ L2 LLM/mock 抽取（仅 canonical 代表稿，写 news_extraction.event_id）
  └─ L3 theme_news_daily（事件级 salience 加权）

年度 / 按需
  └─ aggregate_theme_news_signals：sum(daily) → theme_news_signals
  └─ rank_annual_csi：读 theme_news_signals（默认 skip-extract）
```

---

## 三、事件簇与传播强度

### 3.1 事件聚类（L1）

- **窗口**：处理日 D 的 `[D-3, D]`（72h），可挂接到已有未关闭 event（`last_seen` 在窗内）
- **指纹**：`event_fingerprint = md5(normalize(title)[:120])`，跨源同标题直接同簇
- **相似标题**：token Jaccard ≥ 0.72 或 SequenceMatcher ≥ 0.85 → 同簇
- **代表稿**：正文最长；`mention_count`、`unique_sources` 在簇上累计

### 3.2 强度公式（L3）

```python
base = sign × magnitude × confidence
mention_mult   = log(1 + mention_count)
source_mult    = 1 + α × max(0, unique_sources - 1)    # α=0.2
duration_mult  = 1 + β × min(duration_days, 7)         # β=0.1

event_weight = base × mention_mult × source_mult × duration_mult
```

- **A 纯复制**：hash 重复 → `news_mention_counter++`，不重复 LLM
- **B 同事件异稿**：进同一 event，sources/mentions 累加，LLM 1 次
- **C 同题材不同事件**：独立 event，均计入 theme

### 3.3 theme_news_daily 字段

| 字段 | 含义 |
|------|------|
| `event_count` | 当日命中该 theme 的事件数 |
| `mention_count` | 簇内报道条数之和 |
| `source_diversity` | 当日该 theme 涉及的不重复来源数 |
| `net_score` | salience 加权 bull−bear |

---

## 四、调度

| 层 | 时机 | 脚本 |
|----|------|------|
| L0 | 采集写入 | `collectors/storage.py` |
| L1–L3 | 每日 21:00 | `scripts/run_news_daily_processing.py` |
| 年度 rollup | `rank_annual_csi` 前 | `scripts/aggregate_theme_news_signals.py` |
| 一键年度 | 手动 | `scripts/run_annual_csi_recommendation.py --skip-extract` |

---

## 五、验证与回测

| 项 | 命令 | 通过标准 |
|----|------|----------|
| 单元测试 | `python scripts/test_news_processing_unit.py` | 全绿 |
| 管线验证 | `python scripts/verify_news_pipeline.py` | 表/Job 正常 |
| 强度消融 | `python scripts/backtest_news_salience.py` | salience ≥ flat 或 Top theme 更合理 |
| CSI 截面 | `python scripts/validate_csi_rank.py --year 2026` | 有推荐输出 |
| 日批实跑 | `python scripts/run_news_daily_processing.py --backfill` | events/daily 有数据 |

参数 `α, β` 可在 `news/processing/salience.py` 中调；后续用 2015–2024 新闻回填 + forward return 做网格搜索。

---

## 六、任务清单

- [x] P0 设计文档（本文）
- [ ] P1 SQL：`sql/news_processing_schema.sql`
- [ ] P2 模块：`news/processing/{cluster,salience,daily,batch}.py`
- [ ] P3 入库 mention 计数：`collectors/storage.py`
- [ ] P4 日批脚本 + launchd plist
- [ ] P5 抽取挂 event_id；聚合改读 daily
- [ ] P6 单元测试 + 消融回测 + 全量 backfill 实跑

---

## 七、与 annual_csi_recommendation 关系

- `theme_news_signals.net_score` 改为 **daily rollup**（含 salience），不再直接扫 `news_extraction` 逐条相加
- 新增列：`event_count`, `mention_count`, `source_diversity` 供分析与调试
- 年度窗口仍为 H2(Y−1)；`as_of_date = Y-01-01`
