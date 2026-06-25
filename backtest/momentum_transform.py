"""
momentum_transform.py — v3.6 动能转换框架

核心原则（用户洞见）:
"决定市场行为的永远是市场本身。外部信号都需要转换成内部的动能才能决定走势。"

同一个外部信号（PE 高、新基爆款、两融激增），在不同市场内部状态下，
会被转换成不同的实际动能（放大 / 吸收 / 反转 / 沉默）。

四个转换维度：
- A 政策预期 price-in 程度
- B 资金性质粘性（储蓄 / 信贷 / 杠杆）
- C 牛市动能阶段（短反弹 / 中期 / 长牛）
- D 估值-盈利赛跑节奏

公式：
    过热扣分 = 过热基础分 × ((rA + rB + rC + rD) / 4)

放大率默认 1.0；满足吸收条件 → 下调；满足放大条件 → 上调。

历史校准（v3.6 设计文档 §四）:
- 2007.05: 全吸收 → 0.50 倍 → 维稳（实际 +60% 5 个月）✓
- 2007.10 顶: 全放大 → 1.375 倍 → 强减仓（实际见顶）✓
- 2008.01: 1.125 倍 → 减仓 ✓
- 2009.07: 全放大 → 1.75 倍 → 强减仓（实际暴跌 -24%）✓

在 v5.0 三层架构中的角色：
- 套在评分卡（scorecard）之上，对"过热"信号做动能加权
- 战略层年初决策 + 战术层关键转折点（半年校验）使用

红线：
- 不预言未来 — 所有输入信号都必须是"截至当月已发生"
- 不修改阈值救场 — 转换率阈值固化在代码里
"""

from __future__ import annotations

from dataclasses import dataclass


# =====================================================
# 动能转换输入（截至当月的市场内部状态）
# =====================================================
@dataclass
class MomentumState:
    """
    单月市场内部状态 — 用于动能转换。

    所有字段都允许 None，表示数据缺失（该维度退化为默认 1.0）。
    """
    # A 维度: 政策 price-in
    rate_cum_bp: float | None = None         # 累计加息基点（含降息为负）
    rate_hike_months: int | None = None      # 持续加息月数（首次加息到当月）
    first_tightening_hint: bool = False      # 央行首次出现"微调/退出"措辞
    in_loose_phase: bool = False             # 当前处于宽松周期中段

    # B 维度: 资金粘性
    m1_yoy: float | None = None              # M1 同比 %
    new_fund_3m_over_1000: bool = False      # 新基连续 3 月 > 1000 亿
    new_loan_over_1trn: bool = False         # 当月新增贷款 > 1 万亿
    margin_to_float_pct: float | None = None  # 两融余额 / 流通市值 %

    # C 维度: 牛市动能阶段
    months_since_bear_bottom: int | None = None  # 距上一次熊市底部月数

    # D 维度: 估值-盈利赛跑
    pe_yoy_pct: float | None = None          # PE 同比 %
    eps_yoy_pct: float | None = None         # EPS 同比 %


# =====================================================
# 动能转换结果
# =====================================================
@dataclass
class TransformResult:
    rate_a: float       # 政策 price-in 转换率
    rate_b: float       # 资金粘性转换率
    rate_c: float       # 牛市阶段转换率
    rate_d: float       # 估值-盈利赛跑转换率
    multiplier: float   # 平均放大率 = (rA + rB + rC + rD) / 4
    notes_a: str
    notes_b: str
    notes_c: str
    notes_d: str

    def explain(self) -> str:
        return (
            f"A={self.rate_a:.2f}({self.notes_a}) "
            f"B={self.rate_b:.2f}({self.notes_b}) "
            f"C={self.rate_c:.2f}({self.notes_c}) "
            f"D={self.rate_d:.2f}({self.notes_d}) "
            f"→ ×{self.multiplier:.3f}"
        )


# =====================================================
# 四个维度的转换函数
# =====================================================
def transform_a_policy(state: MomentumState) -> tuple[float, str]:
    """
    A 政策预期 price-in:
    - A1 已加息 ≥75bp 且连续 6 月: 紧缩已 price-in → 0.3（吸收）
    - A2 宽松末期首次出现微调: 紧缩信号放大 → 2.0
    - A3 宽松中段无收紧迹象: 紧缩信号未生效 → 0.5（吸收）
    - 默认: 1.0
    """
    if state.in_loose_phase and state.first_tightening_hint:
        return 2.0, "宽松末期微调首现"
    if (state.rate_cum_bp is not None and state.rate_cum_bp >= 75
            and state.rate_hike_months is not None and state.rate_hike_months >= 6):
        return 0.3, "紧缩已price-in"
    if state.in_loose_phase and not state.first_tightening_hint:
        if state.rate_cum_bp is None or state.rate_cum_bp <= 0:
            return 0.5, "宽松中段"
    return 1.0, "中性"


