-- ETF daily quotes (Tushare fund_daily doc_id=127)
--
-- 用于年度行业/题材判断框架的执行载体：实盘 ETF 不复权价。
-- pct_chg 字段含分红再投回报，累计年度收益建议用 pct_chg 而非裸 close。

CREATE TABLE IF NOT EXISTS fund_daily (
    ts_code      VARCHAR(20)    NOT NULL COMMENT 'ETF代码',
    trade_date   DATE           NOT NULL COMMENT '交易日',
    open         DECIMAL(10,4)  NULL COMMENT '开盘价',
    high         DECIMAL(10,4)  NULL COMMENT '最高价',
    low          DECIMAL(10,4)  NULL COMMENT '最低价',
    close        DECIMAL(10,4)  NULL COMMENT '收盘价（不复权）',
    pre_close    DECIMAL(10,4)  NULL COMMENT '昨收价',
    change_pt    DECIMAL(10,4)  NULL COMMENT '涨跌点（Tushare change 字段映射）',
    pct_chg      DECIMAL(10,4)  NULL COMMENT '涨跌幅(%)，含分红再投',
    vol          DECIMAL(20,2)  NULL COMMENT '成交量(手)',
    amount       DECIMAL(20,4)  NULL COMMENT '成交额(千元)',
    created_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, trade_date),
    KEY idx_fund_daily_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='ETF日行情（Tushare fund_daily）';
