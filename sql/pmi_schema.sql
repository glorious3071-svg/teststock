-- PMI monthly data (Tushare cn_pmi, doc 325)

CREATE TABLE IF NOT EXISTS cn_pmi_monthly (
    month           CHAR(6)        NOT NULL COMMENT '月份 YYYYMM',
    cal_year        SMALLINT       NOT NULL,
    cal_month       TINYINT        NOT NULL,
    pmi_mfg         DECIMAL(5,2)   NULL COMMENT '制造业PMI',
    pmi_production  DECIMAL(5,2)   NULL COMMENT '生产指数',
    pmi_new_order   DECIMAL(5,2)   NULL COMMENT '新订单指数',
    pmi_non_mfg     DECIMAL(5,2)   NULL COMMENT '非制造业商务活动',
    pmi_composite   DECIMAL(5,2)   NULL COMMENT '综合PMI产出',
    created_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (month),
    KEY idx_pmi_year (cal_year, cal_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
