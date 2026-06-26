# v5.0 评分卡 / 动能转换器输入数据来源审计

> 目的：审计我在 `run_2006_strategy.py` 里每个字段的数据来源，
> 区分"真实数据库查询" / "外部研究填入" / "需要补充的源"，
> 为 v5.0 在生产环境替换掉硬编码值提供路线图。

## 一、teststock 已有的真实数据源（MySQL）

| 表 | 内容 | 时间跨度 |
|---|---|---|
| `shibor_daily` | SHIBOR 隔夜/3M/1Y 利率 | 2006+ |
| `lpr_daily` | LPR 1Y/5Y | 2013-10+ |
| `libor_daily` | LIBOR/SOFR | 2006+ |
| `cpi_yoy` / `ppi_yoy` (price_index) | CPI/PPI 同比 | 长期 |
| `m1_m2` (money_supply) | M1/M2 同比 | 长期 |
| `pmi_monthly` | PMI 月度 | 2005+ |
| `social_financing` | 社融存量同比 | 2010+ |
| `cn_gdp_quarterly` | GDP 季度同比 | 长期 |
| `cewc_annual` | 中央经济工作会议货币口径 | 1994+ |
| `corpus_pboc_report` | 央行季度货币政策报告（原文）| 2001+ |
| `index_valuation_monthly` | 沪深 300 / 上证 50 / 中证 500 PE/PB | 2006-01+ |
| `margin_summary` | 两融余额 / 流通市值占比 | 2010-03+ |
| `us_treasury_daily` | 美债 10Y 名义/实际 | 2010+ |

**统一调用接口**: `macro.annual_snapshot.build_snapshot_for_year(conn, year)`
返回 `RateSnapshot` 对象，时点严格卡在 `(year-1)-12-31`。

## 二、字段对照表 — 2006 年初评估

### 评分卡输入 (ScorecardInputs)

| 字段 | 我填的值 | 真实来源 | 来源状态 |
|---|---|---|---|
| `cs300_pe_ttm` | 15.16 | `signals_db/valuation_monthly` (2006-01) | ✅ **真实查询** |
| `cs300_pb` | 1.83 | `signals_db/valuation_monthly` (2006-01) | ✅ **真实查询** |
| `rate_cum_bp_12m` | 0 | 应从 `shibor_daily` 计算 2005-12 vs 2004-12 差值 | ⚠️ **我手填**，可改为查询 |
| `rrr_cum_pp_12m` | 0 | 需要 PBOC 公告抓取（央行政策事件表）| ❌ **暂无表**，可补 |
| `deposit_1y_rate` | 2.25 | 央行 1Y 定存基准（公开资料）| ⚠️ **我手填**，需补表 |
| `pmi_below_52_months` | 0 | `pmi_monthly` 查询连续 <52 月数 | ⚠️ **我手填**，可改为查询 |
| `iva_yoy_trend` | "up" | `cn_gdp_quarterly.si_yoy` (第二产业同比) 趋势 | ⚠️ **我手填**，可改为查询 |
| `ppi_yoy` | 4.9 | `price_index.ppi_yoy` (2005-12) | ⚠️ **我手填**，可改为查询 |
| `ppi_yoy_change` | "flat" | 同上，计算趋势 | ⚠️ **我手填**，可改为查询 |
| `pmi_resume_expansion` | False | `pmi_monthly` 算法判断 | ⚠️ **我手填**，可改为查询 |
| `new_fund_billion` | 80 | tushare/wind 月发新基数据 | ❌ **暂无表** |
| `fund_doubling_6m` | False | 同上 | ❌ **暂无表** |
| `margin_growth_pct` | None | `margin_summary` (2010+) | ✅ **2006 不适用** |
| `fed_reversal` | None | `us_treasury_daily` 联邦基金利率轨迹 | ❌ **暂无表（联邦基金利率）** |
| `us_monthly_pct` | 2 | 标普500月线 / `us_index_daily`（接口不可用）| ❌ **暂无表** |
| `global_recession` | False | NBER 衰退标记（外部数据）| ❌ **暂无表** |
| `fed_zero_qe` | False | 公开信息 | ❌ **暂无表** |
| `global_stimulus` | False | 公开信息 | ❌ **暂无表** |
| `pboc_tone` | "neutral" | `corpus_pboc_report` + LLM 抽取 | ⚠️ **我手填**，已有原文表 |
| `stamp_duty` | None | 公开公告 | ❌ **暂无表** |
| `central_meeting_tone` | "neutral" | `cewc_annual.monetary_policy` | ⚠️ **我手填**，可改为查询 |

### 动能转换器输入 (MomentumState)

