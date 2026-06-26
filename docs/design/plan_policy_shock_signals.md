# PLAN: LLM 驱动的政策事件信号 → 评分卡外部维度强化

> 目标：建立「重大外部政策/市场事件」信号库，接入评分卡，解决贸易战(2018)/制裁/地缘政治等结构性盲点
> 预期效果：中期(2012-2018) ρ_12m 从 +0.197 → 负值（方向修正）
> 工期：P0 验证 约 2-4 小时

---

## 背景

评分卡在中期(2012-2018)的 ρ_12m = +0.197（严重反向），根因诊断：
- 2013 钱荒：短暂流动性冲击，对长期投资无影响（假问题，已排除）
- **2018 贸易战**：改变企业盈利预期，传导 6 个月，评分卡完全没有捕捉
- **2022 俄乌战争 + 房地产暴雷**：同类问题
- 所有月度宏观指标（M1/PMI出口新订单）都滞后于或同步于股市，不能作为领先信号

关键洞察：这类事件无法用宏观数据预测，但可以在**事件发生后**通过信号注册阻止评分卡继续发出错误的加仓信号。

---

## 架构

```
信息采集（手工 seed / 未来 LLM 自动化）
  ↓
external_policy_shocks 表（结构化事件 + 方向 + 强度 + 持续期）
  ↓
scorecard.py score_external 读取 → 在 snapshot 前 duration_months 内有 bearish 事件 → 风险加分
  ↓
回测验证 → 看 2018/2022-2023 是否改善
```

---

## Step 1: 建表 Schema

文件：`sql/external_policy_shocks_schema.sql`

```sql
CREATE TABLE IF NOT EXISTS external_policy_shocks (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    event_date      DATE           NOT NULL  COMMENT '事件发生日',
    event_type      VARCHAR(30)    NOT NULL  COMMENT 'trade_war / sanction / geopolitics / domestic_crisis / pandemic',
    direction       VARCHAR(10)    NOT NULL  COMMENT 'bearish / bullish',
    magnitude       TINYINT        NOT NULL  COMMENT '强度 1(轻微) / 2(中等) / 3(重大)',
    duration_months TINYINT        NOT NULL  COMMENT '预计影响持续月数',
    affected_dim    VARCHAR(20)    DEFAULT 'external' COMMENT '影响评分卡维度: external / fundamental / all',
    title           VARCHAR(200)   NOT NULL  COMMENT '事件标题',
    source          VARCHAR(50)    NULL      COMMENT '信号来源: manual / llm / news_scraper',
    confidence      DECIMAL(3,2)   DEFAULT 1.00 COMMENT '信号置信度 0-1',
    notes           TEXT           NULL,
    created_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_shock_date (event_date),
    KEY idx_shock_type (event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='重大外部政策/市场冲击事件信号库（手工+LLM自动）';
```

---

## Step 2: 手工录入 2012-2025 重大事件 Seed

文件：`data/external_policy_shocks_seed.csv`

录入原则：
- 只录入**实质性影响 A 股超过 1 个月**的事件
- 不录入钱荒类短暂流动性脉冲（已确认是假问题）
- 每个事件标注 direction / magnitude / duration_months

**核心事件清单（约 25-30 个）**：

| event_date | event_type | direction | magnitude | duration | title |
|---|---|---|---|---|---|
| 2013-06-20 | domestic_crisis | bearish | 1 | 1 | 银行间钱荒（短暂冲击，低强度） |
| 2015-06-15 | domestic_crisis | bearish | 3 | 6 | A股股灾（杠杆熔断） |
| 2015-08-11 | domestic_crisis | bearish | 2 | 3 | 811汇改（人民币急贬） |
| 2016-01-04 | domestic_crisis | bearish | 2 | 3 | 熔断机制启动 |
| 2016-11-09 | geopolitics | bearish | 1 | 3 | 特朗普当选（不确定性） |
| 2018-03-22 | trade_war | bearish | 3 | 12 | 中美贸易战开启（301备忘录） |
| 2018-07-06 | trade_war | bearish | 2 | 9 | 第一批关税正式生效 |
| 2018-09-24 | trade_war | bearish | 2 | 6 | 第三批2000亿关税 |
| 2019-05-10 | trade_war | bearish | 2 | 6 | 贸易战升级（2000亿→25%） |
| 2019-08-01 | trade_war | bearish | 2 | 4 | 新增3000亿关税威胁 |
| 2020-01-23 | pandemic | bearish | 3 | 3 | 武汉封城（COVID爆发） |
| 2020-03-09 | geopolitics | bearish | 2 | 2 | 全球股市熔断（美股） |
| 2021-07-24 | domestic_crisis | bearish | 2 | 12 | 教培行业双减政策 |
| 2021-09-20 | domestic_crisis | bearish | 3 | 12 | 恒大债务危机爆发 |
| 2022-02-24 | geopolitics | bearish | 2 | 6 | 俄乌战争爆发 |
| 2022-03-14 | domestic_crisis | bearish | 3 | 4 | 上海封控开始 |
| 2022-10-16 | geopolitics | bearish | 2 | 3 | 中美芯片制裁加码 |
| 2023-08-17 | domestic_crisis | bearish | 2 | 6 | 碧桂园债务危机 |
| 2024-09-24 | domestic_crisis | bullish | 3 | 6 | 924政策大反转 |
| 2025-04-02 | trade_war | bearish | 3 | 6 | 特朗普全球关税战2.0 |
| 2025-04-08 | domestic_crisis | bullish | 2 | 6 | 中央汇金+国资委维稳声明 |

---

## Step 3: 写 Import 脚本

文件：`scripts/import_external_policy_shocks.py`

