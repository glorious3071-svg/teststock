"""Annual macro snapshot for 年初定方向.

Combines CEWC monetary policy with interest-rate levels at year-start
to produce liquidity stance signals for the strategic allocation layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pymysql


@dataclass
class RateSnapshot:
    apply_year: int
    snapshot_date: date
    shibor_on: float | None
    shibor_3m: float | None
    shibor_1y: float | None
    shibor_3m_yoy_bp: float | None
    lpr_1y: float | None
    lpr_5y: float | None
    lpr_1y_yoy_bp: float | None
    libor_3m_usd: float | None
    liquidity_stance: str | None
    rate_trend: str | None
    cewc_monetary_policy: str | None
    policy_rate_gap: str | None
    notes: str | None = None
    gdp_quarter: str | None = None
    gdp_yoy: float | None = None
    si_yoy: float | None = None
    ti_yoy: float | None = None
    growth_stance: str | None = None
    price_month: str | None = None
    cpi_yoy: float | None = None
    cpi_accu: float | None = None
    ppi_yoy: float | None = None
    ppi_accu: float | None = None
    ppi_cpi_spread: float | None = None
    inflation_stance: str | None = None
    money_month: str | None = None
    m1_yoy: float | None = None
    m2_yoy: float | None = None
    m1_m2_scissors: float | None = None
    money_stance: str | None = None
    sf_month: str | None = None
    sf_inc_cumval: float | None = None
    sf_stk_endval: float | None = None
    sf_stk_yoy: float | None = None
    sf_stance: str | None = None
    pmi_month: str | None = None
    pmi_mfg: float | None = None
    pmi_non_mfg: float | None = None
    pmi_composite: float | None = None
    pmi_stance: str | None = None
    us_rate_date: date | None = None
    us_10y_nominal: float | None = None
    us_10y_real: float | None = None
    us_tbill_13w: float | None = None
    us_10y_real_yoy_bp: float | None = None
    global_rate_stance: str | None = None
    pboc_report_date: date | None = None
    pboc_report_title: str | None = None
    corpus_note: str | None = None
    valuation_date: date | None = None
    hs300_pe: float | None = None
    hs300_pe_ttm: float | None = None
    hs300_pb: float | None = None
    sz50_pe_ttm: float | None = None
    sz50_pb: float | None = None
    zz500_pe_ttm: float | None = None
    zz500_pb: float | None = None
    valuation_stance: str | None = None
    margin_date: date | None = None
    margin_rzye: float | None = None
    margin_rqye: float | None = None
    margin_rzrqye: float | None = None
    margin_rzrqye_yoy_pct: float | None = None
    margin_stance: str | None = None


def _last_rate_on_or_before(
    cur,
    table: str,
    col_date: str,
    target: str,
    cols: list[str],
    *,
    extra_where: str = "",
    extra_params: tuple = (),
) -> dict | None:
    """Get the latest row on or before target date from a rate table."""
    select_cols = ", ".join([col_date] + cols)
    cur.execute(
        f"""
        SELECT {select_cols}
        FROM {table}
        WHERE {col_date} <= %s {extra_where}
        ORDER BY {col_date} DESC
        LIMIT 1
        """,
        (target, *extra_params),
    )
    row = cur.fetchone()
    if not row:
        return None
    return dict(zip([col_date] + cols, row))


def _bp_delta(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    return round((current - prior) * 100, 2)


def classify_liquidity(shibor_3m: float | None, shibor_3m_yoy_bp: float | None) -> str | None:
    """Classify domestic liquidity from SHIBOR 3M level and YoY change."""
    if shibor_3m is None:
        return None
    # Thresholds tuned for post-2006 SHIBOR regime (rough guide, not hard rules)
    if shibor_3m <= 2.0 or (shibor_3m_yoy_bp is not None and shibor_3m_yoy_bp <= -50):
        return "宽松"
    if shibor_3m >= 4.0 or (shibor_3m_yoy_bp is not None and shibor_3m_yoy_bp >= 80):
        return "紧缩"
    if shibor_3m >= 3.2 or (shibor_3m_yoy_bp is not None and shibor_3m_yoy_bp >= 30):
        return "偏紧"
    return "中性"


def classify_rate_trend(shibor_3m_yoy_bp: float | None, lpr_1y_yoy_bp: float | None) -> str | None:
    refs = [x for x in (shibor_3m_yoy_bp, lpr_1y_yoy_bp) if x is not None]
    if not refs:
        return None
    avg = sum(refs) / len(refs)
    if avg <= -20:
        return "下行"
    if avg >= 20:
        return "上行"
    return "持平"


def classify_policy_gap(monetary_policy: str | None, liquidity_stance: str | None) -> str | None:
    if not monetary_policy or not liquidity_stance:
        return None
    mp = monetary_policy
    loose_words = ("宽松", "适度宽松")
    tight_words = ("从紧", "收紧", "稳健偏紧")
    if any(w in mp for w in loose_words):
        expected = "宽松"
    elif any(w in mp for w in tight_words):
        expected = "紧缩"
    else:
        expected = "中性"

    if expected == liquidity_stance or (expected == "中性" and liquidity_stance == "偏紧"):
        return "一致"
    if expected == "宽松" and liquidity_stance in ("偏紧", "紧缩"):
        return "政策松但利率紧"
    if expected == "紧缩" and liquidity_stance in ("宽松", "中性"):
        return "政策紧但利率松"
    return "部分背离"


def classify_growth(gdp_yoy: float | None) -> str | None:
    """Classify GDP growth pace for strategic allocation."""
    if gdp_yoy is None:
        return None
    if gdp_yoy >= 8.0:
        return "高速增长"
    if gdp_yoy >= 6.0:
        return "较快增长"
    if gdp_yoy >= 4.5:
        return "稳健增长"
    if gdp_yoy >= 3.0:
        return "放缓"
    return "承压"


def _latest_gdp_for_year(cur, calendar_year: int) -> dict | None:
    """Prefer Q4→Q1 of calendar_year for year-start macro view."""
    for q in (4, 3, 2, 1):
        quarter = f"{calendar_year}Q{q}"
        cur.execute(
            """
            SELECT quarter, gdp_yoy, si_yoy, ti_yoy
            FROM cn_gdp_quarterly WHERE quarter = %s
            """,
            (quarter,),
        )
        row = cur.fetchone()
        if row:
            return dict(zip(["quarter", "gdp_yoy", "si_yoy", "ti_yoy"], row))
    return None


def classify_inflation(cpi_yoy: float | None, ppi_yoy: float | None) -> str | None:
    """Classify inflation environment from CPI/PPI YoY."""
    if cpi_yoy is None:
        return None
    if cpi_yoy < 0:
        return "通缩风险"
    if cpi_yoy < 1.0:
        return "低通胀"
    if cpi_yoy <= 3.0:
        return "温和通胀"
    if cpi_yoy <= 5.0:
        return "通胀"
    return "高通胀"


def classify_money(m1_yoy: float | None, m2_yoy: float | None, scissors: float | None) -> str | None:
    """Classify money supply environment (aligns with v3.6 M1/M2 signals)."""
    if m1_yoy is None and m2_yoy is None:
        return None
    if m2_yoy is not None and m2_yoy >= 13.0:
        return "宽货币"
    if m2_yoy is not None and m2_yoy < 8.5:
        return "紧信用"
    if m1_yoy is not None and m1_yoy >= 20.0:
        return "活性偏强"
    if scissors is not None and scissors > 3.0:
        return "活性偏强"
    if m1_yoy is not None and m1_yoy < 10.0:
        return "活性偏弱"
    if scissors is not None and scissors < -3.0:
        return "活性偏弱"
    return "中性"


def classify_sf(sf_stk_yoy: float | None, sf_inc_cumval: float | None) -> str | None:
    """Classify credit expansion from social financing stock YoY or annual increment."""
    if sf_stk_yoy is not None:
        if sf_stk_yoy >= 11.0:
            return "信用扩张"
        if sf_stk_yoy >= 9.0:
            return "稳健扩张"
        if sf_stk_yoy >= 7.0:
            return "中性"
        return "信用放缓"
    if sf_inc_cumval is not None:
        # 万亿元量级：2010s 高位约 25-30 万亿，近年约 30-35 万亿
        trillion = sf_inc_cumval / 10000.0
        if trillion >= 30:
            return "信用扩张"
        if trillion >= 22:
            return "稳健扩张"
        if trillion >= 15:
            return "中性"
        return "信用放缓"
    return None


def _latest_month_data(cur, calendar_year: int) -> dict | None:
    """Prefer Dec→Jan of calendar_year for CPI/PPI year-start view."""
    for m in range(12, 0, -1):
        month = f"{calendar_year}{m:02d}"
        cur.execute(
            """
            SELECT c.month, c.nt_yoy, c.nt_accu, p.ppi_yoy, p.ppi_accu
            FROM cn_cpi_monthly c
            LEFT JOIN cn_ppi_monthly p ON c.month = p.month
            WHERE c.month = %s
            """,
            (month,),
        )
        row = cur.fetchone()
        if row:
            return dict(zip(["month", "cpi_yoy", "cpi_accu", "ppi_yoy", "ppi_accu"], row))
    return None


def _latest_money_for_year(cur, calendar_year: int) -> dict | None:
    """Prefer Dec→Jan of calendar_year for M1/M2 year-start view."""
    for m in range(12, 0, -1):
        month = f"{calendar_year}{m:02d}"
        cur.execute(
            """
            SELECT month, m1_yoy, m2_yoy
            FROM cn_m_monthly WHERE month = %s
            """,
            (month,),
        )
        row = cur.fetchone()
        if row:
            return dict(zip(["month", "m1_yoy", "m2_yoy"], row))
    return None


def classify_pmi(pmi_mfg: float | None, pmi_non_mfg: float | None) -> str | None:
    """Classify manufacturing/services PMI vs boom-bust line (50)."""
    ref = pmi_mfg
    if ref is None:
        ref = pmi_non_mfg
    if ref is None:
        return None
    if ref >= 52.0:
        return "景气"
    if ref >= 50.0:
        return "荣枯线上方"
    if ref >= 48.0:
        return "临界偏弱"
    return "收缩"


def classify_global_rate(us_10y_real: float | None, us_10y_nominal: float | None) -> str | None:
    """Classify global financial conditions from US 10Y real/nominal yields."""
    ref = us_10y_real
    nominal_mode = False
    if ref is None:
        ref = us_10y_nominal
        nominal_mode = True
    if ref is None:
        return None
    if not nominal_mode:
        if ref < 0:
            return "全球宽松"
        if ref < 1.0:
            return "中性偏松"
        if ref < 2.0:
            return "中性"
        if ref < 3.0:
            return "偏紧"
        return "紧缩"
    if ref < 2.0:
        return "全球宽松"
    if ref < 3.5:
        return "中性"
    if ref < 5.0:
        return "偏紧"
    return "紧缩"


def classify_valuation(pe_ttm: float | None) -> str | None:
    """Classify broad index valuation from PE-TTM."""
    if pe_ttm is None:
        return None
    if pe_ttm <= 12:
        return "偏低"
    if pe_ttm <= 18:
        return "合理"
    if pe_ttm <= 25:
        return "偏高"
    return "高估"


def classify_margin_stance(rzrqye: float | None, yoy_pct: float | None) -> str | None:
    """Classify margin sentiment from balance level and YoY change."""
    if rzrqye is None:
        return None
    trillions = rzrqye / 1e12
    if trillions >= 2.0 or (yoy_pct is not None and yoy_pct >= 20):
        return "偏热"
    if trillions <= 0.4 or (yoy_pct is not None and yoy_pct <= -10):
        return "偏冷"
    return "中性"


def _latest_margin_summary(cur, snapshot_target: str, yoy_target: str) -> dict[str, Any]:
    """SSE+SZSE margin totals on last trading day on/before target."""
    if not _table_exists(cur, "margin_daily"):
        return {}
    cur.execute(
        """
        SELECT MAX(trade_date) FROM margin_daily
        WHERE trade_date <= %s AND exchange_id IN ('SSE', 'SZSE')
        """,
        (snapshot_target,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return {}
    trade_date = row[0]
    cur.execute(
        """
        SELECT SUM(rzye), SUM(rqye), SUM(rzrqye)
        FROM margin_daily
        WHERE trade_date = %s AND exchange_id IN ('SSE', 'SZSE')
        """,
        (trade_date,),
    )
    sums = cur.fetchone()
    if not sums or sums[2] is None:
        return {}

    cur.execute(
        """
        SELECT MAX(trade_date) FROM margin_daily
        WHERE trade_date <= %s AND exchange_id IN ('SSE', 'SZSE')
        """,
        (yoy_target,),
    )
    prior_date_row = cur.fetchone()
    prior_rzrqye = None
    if prior_date_row and prior_date_row[0]:
        cur.execute(
            """
            SELECT SUM(rzrqye) FROM margin_daily
            WHERE trade_date = %s AND exchange_id IN ('SSE', 'SZSE')
            """,
            (prior_date_row[0],),
        )
        prior_row = cur.fetchone()
        prior_rzrqye = float(prior_row[0]) if prior_row and prior_row[0] is not None else None

    rzrqye = float(sums[2])
    yoy_pct = None
    if prior_rzrqye and prior_rzrqye > 0:
        yoy_pct = round((rzrqye / prior_rzrqye - 1) * 100, 2)

    return {
        "margin_date": trade_date,
        "margin_rzye": float(sums[0]) if sums[0] is not None else None,
        "margin_rqye": float(sums[1]) if sums[1] is not None else None,
        "margin_rzrqye": rzrqye,
        "margin_rzrqye_yoy_pct": yoy_pct,
        "margin_stance": classify_margin_stance(rzrqye, yoy_pct),
    }


def margin_asof_dict(conn, as_of_date: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        yoy_year = int(as_of_date[:4]) - 1
        yoy_target = f"{yoy_year}-12-31"
        return _latest_margin_summary(cur, as_of_date, yoy_target)


def _latest_index_valuation(cur, ts_code: str, snapshot_target: str) -> dict | None:
    if not _table_exists(cur, "index_dailybasic"):
        return None
    cur.execute(
        """
        SELECT trade_date, pe, pe_ttm, pb
        FROM index_dailybasic
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (ts_code, snapshot_target),
    )
    row = cur.fetchone()
    if not row:
        return None
    return dict(zip(["trade_date", "pe", "pe_ttm", "pb"], row))


