"""
scorecard.py — v3.4 双向评分卡

对应 portfolio/docs/反思_评分卡双向化.md。

核心思想：
- 单向评分（v3.3b）只计风险，导致底部所有维度都"看起来差"，错失加仓信号
- 双向评分（v3.4）每个维度同时有"风险加分"和"机会减分"
- 净评分 = Σ(风险) − Σ(机会)
- 评分高 → 风险占优 → 减仓；评分低 → 机会占优 → 加仓

六个维度：
- 估值（PE / PB） [-4, +4]
- 流动性（利率 / 存款准备金率） [-4, +4]
- 基本面（PMI / 工业增加值 / PPI） [-5, +5]
- 情绪（新基发行 / 公募规模 / 两融） [-3, +3]
- 外部（美联储 / 美股 / 全球宏观） [-4, +5]
- 政策（央行口径 / 印花税 / 中央会议） [-4, +4]

档位映射 [-16, +26] → 目标股票仓位 95% ~ 20%（v3.4.11 起 12 档加密阶梯）

在 v5.0 三层架构中的角色：
- Layer 1 战略层（年初）：评分卡校验 LLM Agent 的方向
- 也可在 Layer 2 关键节点（半年度）做仓位校准

红线：
- 不预言未来 — 评分只用当年已发生的信号
- 不修改阈值救场 — 维度阈值固化在代码里
- 加仓档需通过"政策实弹三重门"约束（保留 v3.3b 的机制）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# =====================================================
# 单项评分卡条目
# =====================================================
@dataclass
class ScoreItem:
    """单条评分规则的命中记录"""
    dimension: str          # 'valuation'/'liquidity'/'fundamental'/'sentiment'/'external'/'policy'
    name: str               # 信号名（如 'PE>50'）
    direction: str          # 'risk' 或 'opportunity'
    score: int              # 风险正分 / 机会负分


# =====================================================
# 评分输入（年初/年中评估时填入的宏观快照）
# =====================================================
@dataclass
class ScorecardInputs:
    """
    评分卡输入 — 应来自 teststock 的 MySQL 宏观表 + signals_db。

    所有字段都允许 None，表示该信号缺失（评分时跳过该条规则）。
    """
    # 估值
    cs300_pe_ttm: float | None = None
    cs300_pb: float | None = None

    # 流动性（累计基点，正为加息/加准，负为降息/降准）
    rate_cum_bp_12m: float | None = None      # 过去 12 月累计基点（正加息负降息）
    rrr_cum_pp_12m: float | None = None       # 过去 12 月累计 pp（正加准负降准）
    deposit_1y_rate: float | None = None       # 1 年定存利率 %

    # 基本面
    pmi_below_52_months: int | None = None    # PMI < 52 连续月数
    iva_yoy_trend: str | None = None          # 'down' / 'up' / 'flat'
    ppi_yoy: float | None = None              # PPI 同比 %
    ppi_yoy_change: str | None = None         # 'turn_negative' / 'turn_positive' / 'flat'
    pmi_resume_expansion: bool = False         # PMI 从 <50 回到 ≥50
    pmi_mfg_3m_avg: float | None = None       # 制造业 PMI 3 月滚动均值（消季节性，判过热）
    pmi_prod_minus_order: float | None = None  # 生产 − 新订单（库存/需求背离信号）
    pmi_non_mfg: float | None = None          # v10-R1: 非制造业 PMI（独立信号，ρ_12m=-0.325）

    # 情绪
    new_fund_billion: float | None = None      # 月发新基（亿元）
    new_fund_count: int | None = None          # 当月新成立基金只数（健康度过滤用）
    fund_doubling_6m: bool = False             # 公募规模半年翻倍（v5: 已废弃，错向率高）
    margin_growth_pct: float | None = None     # 两融余额增长 %（季度）

    # 外部
    fed_reversal: str | None = None            # 'hike_to_cut' / 'cut_to_hike' / None
    us_monthly_pct: float | None = None        # 美股月度涨跌幅 %
    global_recession: bool = False             # 主要经济体衰退确认
    fed_zero_qe: bool = False                  # 美联储零利率 + QE
    global_stimulus: bool = False              # 全球同步刺激（G20 共识，bool 旧规则）
    cb_cuts_6m: int | None = None              # v8: 近 6 月主要央行降息次数（FED/ECB/BOE/BOJ/PBOC）
    gold_yoy_pct: float | None = None          # v11-G: 黄金 YoY %（全球流动性代理）
    vix_30d_avg: float | None = None           # v11-V: VIX 月均（恐慌指数）
    fed_rate_level: float | None = None        # v11-F: FED rate 绝对水平 %
    us10y_chg_12m_bp: float | None = None      # v12-M4: 美 10Y 12 月变化 bp（>+100=紧缩周期风险）

    # 价格动量（v12-M2: 动量过滤）
    cs300_6m_return: float | None = None       # v12-M2: CS300 过去 6 月累计收益%（用于过滤"估值便宜但已大涨"的伪机会）

    # 估值分位（v12-M3: 双确认）
    cs300_pe_p20_60m: float | None = None      # v12-M3: PE 60 月滚动 P20（用于"真便宜"双确认）

    # 第一性原理 — ROE（v12-R1: 盈利能力层）
    roe_implied: float | None = None           # v12-R1: 隐含 ROE = PB / PE_TTM × 100（衡量企业实际盈利能力）
    roe_3y_trend: str | None = None            # v12-R1: ROE 3 年趋势 'rising'/'flat'/'declining'（决定 PE 信号的有效性）

    # 企业信心（v13-B1: 第一性原理新维度）
    enterprise_boom_index: float | None = None  # v13-B1: 企业景气指数（季度，100=荣枯线，<110→机会 74%命中）

    # 政策
    pboc_tone: str | None = None              # 'tight' / 'loose' / 'neutral'
    stamp_duty: str | None = None             # 'tighten' / 'loosen'
    central_meeting_tone: str | None = None    # 'dual_prevent' / 'expansionary' / 'neutral'
    national_team_action: str | None = None    # v3.4.9：'entry' / 'exit' / None — 国家队入场强信号
    property_policy: str | None = None         # v3.4.10：'tighten' / 'loosen' / None — 房地产政策大转向


# =====================================================
# 评分卡结果
# =====================================================
@dataclass
class ScorecardResult:
    year: int
    items: List[ScoreItem] = field(default_factory=list)
    total_score: int = 0
    target_equity_pct: float = 75.0
    band: str = ""
    notes: str = ""

    def items_by_dimension(self) -> dict[str, list[ScoreItem]]:
        d: dict[str, list[ScoreItem]] = {}
        for it in self.items:
            d.setdefault(it.dimension, []).append(it)
        return d


# =====================================================
# 各维度评分函数
# =====================================================
def score_valuation(inp: ScorecardInputs) -> List[ScoreItem]:
    """估值维度: [-4, +4]

    v12-R1 (2026-06 第一性原理): PE 信号须结合 ROE 趋势才有意义。
      - PE<15 + ROE 上升 → 真底部，保留 -2
      - PE<15 + ROE 平稳 → 中性便宜，-1
      - PE<15 + ROE 下降 → 估值陷阱，0 分（不打分）
    逻辑：股价 = EPS × PE，当 EPS（ROE）持续下滑时，
          PE"便宜"是假象，企业盈利能力在萎缩。
    """
    items = []
    pe = inp.cs300_pe_ttm
    pb = inp.cs300_pb
    if pe is not None:
        if pe > 50: items.append(ScoreItem("valuation", "PE>50", "risk", +2))
        elif pe > 40: items.append(ScoreItem("valuation", "PE>40", "risk", +1))
        if pe < 15:
            trend = inp.roe_3y_trend
            if trend == 'rising':
                # ROE 上升 + PE 便宜 = 真底部（第一性原理确认）
                items.append(ScoreItem("valuation", "PE<15+ROE上升(真底部)", "opportunity", -2))
            elif trend == 'declining':
                # ROE 下降 = 估值陷阱，PE 再低也不打分
                pass  # v12-R1: 不触发任何信号
            else:
                # ROE 平稳或数据缺失 → 保守打 -1
                items.append(ScoreItem("valuation", "PE<15+ROE平稳", "opportunity", -1))
    if pb is not None:
        pass  # v3.4.12 裁剪
    return items


def score_liquidity(inp: ScorecardInputs) -> List[ScoreItem]:
    """流动性维度: [-5, +4]

    v9-C (2026-06): 新增「累计降息>100bp + 累计降准>1pp 共振 → -1」(D+A+C 组合的 C 部分)
    """
    items = []
    if inp.rate_cum_bp_12m is not None:
        v = inp.rate_cum_bp_12m
        if v > 150: items.append(ScoreItem("liquidity", "累计加息>150bp", "risk", +1))
        if v > 100: items.append(ScoreItem("liquidity", "累计加息>100bp", "risk", +1))
        if v < -100: items.append(ScoreItem("liquidity", "累计降息>100bp", "opportunity", -2))
    if inp.rrr_cum_pp_12m is not None:
        v = inp.rrr_cum_pp_12m
        if v > 3: items.append(ScoreItem("liquidity", "累计加准>3pp", "risk", +1))
        if v < -1: items.append(ScoreItem("liquidity", "累计降准>1pp", "opportunity", -1))
    if inp.deposit_1y_rate is not None:
        r = inp.deposit_1y_rate
        if r > 3.5: items.append(ScoreItem("liquidity", "1Y定存>3.5%", "risk", +1))
        if r < 2.5: items.append(ScoreItem("liquidity", "1Y定存<2.5%", "opportunity", -1))
    # v9-C: 流动性共振规则
    # v3.4.12 裁剪：降息降准共振触发 2 次方向错（|t|=0.08，触发后均回报 +16.7% vs 未触发 +18.6%）
    # if (inp.rate_cum_bp_12m is not None and inp.rrr_cum_pp_12m is not None
    #         and inp.rate_cum_bp_12m < -100 and inp.rrr_cum_pp_12m < -1):
    #     items.append(ScoreItem("liquidity", "降息降准共振(双松)", "opportunity", -1))
    return items


def score_fundamental(inp: ScorecardInputs) -> List[ScoreItem]:
    """基本面维度: [-6, +5]（v3.4.1 PMI 子改进 + v9 D/A 反转/连续过滤）

    v9-D (2026-06): 「PMI 重回扩张」从 -2（机会）改为 +1（风险）— 基于 ML 分析与
      18 年回测验证：PMI 在 12-31 snapshot 重回 50 上方时，次月/年 CS300 反而下跌
      （滞后顶部信号）。19 月触发反转后 P&L +4.83 pp，回撤 -1.82 pp。
    v9-A (2026-06): 新增「PMI<52 连续 ≥6 月 → -1」深度收缩反弹机会信号。
      142 月触发，与 D 组合 P&L +6.90 pp。
    """
    items = []
    if inp.pmi_below_52_months and inp.pmi_below_52_months >= 2:
        items.append(ScoreItem("fundamental", "PMI<52连续2月", "risk", +1))
    # v3.4.12 裁剪：PMI<52 连续≥6 月（v9-A）触发 13 次方向错（|t|=0.06，触发后 +17.7% vs 未触发 +19.8%）
    # if inp.pmi_below_52_months and inp.pmi_below_52_months >= 6:
    #     items.append(ScoreItem("fundamental", "PMI<52连续≥6月(深度收缩)", "opportunity", -1))
    if inp.iva_yoy_trend == "down":
        items.append(ScoreItem("fundamental", "工业增加值下行", "risk", +1))
    if inp.iva_yoy_trend == "up":
        items.append(ScoreItem("fundamental", "工业增加值回升", "opportunity", -1))
    # v3.4.12 裁剪：PPI 转负触发 4 次方向错（|t|=0.25，触发后 +24% vs 未触发 +17%）
    # if inp.ppi_yoy_change == "turn_negative":
    #     items.append(ScoreItem("fundamental", "PPI转负", "risk", +2))
    # v3.4.12 裁剪：PPI 触底反弹触发 3 次方向错（触发后 -4% vs 未触发 +22%）
    # if inp.ppi_yoy_change == "turn_positive":
    #     items.append(ScoreItem("fundamental", "PPI触底反弹", "opportunity", -1))
    # v9-D 反转: PMI 重回扩张是滞后顶部信号
    if inp.pmi_resume_expansion:
        items.append(ScoreItem("fundamental", "PMI重回扩张(滞后顶)", "risk", +1))
    # v3.4.12 裁剪：PMI3M 均≥53 触发 5 次方向错（|t|=0.47，触发后 +34% vs 未触发 +13%）
    # v3.4.1: PMI 3 月均值过热（消季节性）
    # if inp.pmi_mfg_3m_avg is not None and inp.pmi_mfg_3m_avg >= 53.0:
    #     items.append(ScoreItem("fundamental", "PMI3M均≥53(景气过热)", "risk", +1))
    # v3.4.1: 生产 − 新订单 背离（库存/需求领先信号）
    if inp.pmi_prod_minus_order is not None:
        if inp.pmi_prod_minus_order >= 3.0:
            items.append(ScoreItem("fundamental", "生产>订单≥3(被动累库)", "risk", +1))
        if inp.pmi_prod_minus_order <= -3.0:
            items.append(ScoreItem("fundamental", "订单>生产≥3(需求领先)", "opportunity", -1))
    # v10-R1: 非制造业 PMI（独立信号，ρ_12m=-0.325 强反向）
    if inp.pmi_non_mfg is not None:
        if inp.pmi_non_mfg > 55:
            items.append(ScoreItem("fundamental", "非制造业PMI>55(过热)", "risk", +1))
        if inp.pmi_non_mfg < 50:
            items.append(ScoreItem("fundamental", "非制造业PMI<50(收缩→反弹)", "opportunity", -1))
    # v13-B1: 企业景气指数（季度，前向填充到月度）
    # 景气 < 110 → 74% 命中次4季涨，均涨 +15.1%（数据验证，2005Q1起）
    ebi = inp.enterprise_boom_index
    if ebi is not None:
        if ebi < 110:
            items.append(ScoreItem("fundamental", "企业景气<110(低信心底部)", "opportunity", -1))
    return items


def score_sentiment(inp: ScorecardInputs) -> List[ScoreItem]:
    """情绪维度: [-3, +3]

    v5 (基于 2008-2025 18 年回测探索后的精简规则):
      - 月发新基绝对值的「过热阈值 >1500」和「6M翻倍」在牛市初期结构性错向（5/8 错），已废弃
      - 仅保留「冰点 <200 → -1」作为反向情绪信号（历史 100% 命中率：2009/2012）
      - 加「健康度过滤」：new_fund_count < 5 时跳过，避免监管暂停期（如 2007Q4）的伪冰点信号

    v6 (两融, 2026-06 探索后的精简规则):
      - 旧规则 >50/+1、<-30/-1 在 15 年回测中仅 40% 命中（过热信号牛市初期错向）
      - 仅保留「冰点 YoY < -20% → -1」（2017/2019 触发 2/2 = 100% 对方向）
    """
    items = []
    nf = inp.new_fund_billion
    nc = inp.new_fund_count
    # 月发新基冰点 — 需 fund_count >= 5 才信任，避免监管伪信号
    if nf is not None and nf < 200 and (nc is None or nc >= 5):
        items.append(ScoreItem("sentiment", "月发新基<200亿(健康)", "opportunity", -1))
    # 两融冰点 — YoY 大幅下行表示杠杆资金离场，反向机会
    mg = inp.margin_growth_pct
    if mg is not None and mg < -20:
        items.append(ScoreItem("sentiment", "两融YoY<-20%(冰点)", "opportunity", -1))
    return items


def score_external(inp: ScorecardInputs) -> List[ScoreItem]:
    """外部维度: [-5, +5]

    v8 (2026-06): 新增 cb_cuts_6m >= 3 → -1 规则（基于 ML RF 特征重要性 0.100 top 4）。
    ML 探索 + 18 年回测：触发 17 月，next_1m 平均 +3.9%，71% 命中（2008Q4-2009 / 2024-2025 集中）；
    严格红线 3/3 通过：累计回报 +1.42 pp、回撤持平、Spearman ρ 从 -0.194 → -0.199。
    """
    items = []
    if inp.fed_reversal == "hike_to_cut":
        # 注意：加息转降息对中国资产是利好（流动性宽松预期），但短期波动剧烈，仍计风险 +2
        items.append(ScoreItem("external", "美联储加→降反转", "risk", +2))
    if inp.us_monthly_pct is not None:
        if inp.us_monthly_pct < -5:
            items.append(ScoreItem("external", "美股月跌>5%", "risk", +1))
        # v3.4.12 裁剪：美股月涨>5% 触发 1 次方向错（2010-12 触发后 2011 -26.5%，t=0 不显著）
        # if inp.us_monthly_pct > 5:
        #     items.append(ScoreItem("external", "美股月涨>5%", "opportunity", -1))
    if inp.global_recession:
        items.append(ScoreItem("external", "主要经济体衰退", "risk", +2))
    if inp.fed_zero_qe:
        items.append(ScoreItem("external", "美联储零利率+QE", "opportunity", -2))
    if inp.global_stimulus:
        items.append(ScoreItem("external", "全球同步刺激", "opportunity", -1))
    # v8: 全球央行 6 月内同步降息 (FED/ECB/BOE/BOJ/PBOC ≥3 家)
    if inp.cb_cuts_6m is not None and inp.cb_cuts_6m >= 3:
        items.append(ScoreItem("external", "全球央行6M内≥3家降息", "opportunity", -1))
    # v11-G: 黄金 YoY > 25% → 全球流动性放水代理（89% 命中 17/19）
    if inp.gold_yoy_pct is not None and inp.gold_yoy_pct > 25:
        items.append(ScoreItem("external", "黄金YoY>25%(全球放水)", "opportunity", -1))
    # v11-V: VIX 月均 > 30 → 极端恐慌底反弹（80% 命中 16/20）
    if inp.vix_30d_avg is not None and inp.vix_30d_avg > 30:
        items.append(ScoreItem("external", "VIX月均>30(恐慌底)", "opportunity", -1))
    # v11-F: FED rate ≥ 4.5 → 紧缩末段反转预期（71% 涨）
    if inp.fed_rate_level is not None and inp.fed_rate_level >= 4.5:
        items.append(ScoreItem("external", "FED利率≥4.5(紧缩末段)", "opportunity", -1))
    # v12-M4: 美 10Y 12 月升幅 > 100bp → 美债收紧周期 = A 股资金外流压力
    if inp.us10y_chg_12m_bp is not None and inp.us10y_chg_12m_bp > 100:
        items.append(ScoreItem("external", "美10Y升>100bp(紧缩周期)", "risk", +1))
    return items


def score_policy(inp: ScorecardInputs) -> List[ScoreItem]:
    """政策维度: [-4, +4]"""
    items = []
    if inp.pboc_tone == "tight":
        items.append(ScoreItem("policy", "央行口径从紧", "risk", +2))
    if inp.pboc_tone == "loose":
        items.append(ScoreItem("policy", "央行口径宽松", "opportunity", -2))
    if inp.stamp_duty == "tighten":
        items.append(ScoreItem("policy", "印花税/IPO收紧", "risk", +1))
    if inp.stamp_duty == "loosen":
        items.append(ScoreItem("policy", "印花税/IPO放松", "opportunity", -1))
    if inp.central_meeting_tone == "dual_prevent":
        items.append(ScoreItem("policy", "中央会议双防", "risk", +1))
    if inp.central_meeting_tone == "expansionary":
        items.append(ScoreItem("policy", "中央会议积极宽松", "opportunity", -1))
    # v3.4.9 国家队入场 (v12-M1 反死锚: -2 → -1，避免长期持有时拖累总分)
    if inp.national_team_action == "entry":
        items.append(ScoreItem("policy", "国家队入场", "opportunity", -1))
    if inp.national_team_action == "exit":
        items.append(ScoreItem("policy", "国家队减持", "risk", +1))
    # v3.4.10 新增：房地产政策大转向（discrete，强度 ±1 与 stamp_duty 同级）
    if inp.property_policy == "loosen":
        items.append(ScoreItem("policy", "房地产政策放松", "opportunity", -1))
    if inp.property_policy == "tighten":
        items.append(ScoreItem("policy", "房地产政策收紧", "risk", +1))
    return items


# =====================================================
# 档位映射（评分 → 目标股票仓位 %）
# =====================================================
def score_to_target_equity(score: int) -> tuple[float, str]:
    """评分 → (目标股票仓位 %, 档位描述)

    v3.4.13 C 全光谱映射（取代 v3.4.11 12 档）：极端档位拉伸到 100% / 0%，
    中间档同步扩大间距，让评分卡的方向判断在 P&L 上得到更充分体现。

      - 加仓侧：100%（≤-10）/ 95% / 90% / 85% / 80%（score==0）
      - 减仓侧：65%（≤+3）/ 50% / 35% / 15% / 0%（>+12）
      - 21 年回测：终值 908→1171 万 (+263 万)、年化 +11.08%→+12.43%（+1.35pp）、
        MDD -32.13%→-29.45%（同步改善 +2.67pp），Sharpe 0.233→0.256
      - 极端档触发：score≤-10 只在 2009 触发 1 次；score>+12 未触发；
        100% 满仓门槛严格，避免过早 all-in
      - 回测脚本：scripts/backtest_aggressive_mapping.py
      - 详见 docs/v50_scorecard_spec.md §十一 v3.4.13
    """
    if score <= -10:
        return 100.0, "极度便宜+刺激共振"
    if score <= -7:
        return 95.0, "深度机会"
    if score <= -4:
        return 90.0, "机会显著"
    if score <= -1:
        return 85.0, "机会偏多"
    if score == 0:
        return 80.0, "平衡偏多"
    if score <= 3:
        return 65.0, "中性偏防"
    if score <= 6:
        return 50.0, "风险偏多"
    if score <= 9:
        return 35.0, "风险显著"
    if score <= 12:
        return 15.0, "高风险"
    return 0.0, "极端风险"


def momentum_filter_equity(target_eq: float, cs300_6m_return: float | None) -> tuple[float, str]:
    """v12-M2 动量过滤 — 解决"估值便宜但已大涨"的伪机会问题

    评分卡给加仓档（target_eq >= 80）时，加 CS300 6 月动量过滤：
      - 6M 涨幅 > +25% → 牛市顶部假信号，仓位下降一档（避免追高）
      - 6M 涨幅 > +15% → 略下降（中性偏多）
      - 6M 涨幅 < -10% → 真正底部，完全放行
      - 中间区间不动

    设计意图：评分卡 2014-2015 杠杆牛、2017-2018 慢牛顶、2021 抱团顶
    都是"估值便宜 + 政策利好 + 已大涨"的伪机会，动量过滤可识别。
    """
    if target_eq < 80 or cs300_6m_return is None:
        return target_eq, ''
    if cs300_6m_return > 25:
        # 强趋势顶部 → 降两档
        new_eq = max(60.0, target_eq - 25)
        return new_eq, f' (6M+{cs300_6m_return:.0f}% 强势顶 →降至 {new_eq:.0f}%)'
    if cs300_6m_return > 15:
        # 中势 → 降一档
        new_eq = max(65.0, target_eq - 15)
        return new_eq, f' (6M+{cs300_6m_return:.0f}% 中势顶 →降至 {new_eq:.0f}%)'
    return target_eq, ''


# =====================================================
# 主入口
# =====================================================
def evaluate_scorecard(year: int, inputs: ScorecardInputs) -> ScorecardResult:
    """六维双向评分，返回总分 + 目标仓位 + 详细命中项"""
    items: List[ScoreItem] = []
    items += score_valuation(inputs)
    items += score_liquidity(inputs)
    items += score_fundamental(inputs)
    items += score_sentiment(inputs)
    items += score_external(inputs)
    items += score_policy(inputs)

    total = sum(it.score for it in items)
    target_equity, band = score_to_target_equity(total)

    return ScorecardResult(
        year=year,
        items=items,
        total_score=total,
        target_equity_pct=target_equity,
        band=band,
        notes=f"v3.4 双向评分卡 — {len(items)} 条命中",
    )


def policy_triple_gate(inputs: ScorecardInputs) -> tuple[bool, str]:
    """
    'V3.3b 加仓三重门' — 防止过早抄底。

    评分到加仓档（target_equity >= 80%）时，需通过：
    1) 央行口径已转宽松（pboc_tone == 'loose'）
    2) 中央经济会议定调积极（central_meeting_tone == 'expansionary'）
    3) PPI 已触底反弹 或 PMI 已重回扩张（基本面有真实改善信号）

    满足任意两条 → 放行加仓
    都不满足或只满足 1 条 → 维持原档位
    """
    hits = []
    if inputs.pboc_tone == "loose":
        hits.append("央行宽松")
    if inputs.central_meeting_tone == "expansionary":
        hits.append("中央积极")
    # v9-D 后：pmi_resume_expansion 已改为风险信号（+1），不再算"触底反弹"
    # 第三道闸门改为仅看 ppi_yoy_change == 'turn_positive' 作为基本面真触底
    if inputs.ppi_yoy_change == "turn_positive":
        hits.append("PPI触底反弹")

    passed = len(hits) >= 2
    return passed, " + ".join(hits) if hits else "无政策实弹"