| 字段 | 我填的值 | 真实来源 | 来源状态 |
|---|---|---|---|
| `rate_cum_bp` | 0 | `shibor_daily` 累计差值 | ⚠️ **我手填** |
| `rate_hike_months` | 0 | `shibor_daily` 算法判断 | ⚠️ **我手填** |
| `first_tightening_hint` | False | `corpus_pboc_report` LLM 抽取 | ❌ **未实现 LLM 抽取** |
| `in_loose_phase` | False | 综合判断 | ❌ **未实现** |
| `m1_yoy` | 11.8 | `money_supply.m1_yoy` (2005-12) | ⚠️ **我手填**，可改为查询 |
| `new_fund_3m_over_1000` | False | 月发新基数据 | ❌ **暂无表** |
| `new_loan_over_1trn` | False | 社融分项（人民币贷款）| ⚠️ **可能在 social_financing 里** |
| `margin_to_float_pct` | None | `margin_summary` (2010+) | ✅ **2006 不适用** |
| `months_since_bear_bottom` | 6 | 算法判断（上证综指/沪深300 大底）| ❌ **未实现自动识别** |
| `pe_yoy_pct` | -5 | `valuation_monthly` 同比计算 | ⚠️ **我手填**，可改为查询 |
| `eps_yoy_pct` | 15 | 沪深 300 成分股 EPS 加权 | ❌ **暂无表** |

## 三、来源分类统计

| 类别 | 评分卡 | 动能 | 合计 |
|---|---:|---:|---:|
| ✅ 真实查询（已用）| 2 | 0 | 2 |
| ⚠️ 我手填但可改为查询 | 10 | 5 | 15 |
| ❌ 暂无表（需补充数据源）| 9 | 6 | 15 |

**结论**: 当前 v5.0 输入中只有 **2/32 = 6%** 是真正从数据库查的（沪深 300 PE/PB），
其余 **94% 是我基于历史知识手填**。这不可接受用于生产 / 完整回测。

## 四、补全路线图（优先级排序）

### P0 — 立即可补（数据已在 teststock，只需写适配层）
1. `cewc_annual` → `central_meeting_tone` （中央经济工作会议口径）
2. `corpus_pboc_report` → `pboc_tone` （央行报告口径 LLM 抽取）
3. `shibor_daily` + `lpr_daily` → `rate_cum_bp_12m`, `rate_hike_months`
4. `pmi_monthly` → `pmi_below_52_months`, `pmi_resume_expansion`
5. `price_index` → `ppi_yoy`, `ppi_yoy_change`
6. `money_supply` → `m1_yoy`
7. `index_valuation_monthly` → `pe_yoy_pct`
8. `cn_gdp_quarterly` → `iva_yoy_trend` （第二产业同比趋势）

→ **预计 P0 完成后，评分卡覆盖率可达 14/22 = 64%**

### P1 — 需新建数据表（teststock 没有但 tushare/外部可获取）
1. **存款准备金率历史表** — PBOC 公告抓取（可用 `scrape_pboc_reports.py` 扩展）
2. **存款基准利率历史表** — 同上
3. **月发新基金规模表** — tushare `fund_amount`
4. **联邦基金利率 + 美股月线** — 已有 `us_treasury_daily` 但缺联邦基金；美股月线缺
5. **沪深 300 季度 EPS 表** — tushare `index_dailybasic` 已含部分

### P2 — 需 LLM 抽取（半自动）
1. `first_tightening_hint` — 央行季度报告"微调/退出"措辞识别
2. `in_loose_phase` — 综合 cewc + pboc 判断
3. `months_since_bear_bottom` — 沪深 300 月线自动识别熊底

## 五、对当前 2006 评估结果的影响

我目前给出的 **75% 股票** 结论，关键驱动是：
- ✅ **PE<20 + PB<2** (来自真实数据) → -2 分
- ⚠️ **1Y定存<2.5%** (我填 2.25, 真实值需查) → -1 分
- ⚠️ **工业增加值回升** (我填 up, 真实数据 si_yoy 2005-Q4 ≈ +11.8%) → -1 分
- ⚠️ **新基<200亿** (我填 80, 真实数据需补) → -1 分

如果真实查询替换我的手填值，**核心结论 (-5 分) 大概率不变**，因为：
- 估值是硬数据，未变
- 利率水平公开可查，2.25% 是 2005-2014 实际利率
- 第二产业增速 2005-Q4 +11.8% 确为上行
- 新基 2005 实际月均 60-100 亿，<200 亿确定

但**动能转换器的 C=1.5 短反弹结论**有点弱：
- 我用"距 2005-07 上证综指 998 点 = 6 月"，但其实理性人 2006 年初不一定能确认那是"熊底"
- 完整实现需要算法识别熊底（比如沪深 300 跌至阶段最低且滚动 6 月未破）

## 六、建议下一步

如果你想让 v5.0 真正可投产，**Phase 3 的核心工作就是补这个数据适配层**：

```python
# 目标 API
from backtest.scorecard_adapter import build_scorecard_inputs

inputs = build_scorecard_inputs(
    conn=teststock_mysql_conn,
    apply_year=2006,
    valuation_db=signals_sqlite_conn,
)
result = evaluate_scorecard(2006, inputs)
```

适配器内部会调用 teststock 现成的 `build_snapshot_for_year` + 额外的查询逻辑，
把 22 个评分卡字段全部从真实数据填满（除少数 LLM 抽取项）。
