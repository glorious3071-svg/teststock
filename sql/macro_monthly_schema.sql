-- 月频宏观指标（长格式）
--
-- 用于年度行业/题材判断框架的宏观底层数据。
-- 长格式 (indicator, period, value) 便于横向扩展任意新指标，
-- 查询时按 indicator 过滤或 PIVOT 宽化。
--
-- 指标清单（indicator 取值）：
--   pmi_mfg          制造业PMI（50 为荣枯线）
--   pmi_non_mfg      非制造业PMI（服务业景气）
--   ppi_yoy          PPI 同比 %
--   ppi_mom          PPI 环比 %
--   cpi_yoy          CPI 同比 %
--   cpi_mom          CPI 环比 %
--   m2_yoy           M2 同比增速 %
--   m2_balance       M2 余额 亿元
--   m1_yoy           M1 同比增速 %
--   sf_inc_month     社会融资规模月增量 亿元
--   iva_yoy          规模以上工业增加值 YoY %
--   rrr_large        大型金融机构存款准备金率 %（按生效月份展开）

CREATE TABLE IF NOT EXISTS macro_monthly (
    indicator   VARCHAR(60)    NOT NULL COMMENT '指标代码',
    period      DATE           NOT NULL COMMENT '统计月份（当月1日，如 2024-01-01）',
    value       DECIMAL(14,4)  NULL     COMMENT '指标值（单位见表头注释）',
    created_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (indicator, period),
    KEY idx_macro_period (period)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='月频宏观指标，长格式（AkShare + Tushare 混合源）';
