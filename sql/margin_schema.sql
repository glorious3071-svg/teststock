-- Margin trading daily summary (Tushare margin, doc_id=58)

CREATE TABLE IF NOT EXISTS margin_daily (
    trade_date    DATE           NOT NULL COMMENT '交易日',
    exchange_id   VARCHAR(8)     NOT NULL COMMENT 'SSE/SZSE/BSE',
    rzye          DECIMAL(20,2)  NULL COMMENT '融资余额(元)',
    rzmre         DECIMAL(20,2)  NULL COMMENT '融资买入额(元)',
    rzche         DECIMAL(20,2)  NULL COMMENT '融资偿还额(元)',
    rqye          DECIMAL(20,2)  NULL COMMENT '融券余额(元)',
    rqmcl         DECIMAL(20,2)  NULL COMMENT '融券卖出量',
    rzrqye        DECIMAL(20,2)  NULL COMMENT '融资融券余额(元)',
    rqyl          DECIMAL(20,2)  NULL COMMENT '融券余量',
    created_at    TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, exchange_id),
    KEY idx_margin_daily_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
