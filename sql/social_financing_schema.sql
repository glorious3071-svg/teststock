-- Social financing monthly data (Tushare sf_month, doc 310)

CREATE TABLE IF NOT EXISTS sf_monthly (
    month        CHAR(6)         NOT NULL COMMENT '月份 YYYYMM',
    cal_year     SMALLINT        NOT NULL,
    cal_month    TINYINT         NOT NULL,
    inc_month    DECIMAL(14,2)   NULL COMMENT '社融增量当月值（亿元）',
    inc_cumval   DECIMAL(14,2)   NULL COMMENT '社融增量累计值（亿元）',
    stk_endval   DECIMAL(10,2)   NULL COMMENT '社融存量期末值（万亿元）',
    created_at   TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (month),
    KEY idx_sf_year (cal_year, cal_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
