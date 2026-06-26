#!/usr/bin/env python3
"""可视化 annual_sector_signals 数据质量检查。

生成 3 张图：
1. 主题出现热力图（时间 × 主题，颜色=信号强度）
2. 主题碎片化分析（相似主题归组）
3. 每季度信号强度分布
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

OUTPUT_DIR = ROOT / "docs" / "assets"
OUTPUT_DIR.mkdir(exist_ok=True)

STRENGTH_VAL = {"强": 3, "中": 2, "弱": 1}
STRENGTH_COLOR = {"强": "#d62728", "中": "#ff7f0e", "弱": "#aec7e8"}

# 主题归并规则：关键词 → 标准主题
THEME_MERGE = {
    "农业": ["农业", "三农", "新农村", "农产品", "生猪", "农药", "农业产业化"],
    "节能环保": ["节能", "环保", "减排", "绿色低碳", "降碳", "合同能源"],
    "科技/自主创新": ["科技创新", "自主创新", "高技术", "先进制造", "高端制造", "科技/AI", "新质生产力", "AI"],
    "消费/内需": ["消费", "内需", "零售"],
    "基建/城镇化": ["基建", "城镇化", "城镇", "区域发展", "工程咨询", "顺周期"],
    "汽车": ["汽车", "新能源车", "节能汽车"],
    "金融/银行": ["金融", "银行", "证券", "大金融"],
    "房地产": ["房地产", "地产", "楼市", "城投化债"],
    "新能源": ["新能源", "光伏", "风电", "锂电", "动力电池"],
    "半导体/芯片": ["半导体", "芯片", "集成电路"],
    "军工": ["军工", "国防", "航空航天"],
    "医药": ["医药", "生物", "医疗", "健康"],
    "电力": ["电力", "能源", "石化"],
    "民营/创投": ["民营", "创投", "中小企业"],
    "对外开放/出海": ["境外", "出海", "对外开放"],
}


def mysql_config() -> dict:
    load_dotenv(ROOT / ".env")
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "teststock"),
        "password": os.getenv("MYSQL_PASSWORD", "teststock"),
        "database": os.getenv("MYSQL_DATABASE", "teststock"),
        "charset": "utf8mb4",
    }


def load_data() -> pd.DataFrame:
    conn = pymysql.connect(**mysql_config())
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("""
            SELECT as_of_date, theme, signal_strength
            FROM annual_sector_signals
            ORDER BY as_of_date, theme
        """)
        rows = cur.fetchall()
    conn.close()
    df = pd.DataFrame(rows)
    df["as_of_date"] = pd.to_datetime(df["as_of_date"])
    df["strength_val"] = df["signal_strength"].map(STRENGTH_VAL)
    return df


def normalize_theme(raw: str) -> str:
    for canonical, keywords in THEME_MERGE.items():
        for kw in keywords:
            if kw in raw:
                return canonical
    return raw  # 未匹配的保持原样


def plot_timeline_heatmap(df: pd.DataFrame, output: Path):
    """图1：时间 × 归一化主题 的信号强度热力图。"""
    df2 = df.copy()
    df2["theme_norm"] = df2["theme"].apply(normalize_theme)

    # 按归一化主题 + 日期聚合（取最强信号）
    pivot = df2.groupby(["as_of_date", "theme_norm"])["strength_val"].max().unstack(fill_value=0)

    # 按出现次数降序排列主题
    theme_order = (pivot > 0).sum().sort_values(ascending=False).index.tolist()
    pivot = pivot[theme_order]

    dates = pivot.index
    quarters = [f"{d.year}-Q{(d.month-1)//3+1}" for d in dates]

    fig, ax = plt.subplots(figsize=(max(14, len(dates) * 0.5), max(8, len(theme_order) * 0.45)))

    cmap = matplotlib.colors.ListedColormap(["#f5f5f5", "#aec7e8", "#ff7f0e", "#d62728"])
    bounds = [-0.5, 0.5, 1.5, 2.5, 3.5]
    norm = matplotlib.colors.BoundaryNorm(bounds, cmap.N)

    im = ax.imshow(pivot.T.values, aspect="auto", cmap=cmap, norm=norm,
                   interpolation="nearest")

    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(quarters, rotation=75, ha="right", fontsize=8)
    ax.set_yticks(range(len(theme_order)))
    ax.set_yticklabels(theme_order, fontsize=9)
    ax.set_title("行业题材政策信号热力图（按季度）", fontsize=13, pad=12)
    ax.set_xlabel("季度", fontsize=10)

    legend_patches = [
        mpatches.Patch(color="#d62728", label="强"),
        mpatches.Patch(color="#ff7f0e", label="中"),
        mpatches.Patch(color="#aec7e8", label="弱"),
        mpatches.Patch(color="#f5f5f5", label="无"),
    ]
    ax.legend(handles=legend_patches, loc="upper right", fontsize=9)

    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"图1 保存: {output}")


def plot_theme_fragmentation(df: pd.DataFrame, output: Path):
    """图2：主题碎片化——原始主题频次 vs 归一化后频次对比。"""
    raw_counts = df["theme"].value_counts().head(40)
    df2 = df.copy()
    df2["theme_norm"] = df2["theme"].apply(normalize_theme)
    norm_counts = df2["theme_norm"].value_counts()

    # 找出碎片化严重的主题组（归一化后频次 - 最大单一原始频次 > 2）
    frag_data = []
    for canon, count in norm_counts.items():
        sub = df2[df2["theme_norm"] == canon]["theme"].value_counts()
        frag_data.append({
            "canon": canon,
            "total": count,
            "variants": len(sub),
            "top_raw": sub.index[0] if len(sub) else canon,
            "variant_list": list(sub.index[:5]),
        })
    frag_df = pd.DataFrame(frag_data).sort_values("total", ascending=False).head(15)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7))

    # 左图：原始主题频次（Top 30）
    raw_top = raw_counts.head(30)
    colors = ["#d62728" if c >= 8 else "#ff7f0e" if c >= 4 else "#aec7e8" for c in raw_top.values]
    ax1.barh(range(len(raw_top)), raw_top.values, color=colors)
    ax1.set_yticks(range(len(raw_top)))
    ax1.set_yticklabels(raw_top.index, fontsize=8)
    ax1.invert_yaxis()
    ax1.set_title("原始主题出现频次 Top30\n（红=高频，碎片化严重）", fontsize=11)
    ax1.set_xlabel("出现季度数")
    for i, v in enumerate(raw_top.values):
        ax1.text(v + 0.1, i, str(v), va="center", fontsize=8)

    # 右图：归一化后主题频次 + 变体数
    y = range(len(frag_df))
    bars = ax2.barh(y, frag_df["total"].values, color="#4878d0", alpha=0.8)
    ax2.set_yticks(y)
    ax2.set_yticklabels(frag_df["canon"].values, fontsize=9)
    ax2.invert_yaxis()
    ax2.set_title("归一化后主题频次 Top15\n（括号=变体数量）", fontsize=11)
    ax2.set_xlabel("出现季度数（归并后）")
    for i, row in enumerate(frag_df.itertuples()):
        ax2.text(row.total + 0.1, i, f"{row.total}（{row.variants}种写法）", va="center", fontsize=8)

    plt.suptitle("主题命名碎片化分析", fontsize=13, y=1.01)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"图2 保存: {output}")


def plot_strength_distribution(df: pd.DataFrame, output: Path):
    """图3：每季度信号强度分布 + 整体强度占比。"""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6))

    # 左图：堆叠柱状图（每季度强/中/弱数量）
    pivot = df.groupby(["as_of_date", "signal_strength"]).size().unstack(fill_value=0)
    for col in ["强", "中", "弱"]:
        if col not in pivot.columns:
            pivot[col] = 0
    pivot = pivot[["强", "中", "弱"]]
    dates = pivot.index
    quarters = [f"{d.year}-Q{(d.month-1)//3+1}" for d in dates]

    x = range(len(dates))
    ax1.bar(x, pivot["强"], label="强", color="#d62728")
    ax1.bar(x, pivot["中"], bottom=pivot["强"], label="中", color="#ff7f0e")
    ax1.bar(x, pivot["弱"], bottom=pivot["强"] + pivot["中"], label="弱", color="#aec7e8")
    ax1.set_xticks(x)
    ax1.set_xticklabels(quarters, rotation=75, ha="right", fontsize=8)
    ax1.set_ylabel("信号数")
    ax1.set_title("每季度信号强度分布", fontsize=11)
    ax1.legend()

    # 右图：整体强度饼图
    total_counts = df["signal_strength"].value_counts()
    pie_vals = [total_counts.get(k, 0) for k in ["强", "中", "弱"]]
    pie_colors = [STRENGTH_COLOR[k] for k in ["强", "中", "弱"]]
    wedges, texts, autotexts = ax2.pie(
        pie_vals,
        labels=[f"强 ({pie_vals[0]})", f"中 ({pie_vals[1]})", f"弱 ({pie_vals[2]})"],
        colors=pie_colors,
        autopct="%1.1f%%",
        startangle=90,
    )
    ax2.set_title(f"整体信号强度分布\n（共 {sum(pie_vals)} 条，{len(dates)} 个季度）", fontsize=11)

    plt.suptitle("行业信号强度分析", fontsize=13)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"图3 保存: {output}")


def print_problem_summary(df: pd.DataFrame):
    """打印数据质量问题摘要。"""
    df2 = df.copy()
    df2["theme_norm"] = df2["theme"].apply(normalize_theme)

    print("\n=== 数据质量检查 ===")
    print(f"已覆盖季度: {df['as_of_date'].nunique()} 个")
    print(f"原始主题种类: {df['theme'].nunique()} 种")
    print(f"归一化后主题: {df2['theme_norm'].nunique()} 种（压缩比 {df['theme'].nunique()/df2['theme_norm'].nunique():.1f}x）")

    print("\n--- 碎片化严重的主题组 ---")
    for canon, keywords in THEME_MERGE.items():
        variants = df[df["theme"].apply(lambda t: any(kw in t for kw in keywords))]["theme"].unique()
        if len(variants) > 1:
            print(f"  [{canon}] {len(variants)} 种写法: {list(variants)[:6]}")

    print("\n--- 未归并的零散主题（出现1次） ---")
    singles = df["theme"].value_counts()
    singles = singles[singles == 1].index.tolist()
    print(f"  共 {len(singles)} 个: {singles[:20]}")


def main():
    print("加载数据...")
    df = load_data()
    print(f"共 {len(df)} 条信号，{df['as_of_date'].nunique()} 个季度")

    plt.rcParams["font.family"] = ["STHeiti", "HanziPen SC", "STFangsong",
                                    "Hiragino Sans GB", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    plot_timeline_heatmap(df, OUTPUT_DIR / "sector_signals_heatmap.png")
    plot_theme_fragmentation(df, OUTPUT_DIR / "sector_signals_fragmentation.png")
    plot_strength_distribution(df, OUTPUT_DIR / "sector_signals_strength.png")
    print_problem_summary(df)


if __name__ == "__main__":
    main()
