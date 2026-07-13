"""Load annual direction context from local database."""

from __future__ import annotations

import pymysql

from agents.annual_direction.db import connect
from agents.annual_direction.mode import AgentMode, resolve_mode
from agents.annual_direction.models import AnnualContext, EtfCandidate
from agents.annual_direction.sector_signals import sector_signals_as_of
from macro.annual_snapshot import annual_macro_brief, load_snapshot, margin_asof_dict, pboc_asof_dict, valuation_asof_dict

THEME_ORDER = [
    "宽基",
    "成长",
    "科技",
    "消费",
    "医药",
    "金融",
    "红利",
    "新能源",
    "军工",
    "周期",
    "其他",
]

# Representative index keywords → theme hints for ETF pool
THEME_RULES: list[tuple[str, str]] = [
    ("沪深300", "宽基"),
    ("中证500", "宽基"),
    ("上证50", "宽基"),
    ("创业板", "成长"),
    ("科创", "科技"),
    ("半导体", "科技"),
    ("芯片", "科技"),
    ("计算机", "科技"),
    ("人工智能", "科技"),
    ("消费", "消费"),
    ("白酒", "消费"),
    ("医药", "医药"),
    ("医疗", "医药"),
    ("银行", "金融"),
    ("证券", "金融"),
    ("红利", "红利"),
    ("股息", "红利"),
    ("有色", "周期"),
    ("煤炭", "周期"),
    ("新能源", "新能源"),
    ("光伏", "新能源"),
    ("军工", "军工"),
    ("国债", "债券"),
    ("货币", "现金"),
]


def _theme_hint(index_name: str | None) -> str | None:
    if not index_name:
        return None
    for kw, theme in THEME_RULES:
        if kw in index_name:
            return theme
    return "其他"


def load_cewc(conn, apply_year: int) -> dict | None:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM cewc_annual WHERE apply_year = %s", (apply_year,))
        return cur.fetchone()


def load_etf_candidates(
    conn, apply_year: int, *, as_of_date: str, backtest: bool, limit_per_theme: int = 3
) -> list[EtfCandidate]:
    sql = """
        SELECT ts_code, extname, index_name, list_date, exchange
        FROM passive_etf
        WHERE list_date IS NOT NULL
          AND list_date <= %s
          AND is_enhanced = 0
          AND (etf_type IS NULL OR etf_type = '纯境内')
    """
    if backtest:
        # 回测：包含当时已上市、现已退市的标的，不用当前 list_status='L' 过滤
        sql += " AND (list_status IN ('L', 'D') OR list_status IS NULL)"
    else:
        sql += " AND list_status = 'L'"
    sql += " ORDER BY list_date ASC"
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql, (as_of_date,))
        rows = cur.fetchall()

    by_theme: dict[str, list[EtfCandidate]] = {}
    for r in rows:
        code = r["ts_code"]
        if not (code.endswith(".SH") or code.endswith(".SZ")):
            continue
        hint = _theme_hint(r.get("index_name"))
        if hint in ("债券", "现金"):
            continue
        ld = r["list_date"]
        list_date = ld.isoformat() if hasattr(ld, "isoformat") else str(ld)
        cand = EtfCandidate(
            ts_code=r["ts_code"],
            extname=r["extname"],
            index_name=r.get("index_name"),
            list_date=list_date,
            exchange=r.get("exchange"),
            theme_hint=hint,
        )
        key = hint or "其他"
        by_theme.setdefault(key, [])
        if len(by_theme[key]) < limit_per_theme:
            by_theme[key].append(cand)

    out: list[EtfCandidate] = []
    for theme in THEME_ORDER:
        if theme in by_theme:
            out.extend(by_theme[theme])
    for theme, cands in by_theme.items():
        if theme not in THEME_ORDER:
            out.extend(cands)
    return out


def load_etf_count(conn, as_of_date: str, *, backtest: bool) -> int:
    sql = """
        SELECT COUNT(*) FROM passive_etf
        WHERE list_date IS NOT NULL AND list_date <= %s
    """
    if backtest:
        sql += " AND (list_status IN ('L', 'D') OR list_status IS NULL)"
    else:
        sql += " AND list_status = 'L'"
    with conn.cursor() as cur:
        cur.execute(sql, (as_of_date,))
        return cur.fetchone()[0]


def build_context(apply_year: int, *, mode: str = "auto") -> AnnualContext:
    agent_mode = resolve_mode(apply_year, mode=mode)
    as_of = agent_mode.as_of_date
    conn = connect()
    try:
        cewc = load_cewc(conn, apply_year)
        snap = load_snapshot(conn, apply_year)
        if snap is None:
            snap = {}
        val = valuation_asof_dict(conn, as_of)
        pboc = pboc_asof_dict(conn, as_of)
        margin = margin_asof_dict(conn, as_of)
        for key, value in {**val, **pboc, **margin}.items():
            if value is not None and snap.get(key) is None:
                snap[key] = value
        brief = annual_macro_brief(conn, apply_year)
        if not brief or brief.endswith("暂无宏观快照"):
            if val.get("hs300_pe_ttm") or val.get("sz50_pe_ttm"):
                parts = [f"=== {apply_year} 年初估值 (截止 {val.get('valuation_date')}) ==="]
                if val.get("hs300_pe_ttm") is not None:
                    parts.append(
                        f"沪深300 PE-TTM {val['hs300_pe_ttm']}% / PB {val.get('hs300_pb')}"
                        f" → {val.get('valuation_stance')}"
                    )
                if val.get("sz50_pe_ttm") is not None:
                    parts.append(f"上证50 PE-TTM {val['sz50_pe_ttm']}% / PB {val.get('sz50_pb')}")
                brief = "\n".join(parts)
        candidates = load_etf_candidates(
            conn, apply_year, as_of_date=as_of, backtest=agent_mode.is_backtest
        )
        count = load_etf_count(conn, as_of, backtest=agent_mode.is_backtest)
        sector_sigs = sector_signals_as_of(conn, as_of)
        csi_top: list[dict] = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rank_position, ts_code, index_name, final_score, best_theme, news_score
                FROM csi_annual_recommendation
                WHERE apply_year = %s AND ts_code LIKE '%%.CSI'
                ORDER BY rank_position LIMIT 10
                """,
                (apply_year,),
            )
            for rank, ts, name, score, theme, news in cur.fetchall():
                csi_top.append({
                    "rank": rank, "ts_code": ts, "index_name": name,
                    "final_score": float(score), "best_theme": theme,
                    "news_score": float(news) if news is not None else None,
                })
    finally:
        conn.close()

    return AnnualContext(
        apply_year=apply_year,
        agent_mode=agent_mode,
        cewc=cewc,
        macro_snapshot=snap,
        macro_brief=brief,
        etf_universe_count=count,
        etf_candidates=candidates,
        sector_signals=[s.to_dict() for s in sector_sigs],
        csi_recommendations=csi_top,
    )