功能：
1. 建表（如不存在）
2. 从 `data/external_policy_shocks_seed.csv` 读取
3. Upsert 到 MySQL
4. 打印入库统计 + 校验

---

## Step 4: ScorecardInputs 加字段 + score_external 加规则

### 4.1 新增字段

在 `backtest/scorecard.py` 的 `ScorecardInputs` 加：

```python
# 外部政策冲击信号（LLM 驱动）
policy_shock_score: float | None = None  # snapshot 前 N 月内有效事件的加权得分
```

### 4.2 score_external 新增规则

```python
# LLM 政策事件覆盖层
if inp.policy_shock_score is not None:
    if inp.policy_shock_score > 0:
        items.append(ScoreItem("external", f"政策冲击+{inp.policy_shock_score:.0f}", "risk", +int(inp.policy_shock_score)))
    if inp.policy_shock_score < 0:
        items.append(ScoreItem("external", f"政策利好{inp.policy_shock_score:.0f}", "opportunity", int(inp.policy_shock_score)))
```

### 4.3 计算逻辑（在 adapter 或 backtest 脚本中）

```python
def compute_policy_shock_score(cur, snapshot_date: date) -> float:
    """
    取 snapshot_date 前 6 个月内的所有有效事件，
    按时间衰减 + 强度加权求和。

    衰减模型：线性衰减
      event_age_months = (snapshot_date - event_date) / 30
      if event_age_months > duration_months: weight = 0
      else: weight = 1 - event_age_months / duration_months

    得分 = Σ (direction_sign × magnitude × weight × confidence)
      direction_sign: bearish = +1, bullish = -1

    返回值 [-3, +3] 区间裁剪
    """
    cur.execute("""
        SELECT event_date, direction, magnitude, duration_months, confidence
        FROM external_policy_shocks
        WHERE event_date BETWEEN %s AND %s
    """, (snapshot_date - timedelta(days=365), snapshot_date))

    score = 0.0
    for event_date, direction, magnitude, duration, confidence in cur.fetchall():
        age_months = (snapshot_date - event_date).days / 30.0
        if age_months > duration:
            continue
        weight = 1.0 - age_months / duration
        sign = 1.0 if direction == 'bearish' else -1.0
        score += sign * magnitude * weight * (confidence or 1.0)

    return max(-3.0, min(3.0, round(score)))
```

---

## Step 5: 回测验证

文件：`scripts/backtest_policy_shocks.py`

### 5.1 回测设计

- baseline: v11 评分卡（不含 policy_shock_score）
- candidate: v11 + policy_shock_score
- 窗口: 2012-2025（重点看中期 2012-2018 是否改善）

### 5.2 红线判定

- 中期 2012-2018 ρ_12m 从 +0.197 → 负值（必须反转方向）
- 近期 2019-2025 ρ_12m 不退化（≤ 当前 -0.342）
- 累计 P&L ≥ baseline
- 最大回撤 ≤ baseline + 3pp

### 5.3 关键验证场景

| 年份 | 事件 | 预期效果 |
|---|---|---|
| 2018 | 贸易战 bearish/3/12月 → 外部 +2~3 | 总分从 -6 提升到 -3 → 减少错误加仓 |
| 2022 | 俄乌+封控 bearish/3/4月 → 外部 +2 | 总分从 -5 提升到 -3 |
| 2024-09 | 924反转 bullish/3/6月 → 外部 -2 | 强化加仓信号 |
| 2025-04 | 关税战2.0 bearish/3/6月 → 外部 +2 | 新事件实时响应 |

---

## Step 6: 可视化

文件：`scripts/visualize_policy_shocks.py`

子图：
1. 事件时间线（类型/方向/强度 标注在 CS300 走势图上）
2. policy_shock_score 时序 vs CS300 对比
3. 三段 ρ_12m 柱状对比（baseline vs candidate）

---

## 未来扩展（P1-P3）

### P1: LLM 自动分析（Claude API）

```python
def analyze_event_with_llm(text: str, event_date: str) -> dict:
    """
    输入：新闻文本
    输出：{direction, magnitude, duration_months, affected_dim, reasoning}

    Prompt 关键结构：
    - 你是 A 股宏观分析师
    - 分析这个事件对 A 股未来 6 个月的影响
    - 回答：是否会压缩企业盈利？是否会推高资金成本？是否有持续性？
    - 输出 JSON
    """
```

### P2: 新闻源自动抓取

- 财新网 / 新华社 / 证监会公告
- 设置关键词过滤（贸易/关税/制裁/地缘/封控/暴雷/违约...）
- 每日抓取 → 触发 LLM 分析 → 自动入库

### P3: 闭环验证

- 每季度自动计算历史信号的「事后准确率」
- 准确率 < 50% 的信号类型降低置信度
- 高准确率的信号类型提升权重

---

## 文件清单

| 文件 | 说明 |
|---|---|
| `sql/external_policy_shocks_schema.sql` | 建表 |
| `data/external_policy_shocks_seed.csv` | 手工种子数据（25-30 事件）|
| `scripts/import_external_policy_shocks.py` | import 脚本 |
| `backtest/scorecard.py` | 加 policy_shock_score 字段 + 规则 |
| `scripts/backtest_policy_shocks.py` | 回测脚本（baseline vs candidate）|
| `scripts/visualize_policy_shocks.py` | 可视化 |

---

## 执行顺序

```
1. 建表 → import seed → 验证数据
2. 改 scorecard.py（ScorecardInputs + score_external）
3. 写 backtest 脚本
4. 跑回测 → 看红线判定
5. 通过 → 合入 main；不通过 → 调整事件强度/持续期
```
