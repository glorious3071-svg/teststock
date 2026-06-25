-- Major index daily valuation (Tushare index_dailybasic, doc_id=128)

CREATE TABLE IF NOT EXISTS index_dailybasic (
    ts_code          VARCHAR(20)    NOT NULL COMMENT '指数代码',
    trade_date       DATE           NOT NULL COMMENT '交易日',
    total_mv         DECIMAL(20,2)  NULL COMMENT '总市值(元)',
    float_mv         DECIMAL(20,2)  NULL COMMENT '流通市值(元)',
    total_share      DECIMAL(20,2)  NULL COMMENT '总股本(股)',
    float_share      DECIMAL(20,2)  NULL COMMENT '流通股本(股)',
    free_share       DECIMAL(20,2)  NULL COMMENT '自由流通股本(股)',
    turnover_rate    DECIMAL(10,4)  NULL COMMENT '换手率%',
    turnover_rate_f  DECIMAL(10,4)  NULL COMMENT '换手率(自由流通)%',
    pe               DECIMAL(10,4)  NULL COMMENT '市盈率',
    pe_ttm           DECIMAL(10,4)  NULL COMMENT '市盈率TTM',
    pb               DECIMAL(10,4)  NULL COMMENT '市净率',
    created_at       TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, trade_date),
    KEY idx_index_dailybasic_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
