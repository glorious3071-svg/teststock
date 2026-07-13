# 年度全 CSI 指数推荐 — 设计方案

> 目标：结合政策信号、新闻语义、动量/估值，每年初输出 CSI 指数推荐排行，并可回测验证。

---

## 一、问题定义

**输入（截至 Y 年 1 月 1 日，防上帝视角）：**

| 层 | 数据源 | 表 |
|----|--------|-----|
| 政策 | CEWC + 发改委政策 | `annual_sector_signals` |
| 新闻 | 财经快讯/研报 LLM 抽取 | `news_extraction` → `theme_news_signals` |
| 市场 | 指数行情/估值 | `index_daily`, `index_dailybasic` |
| 映射 | 题材 → 指数 | `theme_index_map`（176 个 CSI） |
| 宏观 | 利率/PMI 等（总仓位） | `macro_annual_snapshot` + `scorecard` |

**输出：**

- `csi_annual_recommendation` 表：Y 年 Top-N CSI 指数 + 分项得分
- 控制台/Markdown 报告
- 可选：映射到 `passive_etf` 可交易标的

---

## 二、架构（四层）

```
Layer 0  宇宙    index_daily 中 930/931.CSI（176 个）
Layer 1  信号    annual_sector_signals + theme_news_signals
Layer 2  映射    theme_index_map（17 canonical themes）
Layer 3  评分    composite = w_p·政策 + w_n·新闻 + w_m·动量 + w_v·估值
Layer 4  输出    Top-N + 相关去重 + 写入 DB / 报告
```

### 综合评分公式

```
policy_score  = strength(强3/中2/弱1) × relevance(强3/中2/弱1) × duration_mult
news_score    = z(Σ bullish − Σ bearish) 按 theme 聚合，再 × relevance
momentum      = 125 交易日涨跌幅百分位
valuation     = 1 − PB 5Y 百分位（越低越便宜分越高）

final = w_policy·norm(policy) + w_news·norm(news) + w_mom·norm(mom) + w_val·val
```

**默认权重：** 政策 35% · 新闻 15% · 动量 30% · 估值 20%  
（无新闻数据时自动降为：政策 50% · 动量 30% · 估值 20%）

---

## 三、新闻 → 题材信号

### 聚合窗口

- 年度推荐基准日 `as_of_date = Y-01-01`
- 新闻窗口：`[Y-1-07-01, Y-1-12-31]`（下半年政策/产业资讯对年初布局更相关）
- 严格截断：`news_article.pub_time <= as_of_date`

### 聚合规则（`theme_news_signals`）

```python
net_score(theme) = Σ (sign × magnitude × confidence)
  sign = +1 bullish, -1 bearish, 0 neutral
article_count = 命中该 theme 的条数
```

---

## 四、验证方案

### V1 截面验证（历史年份）

对 `annual_sector_signals` 已有年份（2012–2025）：

1. 在 `Y-01-01` 跑 `rank_annual_csi.py`
2. 计算各 CSI **12 个月 forward return**（至 `Y-12-31`）
3. 指标：
   - Spearman(final_score, forward_return)
   - Top-10 vs Bottom-10 平均收益差
   - Top-10 vs 000300.SH 超额

**红线：** ρ_12m > 0 且 Top-Bottom spread > 0（方向正确）

### V2 消融对比

| 配置 | 说明 |
|------|------|
| baseline | 仅政策 + 动量 + 估值（= 原 rank_2026 逻辑） |
| +news | 加入 theme_news_signals |
| full | + 主题持续时长乘数（backtest_long 逻辑） |

### V3 实盘（2026）

- 输出 Top-20 CSI 推荐表
- 与 `annual_direction` Agent ETF 配置交叉验证

---

## 五、执行入口

```bash
# 1. 聚合新闻题材信号（窗口截止 2025-12-31）
python scripts/aggregate_theme_news_signals.py --year 2026

# 2. 年度 CSI 排行
python scripts/rank_annual_csi.py --year 2026 --top 30

# 3. 一键：聚合 + 排行 + 写库
python scripts/run_annual_csi_recommendation.py --year 2026

# 4. 历史验证
python scripts/validate_csi_rank.py --from 2015 --to 2024
```

---

## 六、与现有模块关系

| 现有 | 本方案 |
|------|--------|
| `rank_2026q1_indices.py` | 泛化为 `rank_annual_csi.py`（参数化年份 + 新闻层） |
| `backtest_long.select_indices()` | 验证脚本复用其 duration/去重逻辑（Phase 2） |
| `annual_direction` Agent | 读取 `csi_annual_recommendation` 作为 ETF 映射参考 |
| `news_extraction` | 经 `aggregate_theme_news_signals.py` 汇入评分 |

---

## 七、实施阶段

| Phase | 内容 | 准出 |
|-------|------|------|
| P1 | `theme_news_signals` + 聚合脚本 | 2026 年有 theme 级 net_score |
| P2 | `rank_annual_csi.py` + 写库 | 输出 176 CSI 排名 |
| P3 | `validate_csi_rank.py` | 历史 ρ_12m 报告 |
| P4 | 一键 orchestrator | `run_annual_csi_recommendation.py` |
| P5 | Agent 接入（可选） | context.py 加载推荐 Top-10 |
