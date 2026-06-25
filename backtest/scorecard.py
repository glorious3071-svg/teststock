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
- 基本面（PMI / 工业增加值 / PPI） [-4, +4]
- 情绪（新基发行 / 公募规模 / 两融） [-3, +3]
- 外部（美联储 / 美股 / 全球宏观） [-4, +5]
- 政策（央行口径 / 印花税 / 中央会议） [-4, +4]

档位映射 [-15, +25] → 目标股票仓位 90% ~ 20%

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

    # 情绪
    new_fund_billion: float | None = None      # 月发新基（亿元）
    fund_doubling_6m: bool = False             # 公募规模半年翻倍
    margin_growth_pct: float | None = None     # 两融余额增长 %（季度）

    # 外部
    fed_reversal: str | None = None            # 'hike_to_cut' / 'cut_to_hike' / None
    us_monthly_pct: float | None = None        # 美股月度涨跌幅 %
    global_recession: bool = False             # 主要经济体衰退确认
    fed_zero_qe: bool = False                  # 美联储零利率 + QE
    global_stimulus: bool = False              # 全球同步刺激（G20 共识）

    # 政策
    pboc_tone: str | None = None              # 'tight' / 'loose' / 'neutral'
    stamp_duty: str | None = None             # 'tighten' / 'loosen'
    central_meeting_tone: str | None = None    # 'dual_prevent' / 'expansionary' / 'neutral'


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
    """估值维度: [-4, +4]"""
    items = []
    pe = inp.cs300_pe_ttm
    pb = inp.cs300_pb
    if pe is not None:
        if pe > 50: items.append(ScoreItem("valuation", "PE>50", "risk", +2))
        elif pe > 40: items.append(ScoreItem("valuation", "PE>40", "risk", +1))
        elif pe > 30: items.append(ScoreItem("valuation", "PE>30", "risk", +1))
        if pe < 15: items.append(ScoreItem("valuation", "PE<15", "opportunity", -2))
        elif pe < 20: items.append(ScoreItem("valuation", "PE<20", "opportunity", -1))
    if pb is not None:
        if pb > 3: items.append(ScoreItem("valuation", "PB>3", "risk", +1))
        if pb < 2: items.append(ScoreItem("valuation", "PB<2", "opportunity", -1))
    return items


def score_liquidity(inp: ScorecardInputs) -> List[ScoreItem]:
    """流动性维度: [-4, +4]"""
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
    return items


def score_fundamental(inp: ScorecardInputs) -> List[ScoreItem]:
    """基本面维度: [-4, +4]"""
    items = []
    if inp.pmi_below_52_months and inp.pmi_below_52_months >= 2:
        items.append(ScoreItem("fundamental", "PMI<52连续2月", "risk", +1))
    if inp.iva_yoy_trend == "down":
        items.append(ScoreItem("fundamental", "工业增加值下行", "risk", +1))
    if inp.iva_yoy_trend == "up":
        items.append(ScoreItem("fundamental", "工业增加值回升", "opportunity", -1))
    if inp.ppi_yoy_change == "turn_negative":
        items.append(ScoreItem("fundamental", "PPI转负", "risk", +2))
    if inp.ppi_yoy_change == "turn_positive":
        items.append(ScoreItem("fundamental", "PPI触底反弹", "opportunity", -1))
    if inp.pmi_resume_expansion:
        items.append(ScoreItem("fundamental", "PMI重回扩张", "opportunity", -2))
    return items


def score_sentiment(inp: ScorecardInputs) -> List[ScoreItem]:
    """情绪维度: [-3, +3]"""
    items = []
    nf = inp.new_fund_billion
    if nf is not None:
        if nf > 1500: items.append(ScoreItem("sentiment", "月发新基>1500亿", "risk", +1))
        if nf < 200: items.append(ScoreItem("sentiment", "月发新基<200亿", "opportunity", -1))
    if inp.fund_doubling_6m:
        items.append(ScoreItem("sentiment", "公募半年翻倍", "risk", +1))
    mg = inp.margin_growth_pct
    if mg is not None:
        if mg > 50: items.append(ScoreItem("sentiment", "两融增长>50%", "risk", +1))
        if mg < -30: items.append(ScoreItem("sentiment", "两融下降>30%", "opportunity", -1))
    return items


def score_external(inp: ScorecardInputs) -> List[ScoreItem]:
    """外部维度: [-4, +5]"""
    items = []
    if inp.fed_reversal == "hike_to_cut":
        # 注意：加息转降息对中国资产是利好（流动性宽松预期），但短期波动剧烈，仍计风险 +2
        items.append(ScoreItem("external", "美联储加→降反转", "risk", +2))
    if inp.us_monthly_pct is not None:
        if inp.us_monthly_pct < -5:
            items.append(ScoreItem("external", "美股月跌>5%", "risk", +1))
        if inp.us_monthly_pct > 5:
            items.append(ScoreItem("external", "美股月涨>5%", "opportunity", -1))
    if inp.global_recession:
        items.append(ScoreItem("external", "主要经济体衰退", "risk", +2))
    if inp.fed_zero_qe:
        items.append(ScoreItem("external", "美联储零利率+QE", "opportunity", -2))
    if inp.global_stimulus:
        items.append(ScoreItem("external", "全球同步刺激", "opportunity", -1))
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
    return items


# =====================================================
# 档位映射（评分 → 目标股票仓位 %）
# =====================================================
def score_to_target_equity(score: int) -> tuple[float, str]:
    """评分 → (目标股票仓位 %, 档位描述)"""
    if score <= -10:
        return 90.0, "极度便宜+刺激共振"
    if score <= -5:
        return 80.0, "机会显著"
    if score < 0:
        return 75.0, "机会偏多"
    if score <= 3:
        return 75.0, "平衡"
    if score <= 6:
        return 60.0, "风险偏多"
    if score <= 9:
        return 50.0, "风险显著"
    if score <= 12:
        return 30.0, "高风险"
    return 20.0, "极端风险"


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
    if inputs.ppi_yoy_change == "turn_positive" or inputs.pmi_resume_expansion:
        hits.append("基本面改善")

    passed = len(hits) >= 2
    return passed, " + ".join(hits) if hits else "无政策实弹"
