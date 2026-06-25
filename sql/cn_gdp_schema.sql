-- GDP quarterly data (Tushare cn_gdp, doc 227)

CREATE TABLE IF NOT EXISTS cn_gdp_quarterly (
    quarter      VARCHAR(7)     NOT NULL COMMENT '季度，如 2024Q4',
    cal_year     SMALLINT       NOT NULL COMMENT '日历年',
    cal_quarter  TINYINT        NOT NULL COMMENT '季度 1-4',
    gdp          DECIMAL(16,2)  NULL COMMENT 'GDP累计值（亿元）',
    gdp_yoy      DECIMAL(6,2)   NULL COMMENT '当季同比增速（%）',
    pi           DECIMAL(16,2)  NULL COMMENT '第一产业累计值（亿元）',
    pi_yoy       DECIMAL(6,2)   NULL COMMENT '第一产业同比增速（%）',
    si           DECIMAL(16,2)  NULL COMMENT '第二产业累计值（亿元）',
    si_yoy       DECIMAL(6,2)   NULL COMMENT '第二产业同比增速（%）',
    ti           DECIMAL(16,2)  NULL COMMENT '第三产业累计值（亿元）',
    ti_yoy       DECIMAL(6,2)   NULL COMMENT '第三产业同比增速（%）',
    created_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (quarter),
    KEY idx_cn_gdp_year (cal_year, cal_quarter)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
