-- Price index monthly data (Tushare cn_cpi doc 228, cn_ppi doc 245)

CREATE TABLE IF NOT EXISTS cn_cpi_monthly (
    month        CHAR(6)        NOT NULL COMMENT '月份 YYYYMM',
    cal_year     SMALLINT       NOT NULL,
    cal_month    TINYINT        NOT NULL,
    nt_yoy       DECIMAL(6,2)   NULL COMMENT '全国同比%',
    nt_mom       DECIMAL(6,2)   NULL COMMENT '全国环比%',
    nt_accu      DECIMAL(6,2)   NULL COMMENT '全国累计同比%',
    town_yoy     DECIMAL(6,2)   NULL COMMENT '城市同比%',
    cnt_yoy      DECIMAL(6,2)   NULL COMMENT '农村同比%',
    created_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (month),
    KEY idx_cpi_year (cal_year, cal_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cn_ppi_monthly (
    month           CHAR(6)        NOT NULL COMMENT '月份 YYYYMM',
    cal_year        SMALLINT       NOT NULL,
    cal_month       TINYINT        NOT NULL,
    ppi_yoy         DECIMAL(6,2)   NULL COMMENT '全部工业品同比%',
    ppi_mp_yoy      DECIMAL(6,2)   NULL COMMENT '生产资料同比%',
    ppi_mp_qm_yoy   DECIMAL(6,2)   NULL COMMENT '采掘业同比%',
    ppi_mp_rm_yoy   DECIMAL(6,2)   NULL COMMENT '原料业同比%',
    ppi_mp_p_yoy    DECIMAL(6,2)   NULL COMMENT '加工业同比%',
    ppi_cg_yoy      DECIMAL(6,2)   NULL COMMENT '生活资料同比%',
    ppi_mom         DECIMAL(6,2)   NULL COMMENT '全部工业品环比%',
    ppi_accu        DECIMAL(6,2)   NULL COMMENT '全部工业品累计同比%',
    created_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (month),
    KEY idx_ppi_year (cal_year, cal_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