def transform_b_liquidity_stickiness(state: MomentumState) -> tuple[float, str]:
    """
    B 资金粘性:
    - B1 居民储蓄主导（新基连续 3 月 >1000 亿 + M1<20%）→ 0.7（吸收）
    - B2 信贷溢出主导（M1>25% + 新增贷款>1万亿）→ 1.5（放大）
    - B3 杠杆资金主导（两融/流通市值 > 2%）→ 2.0（强放大）
    - 默认: 1.0
    """
    # B3 优先（最强信号）
    if state.margin_to_float_pct is not None and state.margin_to_float_pct > 2.0:
        return 2.0, "杠杆主导"
    # B2
    if (state.m1_yoy is not None and state.m1_yoy > 25
            and state.new_loan_over_1trn):
        return 1.5, "信贷溢出"
    # B1
    if (state.new_fund_3m_over_1000
            and state.m1_yoy is not None and state.m1_yoy < 20):
        return 0.7, "储蓄长钱"
    return 1.0, "中性"


def transform_c_bull_phase(state: MomentumState) -> tuple[float, str]:
    """
    C 牛市动能阶段:
    - <12 月: 短反弹，散户未接盘 → 1.5（顶部尖锐）
    - 12-24 月: 中段 → 1.0
    - >24 月: 长牛，大妈层接盘 → 0.5（顶部圆滑）
    """
    if state.months_since_bear_bottom is None:
        return 1.0, "未知"
    if state.months_since_bear_bottom < 12:
        return 1.5, f"短反弹{state.months_since_bear_bottom}月"
    if state.months_since_bear_bottom < 24:
        return 1.0, f"中段{state.months_since_bear_bottom}月"
    return 0.5, f"长牛{state.months_since_bear_bottom}月"


def transform_d_pe_eps_race(state: MomentumState) -> tuple[float, str]:
    """
    D 估值-盈利赛跑:
    - PE 涨速 / EPS 涨速 > 3 且 EPS>0: 严重透支 → 2.0
    - PE 涨速 / EPS 涨速 > 2 且 EPS>0: 透支 → 1.5
    - |PE - EPS| < 5pp: 同步 → 0.5（健康）
    - 默认: 1.0
    """
    pe = state.pe_yoy_pct
    eps = state.eps_yoy_pct
    if pe is None or eps is None:
        return 1.0, "未知"
    if eps > 0:
        # 防 0 除
        ratio = pe / max(eps, 1.0)
        if ratio > 3:
            return 2.0, f"严重透支({ratio:.1f}x)"
        if ratio > 2:
            return 1.5, f"透支({ratio:.1f}x)"
    if abs(pe - eps) < 5:
        return 0.5, "同步"
    return 1.0, "中性"


# =====================================================
# 主转换函数
# =====================================================
def transform_momentum(state: MomentumState) -> TransformResult:
    """计算 4 维转换率 + 平均放大率"""
    ra, na = transform_a_policy(state)
    rb, nb = transform_b_liquidity_stickiness(state)
    rc, nc = transform_c_bull_phase(state)
    rd, nd = transform_d_pe_eps_race(state)
    multiplier = (ra + rb + rc + rd) / 4.0
    return TransformResult(
        rate_a=ra, rate_b=rb, rate_c=rc, rate_d=rd,
        multiplier=multiplier,
        notes_a=na, notes_b=nb, notes_c=nc, notes_d=nd,
    )


# =====================================================
# 把转换器套到评分卡上的辅助函数
# =====================================================
def overheat_base_score(
    pe_above_30: bool,
    new_fund_above_1000: bool,
    margin_surge: bool,
    consecutive_5m_rally_50pct: bool,
) -> int:
    """
    "过热"基础分（恒为负或 0）。
    每命中一条 = -1 分。
    """
    score = 0
    if pe_above_30: score -= 1
    if new_fund_above_1000: score -= 1
    if margin_surge: score -= 1
    if consecutive_5m_rally_50pct: score -= 1
    return score


def apply_momentum_to_overheat(
    overheat_base: int,
    state: MomentumState,
) -> tuple[float, TransformResult]:
    """
    把过热基础分通过动能转换器加权。

    Returns:
        (实际过热扣分, 转换详情)
    """
    result = transform_momentum(state)
    actual = overheat_base * result.multiplier
    return actual, result
