-- Money supply monthly data (Tushare cn_m, doc 242)

CREATE TABLE IF NOT EXISTS cn_m_monthly (
    month       CHAR(6)         NOT NULL COMMENT '月份 YYYYMM',
    cal_year    SMALLINT        NOT NULL,
    cal_month   TINYINT         NOT NULL,
    m0          DECIMAL(16,2)   NULL COMMENT 'M0（亿元）',
    m0_yoy      DECIMAL(6,2)    NULL COMMENT 'M0同比%',
    m0_mom      DECIMAL(6,2)    NULL COMMENT 'M0环比%',
    m1          DECIMAL(16,2)   NULL COMMENT 'M1（亿元）',
    m1_yoy      DECIMAL(6,2)    NULL COMMENT 'M1同比%',
    m1_mom      DECIMAL(6,2)    NULL COMMENT 'M1环比%',
    m2          DECIMAL(16,2)   NULL COMMENT 'M2（亿元）',
    m2_yoy      DECIMAL(6,2)    NULL COMMENT 'M2同比%',
    m2_mom      DECIMAL(6,2)    NULL COMMENT 'M2环比%',
    created_at  TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (month),
    KEY idx_cn_m_year (cal_year, cal_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
