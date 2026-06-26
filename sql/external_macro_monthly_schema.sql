-- v11 外部宏观月度衍生特征
-- 数据来源:
--   gold_yoy_pct  -> gold_daily (GC.FOREIGN)
--   vix_30d_avg   -> cboe_vix_daily
--   fed_rate_level -> global_cb_rate_events (cb_code='FED')
-- 评分卡用途: backtest/scorecard.py v11-GVF 三条外部规则

CREATE TABLE IF NOT EXISTS external_macro_monthly (
    month             CHAR(6)        NOT NULL  COMMENT '月份 YYYYMM',
    cal_year          SMALLINT       NOT NULL,
    cal_month         TINYINT        NOT NULL,
    -- 原始值
    vix_30d_avg       DECIMAL(8,2)   NULL      COMMENT 'VIX 月均（恐慌指数）',
    vix_30d_max       DECIMAL(8,2)   NULL      COMMENT 'VIX 月内最大值',
    vix_30d_min       DECIMAL(8,2)   NULL      COMMENT 'VIX 月内最小值',
    gold_close        DECIMAL(10,2)  NULL      COMMENT '黄金月末收盘（GC.FOREIGN USD/oz）',
    gold_yoy_pct      DECIMAL(8,2)   NULL      COMMENT '黄金 12 月 YoY %',
    fed_rate_level    DECIMAL(6,2)   NULL      COMMENT 'FED 利率月末水平 %',
    us10y_yield       DECIMAL(6,2)   NULL      COMMENT '美 10Y 名义收益率月末 %',
    spx_close         DECIMAL(10,2)  NULL      COMMENT 'SPX 月末',
    spx_yoy_pct       DECIMAL(8,2)   NULL      COMMENT 'SPX 12 月 YoY %',
    -- v11 评分卡触发标记
    trig_gold_yoy25   BOOL           NOT NULL DEFAULT FALSE COMMENT 'v11-G: gold_yoy > 25%',
    trig_vix_30plus   BOOL           NOT NULL DEFAULT FALSE COMMENT 'v11-V: vix_30d_avg > 30',
    trig_fed_45plus   BOOL           NOT NULL DEFAULT FALSE COMMENT 'v11-F: fed_rate_level >= 4.5',
    created_at        TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (month),
    KEY idx_ext_year (cal_year, cal_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='外部宏观月度衍生特征（v11 评分卡 GVF 三规则用）';
