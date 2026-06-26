CREATE TABLE IF NOT EXISTS index_daily (
    ts_code      VARCHAR(20)    NOT NULL COMMENT '指数代码',
    trade_date   DATE           NOT NULL COMMENT '交易日',
    open         DECIMAL(10,4)  NULL     COMMENT '开盘点位',
    high         DECIMAL(10,4)  NULL     COMMENT '最高点位',
    low          DECIMAL(10,4)  NULL     COMMENT '最低点位',
    close        DECIMAL(10,4)  NULL     COMMENT '收盘点位',
    pre_close    DECIMAL(10,4)  NULL     COMMENT '昨收点位',
    change_pt    DECIMAL(10,4)  NULL     COMMENT '涨跌点',
    pct_chg      DECIMAL(10,4)  NULL     COMMENT '涨跌幅(%)',
    vol          DECIMAL(20,2)  NULL     COMMENT '成交量(手)',
    amount       DECIMAL(20,4)  NULL     COMMENT '成交额(千元)',
    created_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='主要指数日行情（Tushare index_daily）';