def valuation_asof_dict(conn, as_of_date: str) -> dict[str, Any]:
    """Load year-end index valuation fields for a given cutoff date."""
    with conn.cursor() as cur:
        hs300 = _latest_index_valuation(cur, "000300.SH", as_of_date)
        sz50 = _latest_index_valuation(cur, "000016.SH", as_of_date)
        zz500 = _latest_index_valuation(cur, "000905.SH", as_of_date)

    valuation_date = None
    if hs300:
        valuation_date = hs300["trade_date"]
    elif sz50:
        valuation_date = sz50["trade_date"]

    hs300_pe_ttm = float(hs300["pe_ttm"]) if hs300 and hs300.get("pe_ttm") is not None else None
    sz50_pe_ttm = float(sz50["pe_ttm"]) if sz50 and sz50.get("pe_ttm") is not None else None
    zz500_pe_ttm = float(zz500["pe_ttm"]) if zz500 and zz500.get("pe_ttm") is not None else None

    return {
        "valuation_date": valuation_date,
        "hs300_pe": float(hs300["pe"]) if hs300 and hs300.get("pe") is not None else None,
        "hs300_pe_ttm": hs300_pe_ttm,
        "hs300_pb": float(hs300["pb"]) if hs300 and hs300.get("pb") is not None else None,
        "sz50_pe_ttm": sz50_pe_ttm,
        "sz50_pb": float(sz50["pb"]) if sz50 and sz50.get("pb") is not None else None,
        "zz500_pe_ttm": zz500_pe_ttm,
        "zz500_pb": float(zz500["pb"]) if zz500 and zz500.get("pb") is not None else None,
        "valuation_stance": classify_valuation(hs300_pe_ttm or sz50_pe_ttm),
    }


