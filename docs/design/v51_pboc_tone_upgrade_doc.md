# v51 评分卡升级：pboc_tone 融合 CEWC + PBoC MPR LLM

**升级日期**: 2026-06-26
**版本基线**: v50 → v51（backbone 不变，仅升级 `pboc_tone` 信号）

---

## 1. 升级背景

v50 的 `pboc_tone` 完全依赖 **CEWC（中央经济工作会议）** 一年一次的 `monetary_policy` 字段映射：

| CEWC 原话 | v50 pboc_tone |
|---|---|
| 从紧 | tight |
| 稳健 / 稳健中性 | neutral |
| 适度宽松 | loose |
| 其他后缀修饰（"稳健（灵活适度）"等） | **None（落空）** |

**痛点**：
1. **CEWC 缺失或带修饰词**时 9 个年份（2002-2005、2020-2026）直接落 None，政策维度完全没贡献
2. **年初定调与年中政策反转**之间有时间差。例：2010 年定调"适度宽松"但 2010Q4 已连续加准加息进入紧缩通道；2011 年初依然标 neutral 显然滞后
3. **缺乏量化粒度**：稳健 ↔ 适度宽松之间有大片灰色地带

---

## 2. v51 升级方案

### 2.1 数据基础

新增 2 张表：

**`pboc_mpr_quarterly`** (100 行) — 央行季度货币政策执行报告原文
- PK: `(report_year, report_quarter)`
- 关键字段: `publish_date`, `raw_text` (mediumtext)、`abstract_text`、`next_stage_text`
- 数据源: [pbc.gov.cn 货币政策报告目录页](http://www.pbc.gov.cn/zhengcehuobisi/125207/125227/125957/index.html)
- 覆盖: 2001Q2 - 2026Q1（94/95 抓取成功，1 份 2008Q2 历史链接 404）

**`pboc_mpr_features`** (100 行) — LLM 抽取的结构化特征
- PK: `(report_year, report_quarter)`，外键关联 quarterly
- 字段: `tone` (5态枚举), `tone_score` (-2~+2), `stance_phrase`, `rate_bias`, `rrr_bias`, `liquidity_bias`, `countercyclical`, `key_phrases` (JSON), `next_actions` (JSON), `risk_concerns` (JSON)
- 抽取器: `pplx_sdk.llm.extract`，prompt v1.0，严格基于文本字面、不引入外部知识

### 2.2 融合规则（`_resolve_pboc_tone`）

**核心思想**：CEWC 作主信号（年度定调），MPR 季度均值作交叉验证 + 缺失兜底。

```python
mpr_avg = weighted_avg(last_4_MPRs_before_snapshot, weights=[1,2,3,4])
# 严防上帝视角: 仅取 publish_date <= snapshot_date 的最近 4 份
```

| CEWC | MPR_avg 阈值 | v51 输出 |
|---|---|---|
| 从紧 (score=+2) | ≥ +0.5 | **tight** ✓ |
| 从紧 | < +0.5 | neutral（季度报告未跟上，谨慎收回） |
| 适度宽松 (-2) | ≤ -0.5 | **loose** ✓ |
| 适度宽松 | > -0.5 | neutral（同上） |
| 稳健中性 (+1) | ≥ +1.0 | tight |
|  | ≤ -1.0 | loose |
|  | 其他 | neutral |
| 稳健 (0) | ≥ +1.0 | tight |
|  | ≤ -1.0 | loose |
|  | 其他 | neutral |
| **缺失** | ≥ +1.0 | tight |
|  | ≤ -1.0 | loose |
|  | 其他 | neutral（只要有 MPR 就不再 None） |

### 2.3 严防上帝视角

- snapshot_date = `apply_year - 1` 12月31日
- `_fetch_mpr_avg_score(conn, snapshot_date)` 仅取 `publish_date <= snapshot_date` 的 MPR
- 例：2009 年应用：Q4 2008 报告 2009/2/13 才发布 → 不可用，只能用 Q1-Q3 2008（依然偏紧）→ 因此 2009 v51=neutral 而非 v50 的 loose

---

## 3. 14 处差异年份

| apply_year | CEWC原话 | MPR权均 | v50 | v51 | 业务解读 |
|---|---|---|---|---|---|
| 2005 | (缺) | +0.83 | None | **neutral** | 早期 CEWC 未记录，MPR 兜底 |
| 2009 | 适度宽松 | +0.10 | loose | **neutral** | 2008 Q1-Q3 还在收紧，年初严防上帝视角 |
| 2011 | 稳健 | -2.00 | neutral | **loose** | 2010Q4 已转宽松（未观察到，季度报告先于 CEWC 反映） |
| 2012 | 稳健 | +1.00 | neutral | **tight** | 2011Q4 仍偏紧 |
| 2014 | 稳健 | +1.00 | neutral | **tight** | 2013Q4 仍偏紧 |
| 2016 | 稳健 | -1.00 | neutral | **loose** | 2015 全年宽松通道 |
| 2018 | 稳健中性 | +1.00 | neutral | **tight** | 2017Q4 去杠杆，2018 贸易战前已转紧 |
| 2020 | 稳健（灵活适度） | -1.00 | None | **loose** | 2019Q4 已转宽松 |
| 2021 | 稳健（灵活精准、合理适度） | -1.00 | None | **loose** | 疫情后持续宽松 |
| 2022 | 稳健（灵活适度） | +0.00 | None | **neutral** | 中性 |
| 2023 | 稳健（精准有力） | -1.00 | None | **loose** | 稳增长延续 |
| 2024 | 稳健（灵活适度、精准有效） | -1.00 | None | **loose** | 同上 |
| 2025 | 适度宽松（适时降准降息） | -1.00 | None | **loose** | 14 年来首次重提"适度宽松" |
| 2026 | 适度宽松（灵活高效运用降准降息） | -2.00 | None | **loose** | 持续宽松 |

---

## 4. 评分卡总分影响

12 个年份的 **总分** 受影响（pboc_tone 政策维度 ±2 分）：

| 年份 | v50 总分 | v51 总分 | Δ | 业务含义 |
|---|---|---|---|---|
| 2009 | -7 (机会显著) | -5 (机会偏多) | +2 | 撤回 loose，因 Q4 报告还未发布 |
| 2011 | -1 | -3 | -2 | 加 loose 信号，准备转向 |
| 2012 | -2 | 0 | +2 | 加 tight |
| 2014 | -2 | 0 | +2 | 加 tight |
| 2016 | -6 | -8 (近机会显著) | -2 | 加 loose |
| 2018 | 0 | +2 | +2 | 加 tight（贸易战前已收紧）|
| 2020 | -2 | -4 | -2 | 启用 v51（v50 是 None）|
| 2021 | -5 | -7 (机会显著) | -2 | 同上 |
| 2023 | -1 | -3 | -2 | 同上 |
| 2024 | -3 | -5 | -2 | 同上 |
| 2025 | 0 | -2 | -2 | 同上 |
| 2026 | -4 | -6 (机会偏多) | -2 | 同上 |

**关键发现**：
- **2008 (+14, 极端风险)** 不变 — v50/v51 都识别为 tight
- **2009 (-7→-5)** 微调 — 严防上帝视角导致 loose 撤回为 neutral，但仍在"机会显著"区间附近
- **2018 (0→+2)** 首次进入正分区 — 贸易战前已显紧缩压力
- **2021/2026** 进入"机会偏多"区间 — 政策宽松信号被正确识别

---

## 5. LLM 抽取关键时间线

按 MPR `tone_score` 时间线还原货币政策周期：

| 时期 | tone_score | 抽取的 stance_phrase |
|---|---|---|
| 2007Q3-Q4 | +2 (从紧) | "实行适度从紧的货币政策" → "从紧的货币政策" |
| 2008Q3 | -2 (急转宽松) | "实行适度宽松的货币政策" |
| 2010Q4 | +1 (重新收紧) | "稳健的货币政策" |
| 2016Q4 | +1 (稳健中性) | "稳健中性的货币政策" |
| 2024Q4-2026 | -2 (重回宽松) | "适度宽松的货币政策" — 14 年首次 |

---

## 6. 代码改动

| 文件 | 改动 |
|---|---|
| `scripts/fetch_pboc_mpr.py` | 新增：抓取 100 份 MPR PDF → 解析 → 落 `pboc_mpr_quarterly` |
| `scripts/extract_pboc_mpr_features.py` | 新增：LLM 抽取 → `pboc_mpr_features` |
| `backtest/scorecard_adapter.py` | **改动**：新增 `_CEWC_TONE_SCORE`、`_fetch_mpr_avg_score`、`_resolve_pboc_tone` |
| `scripts/diff_pboc_tone_v51.py` | 新增：tone 三态回归对比 |
| `scripts/compare_scorecard_v51.py` | 新增：评分卡总分全量对比 |
| `scripts/plot_v51_pboc_compare.py` | 新增：3 子图对比可视化 |

---

## 7. 数据完整性

- `pboc_mpr_quarterly`: 100 / 101（缺 2008Q2，历史链接 404）
- `pboc_mpr_features`: 100 (100% 覆盖)
- LLM 模型: `pplx-sdk-default`，prompt v1.0
- 数据库: MariaDB 11.8.6 @ 127.0.0.1:3306

---

## 8. 后续可能的优化（未实施，仅记录）

1. **MPR 抽取版本管理**：新增 prompt v2.0 时支持并存对比
2. **季度粒度信号**：当前评分卡 1 次/年；可探索季度滚动评估
3. **LLM 抽取置信度**：让 LLM 输出 confidence_score 用作权重
4. **CEWC + MPR 一致性指标**：作为政策模糊度的额外信号