def pboc_asof_dict(conn, as_of_date: str) -> dict[str, Any]:
    """Latest PBOC quarterly report published on or before cutoff."""
    with conn.cursor() as cur:
        if not _table_exists(cur, "pboc_monetary_policy"):
            return {}
        cur.execute(
            """
            SELECT pub_date, title, url, pdf_url
            FROM pboc_monetary_policy
            WHERE pub_date <= %s
            ORDER BY pub_date DESC
            LIMIT 1
            """,
            (as_of_date,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    return {
        "pboc_report_date": row[0],
        "pboc_report_title": row[1],
        "pboc_report_url": row[2],
        "pboc_report_pdf": row[3],
    }


def _latest_pmi_for_year(cur, calendar_year: int) -> dict | None:
    for m in range(12, 0, -1):
        month = f"{calendar_year}{m:02d}"
        cur.execute(
            """
            SELECT month, pmi_mfg, pmi_non_mfg, pmi_composite
            FROM cn_pmi_monthly WHERE month = %s
            """,
            (month,),
        )
        row = cur.fetchone()
        if row:
            return dict(zip(["month", "pmi_mfg", "pmi_non_mfg", "pmi_composite"], row))
    return None


def _latest_us_rates(cur, snapshot_target: str, yoy_target: str) -> dict:
    """Load year-end US Treasury snapshot fields."""
    out: dict = {
        "us_rate_date": None,
        "us_10y_nominal": None,
        "us_10y_real": None,
        "us_tbill_13w": None,
        "us_10y_real_yoy_bp": None,
    }

    tycr = _last_rate_on_or_before(cur, "us_tycr_daily", "trade_date", snapshot_target, ["y10"])
    if tycr:
        out["us_rate_date"] = tycr["trade_date"]
        out["us_10y_nominal"] = float(tycr["y10"]) if tycr.get("y10") is not None else None

    trycr = _last_rate_on_or_before(cur, "us_trycr_daily", "trade_date", snapshot_target, ["y10"])
    if trycr:
        out["us_rate_date"] = out["us_rate_date"] or trycr["trade_date"]
        out["us_10y_real"] = float(trycr["y10"]) if trycr.get("y10") is not None else None

    tbr = _last_rate_on_or_before(cur, "us_tbr_daily", "trade_date", snapshot_target, ["w13_ce"])
    if tbr:
        out["us_rate_date"] = out["us_rate_date"] or tbr["trade_date"]
        out["us_tbill_13w"] = float(tbr["w13_ce"]) if tbr.get("w13_ce") is not None else None

    trycr_yoy = _last_rate_on_or_before(cur, "us_trycr_daily", "trade_date", yoy_target, ["y10"])
    if out["us_10y_real"] is not None and trycr_yoy and trycr_yoy.get("y10") is not None:
        out["us_10y_real_yoy_bp"] = _bp_delta(out["us_10y_real"], float(trycr_yoy["y10"]))

    return out


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = %s
        """,
        (table,),
    )
    return cur.fetchone()[0] > 0


def _latest_pboc_report(cur, snapshot_target: str) -> dict | None:
    if not _table_exists(cur, "pboc_monetary_policy"):
        return None
    cur.execute(
        """
        SELECT pub_date, title FROM pboc_monetary_policy
        WHERE pub_date <= %s ORDER BY pub_date DESC LIMIT 1
        """,
        (snapshot_target,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"pub_date": row[0], "title": row[1]}


def _corpus_status_note(cur) -> str:
    tables = [
        ("npr_policy", "政策法规"),
        ("pboc_monetary_policy", "央行货政报告"),
        ("broker_research_report", "券商研报"),
        ("news_flash", "新闻快讯"),
        ("major_news_article", "长篇通讯"),
        ("cctv_news_daily", "新闻联播"),
    ]
    parts = []
    for table, label in tables:
        if not _table_exists(cur, table):
            continue
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        n = cur.fetchone()[0]
        if n:
            parts.append(f"{label}{n}")
    return "、".join(parts) if parts else "语料接口待开权限"


def _latest_sf_for_year(cur, calendar_year: int) -> tuple[dict | None, float | None]:
    """Prefer Dec→Jan; return row and prior-year Dec stock for YoY."""
    current = None
    for m in range(12, 0, -1):
        month = f"{calendar_year}{m:02d}"
        cur.execute(
            """
            SELECT month, inc_month, inc_cumval, stk_endval
            FROM sf_monthly WHERE month = %s
            """,
            (month,),
        )
        row = cur.fetchone()
        if row:
            current = dict(zip(["month", "inc_month", "inc_cumval", "stk_endval"], row))
            break

    prior_stk = None
    if current:
        prior_month = f"{calendar_year - 1}12"
        cur.execute("SELECT stk_endval FROM sf_monthly WHERE month = %s", (prior_month,))
        prior_row = cur.fetchone()
        if prior_row and prior_row[0] is not None and current.get("stk_endval") is not None:
            prior_stk = float(prior_row[0])

    return current, prior_stk


def build_snapshot_for_year(conn, apply_year: int) -> RateSnapshot | None:
    """Build macro snapshot as of Dec 31 of (apply_year - 1), i.e. 年初定方向时点."""
    snapshot_target = f"{apply_year - 1}-12-31"
    yoy_target = f"{apply_year - 2}-12-31"

    with conn.cursor() as cur:
        cur.execute("SELECT monetary_policy FROM cewc_annual WHERE apply_year = %s", (apply_year,))
        cewc_row = cur.fetchone()
        cewc_mp = cewc_row[0] if cewc_row else None

        sh = _last_rate_on_or_before(
            cur, "shibor_daily", "trade_date", snapshot_target,
            ["rate_on", "rate_3m", "rate_1y"],
        )
        sh_yoy = _last_rate_on_or_before(cur, "shibor_daily", "trade_date", yoy_target, ["rate_3m"])

        lp = _last_rate_on_or_before(cur, "lpr_daily", "trade_date", snapshot_target, ["lpr_1y", "lpr_5y"])
        lp_yoy = _last_rate_on_or_before(cur, "lpr_daily", "trade_date", yoy_target, ["lpr_1y"])

        lb = _last_rate_on_or_before(
            cur,
            "libor_daily",
            "trade_date",
            snapshot_target,
            ["rate_3m"],
            extra_where="AND curr_type = %s",
            extra_params=("USD",),
        )

        gdp = _latest_gdp_for_year(cur, apply_year - 1)
        price = _latest_month_data(cur, apply_year - 1)
        money = _latest_money_for_year(cur, apply_year - 1)
        sf, sf_prior_stk = _latest_sf_for_year(cur, apply_year - 1)
        pmi = _latest_pmi_for_year(cur, apply_year - 1)
        us_rates = _latest_us_rates(cur, snapshot_target, yoy_target)
        pboc = _latest_pboc_report(cur, snapshot_target)
        corpus_note = _corpus_status_note(cur)
        hs300_val = _latest_index_valuation(cur, "000300.SH", snapshot_target)
        sz50_val = _latest_index_valuation(cur, "000016.SH", snapshot_target)
        zz500_val = _latest_index_valuation(cur, "000905.SH", snapshot_target)
        margin = _latest_margin_summary(cur, snapshot_target, yoy_target)

    if not sh:
        return None

    snap_date = sh["trade_date"]
    shibor_3m = float(sh["rate_3m"]) if sh.get("rate_3m") is not None else None
    shibor_3m_prior = float(sh_yoy["rate_3m"]) if sh_yoy and sh_yoy.get("rate_3m") is not None else None
    shibor_3m_yoy_bp = _bp_delta(shibor_3m, shibor_3m_prior)

    lpr_1y = float(lp["lpr_1y"]) if lp and lp.get("lpr_1y") is not None else None
    lpr_5y = float(lp["lpr_5y"]) if lp and lp.get("lpr_5y") is not None else None
    lpr_1y_prior = float(lp_yoy["lpr_1y"]) if lp_yoy and lp_yoy.get("lpr_1y") is not None else None
    lpr_1y_yoy_bp = _bp_delta(lpr_1y, lpr_1y_prior)

    liquidity = classify_liquidity(shibor_3m, shibor_3m_yoy_bp)
    trend = classify_rate_trend(shibor_3m_yoy_bp, lpr_1y_yoy_bp)
    gap = classify_policy_gap(cewc_mp, liquidity)

    notes_parts = []
    if lpr_1y is None and apply_year >= 2014:
        notes_parts.append("LPR数据自2013-10起，早年可能缺失")
    if lb is None:
        notes_parts.append("LIBOR取数缺失")
    if gdp is None:
        notes_parts.append("GDP季度数据缺失")
    if price is None:
        notes_parts.append("CPI/PPI月度数据缺失")
    if money is None:
        notes_parts.append("货币供应量月度数据缺失")
    if sf is None and apply_year >= 2011:
        notes_parts.append("社融数据自2010年起，存量同比自2012年初可用")
    if pmi is None:
        notes_parts.append("PMI月度数据缺失")
    if us_rates["us_10y_real"] is None and us_rates["us_10y_nominal"] is None and apply_year >= 2011:
        notes_parts.append("美债利率数据自2010年起")
    if not margin and apply_year >= 2011:
        notes_parts.append("两融数据自2010-03起")

    gdp_yoy = float(gdp["gdp_yoy"]) if gdp and gdp.get("gdp_yoy") is not None else None
    si_yoy = float(gdp["si_yoy"]) if gdp and gdp.get("si_yoy") is not None else None
    ti_yoy = float(gdp["ti_yoy"]) if gdp and gdp.get("ti_yoy") is not None else None

    cpi_yoy = float(price["cpi_yoy"]) if price and price.get("cpi_yoy") is not None else None
    cpi_accu = float(price["cpi_accu"]) if price and price.get("cpi_accu") is not None else None
    ppi_yoy = float(price["ppi_yoy"]) if price and price.get("ppi_yoy") is not None else None
    ppi_accu = float(price["ppi_accu"]) if price and price.get("ppi_accu") is not None else None
    ppi_cpi_spread = (
        round(ppi_yoy - cpi_yoy, 2) if ppi_yoy is not None and cpi_yoy is not None else None
    )

    m1_yoy = float(money["m1_yoy"]) if money and money.get("m1_yoy") is not None else None
    m2_yoy = float(money["m2_yoy"]) if money and money.get("m2_yoy") is not None else None
    m1_m2_scissors = (
        round(m1_yoy - m2_yoy, 2) if m1_yoy is not None and m2_yoy is not None else None
    )

    sf_stk_endval = float(sf["stk_endval"]) if sf and sf.get("stk_endval") is not None else None
    sf_inc_cumval = float(sf["inc_cumval"]) if sf and sf.get("inc_cumval") is not None else None
    sf_stk_yoy = None
    if sf_stk_endval is not None and sf_prior_stk and sf_prior_stk > 0:
        sf_stk_yoy = round((sf_stk_endval / sf_prior_stk - 1) * 100, 2)

    pmi_mfg = float(pmi["pmi_mfg"]) if pmi and pmi.get("pmi_mfg") is not None else None
    pmi_non_mfg = float(pmi["pmi_non_mfg"]) if pmi and pmi.get("pmi_non_mfg") is not None else None
    pmi_composite = float(pmi["pmi_composite"]) if pmi and pmi.get("pmi_composite") is not None else None

    us_10y_nominal = us_rates["us_10y_nominal"]
    us_10y_real = us_rates["us_10y_real"]
    us_tbill_13w = us_rates["us_tbill_13w"]
    us_10y_real_yoy_bp = us_rates["us_10y_real_yoy_bp"]
    us_rate_date = us_rates["us_rate_date"]

    pboc_report_date = pboc["pub_date"] if pboc else None
    pboc_report_title = pboc["title"] if pboc else None

    valuation_date = None
    if hs300_val:
        valuation_date = hs300_val["trade_date"]
    elif sz50_val:
        valuation_date = sz50_val["trade_date"]

    hs300_pe = float(hs300_val["pe"]) if hs300_val and hs300_val.get("pe") is not None else None
    hs300_pe_ttm = float(hs300_val["pe_ttm"]) if hs300_val and hs300_val.get("pe_ttm") is not None else None
    hs300_pb = float(hs300_val["pb"]) if hs300_val and hs300_val.get("pb") is not None else None
    sz50_pe_ttm = float(sz50_val["pe_ttm"]) if sz50_val and sz50_val.get("pe_ttm") is not None else None
    sz50_pb = float(sz50_val["pb"]) if sz50_val and sz50_val.get("pb") is not None else None
    zz500_pe_ttm = float(zz500_val["pe_ttm"]) if zz500_val and zz500_val.get("pe_ttm") is not None else None
    zz500_pb = float(zz500_val["pb"]) if zz500_val and zz500_val.get("pb") is not None else None
    valuation_stance = classify_valuation(hs300_pe_ttm or sz50_pe_ttm)

    margin_date = margin.get("margin_date")
    margin_rzye = margin.get("margin_rzye")
    margin_rqye = margin.get("margin_rqye")
    margin_rzrqye = margin.get("margin_rzrqye")
    margin_rzrqye_yoy_pct = margin.get("margin_rzrqye_yoy_pct")
    margin_stance = margin.get("margin_stance")

    return RateSnapshot(
        apply_year=apply_year,
        snapshot_date=snap_date,
        shibor_on=float(sh["rate_on"]) if sh.get("rate_on") is not None else None,
        shibor_3m=shibor_3m,
        shibor_1y=float(sh["rate_1y"]) if sh.get("rate_1y") is not None else None,
        shibor_3m_yoy_bp=shibor_3m_yoy_bp,
        lpr_1y=lpr_1y,
        lpr_5y=lpr_5y,
        lpr_1y_yoy_bp=lpr_1y_yoy_bp,
        libor_3m_usd=float(lb["rate_3m"]) if lb and lb.get("rate_3m") is not None else None,
        liquidity_stance=liquidity,
        rate_trend=trend,
        cewc_monetary_policy=cewc_mp,
        policy_rate_gap=gap,
        notes="; ".join(notes_parts) if notes_parts else None,
        gdp_quarter=gdp["quarter"] if gdp else None,
        gdp_yoy=gdp_yoy,
        si_yoy=si_yoy,
        ti_yoy=ti_yoy,
        growth_stance=classify_growth(gdp_yoy),
        price_month=price["month"] if price else None,
        cpi_yoy=cpi_yoy,
        cpi_accu=cpi_accu,
        ppi_yoy=ppi_yoy,
        ppi_accu=ppi_accu,
        ppi_cpi_spread=ppi_cpi_spread,
        inflation_stance=classify_inflation(cpi_yoy, ppi_yoy),
        money_month=money["month"] if money else None,
        m1_yoy=m1_yoy,
        m2_yoy=m2_yoy,
        m1_m2_scissors=m1_m2_scissors,
        money_stance=classify_money(m1_yoy, m2_yoy, m1_m2_scissors),
        sf_month=sf["month"] if sf else None,
        sf_inc_cumval=sf_inc_cumval,
        sf_stk_endval=sf_stk_endval,
        sf_stk_yoy=sf_stk_yoy,
        sf_stance=classify_sf(sf_stk_yoy, sf_inc_cumval),
        pmi_month=pmi["month"] if pmi else None,
        pmi_mfg=pmi_mfg,
        pmi_non_mfg=pmi_non_mfg,
        pmi_composite=pmi_composite,
        pmi_stance=classify_pmi(pmi_mfg, pmi_non_mfg),
        us_rate_date=us_rate_date,
        us_10y_nominal=us_10y_nominal,
        us_10y_real=us_10y_real,
        us_tbill_13w=us_tbill_13w,
        us_10y_real_yoy_bp=us_10y_real_yoy_bp,
        global_rate_stance=classify_global_rate(us_10y_real, us_10y_nominal),
        pboc_report_date=pboc_report_date,
        pboc_report_title=pboc_report_title,
        corpus_note=corpus_note,
        valuation_date=valuation_date,
        hs300_pe=hs300_pe,
        hs300_pe_ttm=hs300_pe_ttm,
        hs300_pb=hs300_pb,
        sz50_pe_ttm=sz50_pe_ttm,
        sz50_pb=sz50_pb,
        zz500_pe_ttm=zz500_pe_ttm,
        zz500_pb=zz500_pb,
        valuation_stance=valuation_stance,
        margin_date=margin_date,
        margin_rzye=margin_rzye,
        margin_rqye=margin_rqye,
        margin_rzrqye=margin_rzrqye,
        margin_rzrqye_yoy_pct=margin_rzrqye_yoy_pct,
        margin_stance=margin_stance,
    )


def upsert_snapshot(conn, snap: RateSnapshot) -> None:
    sql = """
        INSERT INTO macro_annual_snapshot (
            apply_year, snapshot_date,
            shibor_on, shibor_3m, shibor_1y, shibor_3m_yoy_bp,
            lpr_1y, lpr_5y, lpr_1y_yoy_bp, libor_3m_usd,
            liquidity_stance, rate_trend, cewc_monetary_policy, policy_rate_gap, notes,
            gdp_quarter, gdp_yoy, si_yoy, ti_yoy, growth_stance,
            price_month, cpi_yoy, cpi_accu, ppi_yoy, ppi_accu, ppi_cpi_spread, inflation_stance,
            money_month, m1_yoy, m2_yoy, m1_m2_scissors, money_stance,
            sf_month, sf_inc_cumval, sf_stk_endval, sf_stk_yoy, sf_stance,
            pmi_month, pmi_mfg, pmi_non_mfg, pmi_composite, pmi_stance,
            us_rate_date, us_10y_nominal, us_10y_real, us_tbill_13w, us_10y_real_yoy_bp, global_rate_stance,
            pboc_report_date, pboc_report_title, corpus_note,
            valuation_date, hs300_pe, hs300_pe_ttm, hs300_pb,
            sz50_pe_ttm, sz50_pb, zz500_pe_ttm, zz500_pb, valuation_stance,
            margin_date, margin_rzye, margin_rqye, margin_rzrqye, margin_rzrqye_yoy_pct, margin_stance
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            snapshot_date=VALUES(snapshot_date),
            shibor_on=VALUES(shibor_on), shibor_3m=VALUES(shibor_3m), shibor_1y=VALUES(shibor_1y),
            shibor_3m_yoy_bp=VALUES(shibor_3m_yoy_bp),
            lpr_1y=VALUES(lpr_1y), lpr_5y=VALUES(lpr_5y), lpr_1y_yoy_bp=VALUES(lpr_1y_yoy_bp),
            libor_3m_usd=VALUES(libor_3m_usd),
            liquidity_stance=VALUES(liquidity_stance), rate_trend=VALUES(rate_trend),
            cewc_monetary_policy=VALUES(cewc_monetary_policy),
            policy_rate_gap=VALUES(policy_rate_gap), notes=VALUES(notes),
            gdp_quarter=VALUES(gdp_quarter), gdp_yoy=VALUES(gdp_yoy),
            si_yoy=VALUES(si_yoy), ti_yoy=VALUES(ti_yoy), growth_stance=VALUES(growth_stance),
            price_month=VALUES(price_month), cpi_yoy=VALUES(cpi_yoy), cpi_accu=VALUES(cpi_accu),
            ppi_yoy=VALUES(ppi_yoy), ppi_accu=VALUES(ppi_accu),
            ppi_cpi_spread=VALUES(ppi_cpi_spread), inflation_stance=VALUES(inflation_stance),
            money_month=VALUES(money_month), m1_yoy=VALUES(m1_yoy), m2_yoy=VALUES(m2_yoy),
            m1_m2_scissors=VALUES(m1_m2_scissors), money_stance=VALUES(money_stance),
            sf_month=VALUES(sf_month), sf_inc_cumval=VALUES(sf_inc_cumval),
            sf_stk_endval=VALUES(sf_stk_endval), sf_stk_yoy=VALUES(sf_stk_yoy),
            sf_stance=VALUES(sf_stance),
            pmi_month=VALUES(pmi_month), pmi_mfg=VALUES(pmi_mfg), pmi_non_mfg=VALUES(pmi_non_mfg),
            pmi_composite=VALUES(pmi_composite), pmi_stance=VALUES(pmi_stance),
            us_rate_date=VALUES(us_rate_date), us_10y_nominal=VALUES(us_10y_nominal),
            us_10y_real=VALUES(us_10y_real), us_tbill_13w=VALUES(us_tbill_13w),
            us_10y_real_yoy_bp=VALUES(us_10y_real_yoy_bp), global_rate_stance=VALUES(global_rate_stance),
            pboc_report_date=VALUES(pboc_report_date), pboc_report_title=VALUES(pboc_report_title),
            corpus_note=VALUES(corpus_note),
            valuation_date=VALUES(valuation_date),
            hs300_pe=VALUES(hs300_pe), hs300_pe_ttm=VALUES(hs300_pe_ttm), hs300_pb=VALUES(hs300_pb),
            sz50_pe_ttm=VALUES(sz50_pe_ttm), sz50_pb=VALUES(sz50_pb),
            zz500_pe_ttm=VALUES(zz500_pe_ttm), zz500_pb=VALUES(zz500_pb),
            valuation_stance=VALUES(valuation_stance),
            margin_date=VALUES(margin_date),
            margin_rzye=VALUES(margin_rzye), margin_rqye=VALUES(margin_rqye),
            margin_rzrqye=VALUES(margin_rzrqye), margin_rzrqye_yoy_pct=VALUES(margin_rzrqye_yoy_pct),
            margin_stance=VALUES(margin_stance)
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                snap.apply_year,
                snap.snapshot_date,
                snap.shibor_on,
                snap.shibor_3m,
                snap.shibor_1y,
                snap.shibor_3m_yoy_bp,
                snap.lpr_1y,
                snap.lpr_5y,
                snap.lpr_1y_yoy_bp,
                snap.libor_3m_usd,
                snap.liquidity_stance,
                snap.rate_trend,
                snap.cewc_monetary_policy,
                snap.policy_rate_gap,
                snap.notes,
                snap.gdp_quarter,
                snap.gdp_yoy,
                snap.si_yoy,
                snap.ti_yoy,
                snap.growth_stance,
                snap.price_month,
                snap.cpi_yoy,
                snap.cpi_accu,
                snap.ppi_yoy,
                snap.ppi_accu,
                snap.ppi_cpi_spread,
                snap.inflation_stance,
                snap.money_month,
                snap.m1_yoy,
                snap.m2_yoy,
                snap.m1_m2_scissors,
                snap.money_stance,
                snap.sf_month,
                snap.sf_inc_cumval,
                snap.sf_stk_endval,
                snap.sf_stk_yoy,
                snap.sf_stance,
                snap.pmi_month,
                snap.pmi_mfg,
                snap.pmi_non_mfg,
                snap.pmi_composite,
                snap.pmi_stance,
                snap.us_rate_date,
                snap.us_10y_nominal,
                snap.us_10y_real,
                snap.us_tbill_13w,
                snap.us_10y_real_yoy_bp,
                snap.global_rate_stance,
                snap.pboc_report_date,
                snap.pboc_report_title,
                snap.corpus_note,
                snap.valuation_date,
                snap.hs300_pe,
                snap.hs300_pe_ttm,
                snap.hs300_pb,
                snap.sz50_pe_ttm,
                snap.sz50_pb,
                snap.zz500_pe_ttm,
                snap.zz500_pb,
                snap.valuation_stance,
                snap.margin_date,
                snap.margin_rzye,
                snap.margin_rqye,
                snap.margin_rzrqye,
                snap.margin_rzrqye_yoy_pct,
                snap.margin_stance,
            ),
        )
    conn.commit()


def rebuild_annual_snapshots(conn, start_year: int = 2007, end_year: int | None = None) -> int:
    """Rebuild macro_annual_snapshot for apply_year range (SHIBOR from 2006 → 2007+)."""
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(apply_year), MAX(apply_year) FROM cewc_annual")
        cewc_min, cewc_max = cur.fetchone()

    if end_year is None:
        end_year = cewc_max or date.today().year
    if cewc_min:
        start_year = max(start_year, cewc_min)

    count = 0
    for y in range(start_year, end_year + 1):
        snap = build_snapshot_for_year(conn, y)
        if snap:
            upsert_snapshot(conn, snap)
            count += 1
    return count


def load_snapshot(conn, apply_year: int) -> dict[str, Any] | None:
    """Load annual snapshot for strategic allocation."""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM macro_annual_snapshot WHERE apply_year = %s", (apply_year,))
        return cur.fetchone()


def annual_macro_brief(conn, apply_year: int) -> str:
    """Human-readable summary for 年初定方向."""
    snap = load_snapshot(conn, apply_year)
    if not snap:
        return f"{apply_year} 年暂无宏观快照"

    lines = [
        f"=== {apply_year} 年初宏观快照 (截止 {snap['snapshot_date']}) ===",
        f"CEWC货币定调: {snap.get('cewc_monetary_policy') or '—'}",
    ]
    if snap.get("pboc_report_date"):
        lines.append(
            f"央行货政报告 ({snap['pboc_report_date']}): {snap.get('pboc_report_title')}"
        )
    if snap.get("corpus_note"):
        lines.append(f"语料库: {snap.get('corpus_note')}")
    if snap.get("gdp_quarter"):
        lines.append(
            f"GDP {snap['gdp_quarter']}: 同比 {snap.get('gdp_yoy')}% "
            f"(二产 {snap.get('si_yoy')}%, 三产 {snap.get('ti_yoy')}%) → {snap.get('growth_stance')}"
        )
    if snap.get("price_month"):
        spread = snap.get("ppi_cpi_spread")
        spread_txt = f", PPI-CPI {spread:+.2f}pp" if spread is not None else ""
        lines.append(
            f"价格 {snap['price_month']}: CPI {snap.get('cpi_yoy')}% / PPI {snap.get('ppi_yoy')}%"
            f"{spread_txt} → {snap.get('inflation_stance')}"
        )
    if snap.get("money_month"):
        scissors = snap.get("m1_m2_scissors")
        scissors_txt = f", 剪刀差 {scissors:+.2f}pp" if scissors is not None else ""
        lines.append(
            f"货币 {snap['money_month']}: M1 {snap.get('m1_yoy')}% / M2 {snap.get('m2_yoy')}%"
            f"{scissors_txt} → {snap.get('money_stance')}"
        )
    if snap.get("sf_month"):
        cumval = snap.get("sf_inc_cumval")
        cumval_txt = f", 全年增量 {cumval/10000:.1f}万亿" if cumval else ""
        stk_yoy = snap.get("sf_stk_yoy")
        stk_txt = f", 存量同比 {stk_yoy}%" if stk_yoy is not None else ""
        lines.append(
            f"社融 {snap['sf_month']}: 存量 {snap.get('sf_stk_endval')}万亿"
            f"{stk_txt}{cumval_txt} → {snap.get('sf_stance')}"
        )
    if snap.get("pmi_month"):
        comp = snap.get("pmi_composite")
        comp_txt = f", 综合 {comp}" if comp is not None else ""
        lines.append(
            f"PMI {snap['pmi_month']}: 制造 {snap.get('pmi_mfg')} / 非制造 {snap.get('pmi_non_mfg')}"
            f"{comp_txt} → {snap.get('pmi_stance')}"
        )
    if snap.get("hs300_pe_ttm") is not None or snap.get("sz50_pe_ttm") is not None:
        val_parts = []
        if snap.get("hs300_pe_ttm") is not None:
            val_parts.append(
                f"沪深300 PE-TTM {snap['hs300_pe_ttm']}% / PB {snap.get('hs300_pb')}"
            )
        if snap.get("sz50_pe_ttm") is not None:
            val_parts.append(f"上证50 PE-TTM {snap['sz50_pe_ttm']}% / PB {snap.get('sz50_pb')}")
        if snap.get("zz500_pe_ttm") is not None:
            val_parts.append(f"中证500 PE-TTM {snap['zz500_pe_ttm']}% / PB {snap.get('zz500_pb')}")
        lines.append(
            f"估值 (截止 {snap.get('valuation_date')}): "
            + " | ".join(val_parts)
            + f" → {snap.get('valuation_stance')}"
        )
    if snap.get("margin_rzrqye") is not None:
        yoy = snap.get("margin_rzrqye_yoy_pct")
        yoy_txt = f", 同比 {yoy:+.2f}%" if yoy is not None else ""
        margin_trn = float(snap["margin_rzrqye"]) / 1e12
        lines.append(
            f"两融 (截止 {snap.get('margin_date')}): 余额 {margin_trn:.3f}万亿"
            f"{yoy_txt} → {snap.get('margin_stance')}"
        )
    if snap.get("us_10y_real") is not None or snap.get("us_10y_nominal") is not None:
        yoy = snap.get("us_10y_real_yoy_bp")
        yoy_txt = f", 实际10Y同比 {yoy}bp" if yoy is not None else ""
        nom = snap.get("us_10y_nominal")
        nom_txt = f" / 名义 {nom}%" if nom is not None else ""
        tbill = snap.get("us_tbill_13w")
        tbill_txt = f", 13周 {tbill}%" if tbill is not None else ""
        lines.append(
            f"美债 (截止 {snap.get('us_rate_date')}): 实际10Y {snap.get('us_10y_real')}%"
            f"{nom_txt}{tbill_txt}{yoy_txt} → {snap.get('global_rate_stance')}"
        )
    lines.extend(
        [
            f"SHIBOR 3M: {snap.get('shibor_3m')}% (同比 {snap.get('shibor_3m_yoy_bp')} bp)",
            f"SHIBOR 1Y: {snap.get('shibor_1y')}%",
        ]
    )
    if snap.get("lpr_1y") is not None:
        lines.append(f"LPR 1Y/5Y: {snap['lpr_1y']}% / {snap.get('lpr_5y')}%")
    if snap.get("libor_3m_usd") is not None:
        lines.append(f"LIBOR 3M (USD): {snap['libor_3m_usd']}%")
    lines.extend(
        [
            f"流动性判断: {snap.get('liquidity_stance')} | 利率趋势: {snap.get('rate_trend')}",
            f"政策-利率: {snap.get('policy_rate_gap')}",
        ]
    )
    return "\n".join(lines)
