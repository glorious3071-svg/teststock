CREATE TABLE IF NOT EXISTS etf_share_size_snapshot (
  ts_code         VARCHAR(20)    NOT NULL,
  trade_date      DATE           NOT NULL COMMENT 'Exchange observation date',
  available_date  DATE           NOT NULL COMMENT 'Conservative point-in-time availability: trade_date plus one calendar day',
  total_share_wan DECIMAL(24,4)  NOT NULL COMMENT 'Total ETF shares in 10,000 units',
  total_size_wan  DECIMAL(24,4)  NULL COMMENT 'Total ETF size in CNY 10,000',
  nav             DECIMAL(18,8)  NULL,
  close_price     DECIMAL(18,8)  NULL,
  exchange        VARCHAR(8)     NULL,
  source          VARCHAR(32)    NOT NULL DEFAULT 'tushare_etf_share_size',
  updated_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_etf_share_size_available (available_date, ts_code),
  KEY idx_etf_share_size_trade (trade_date, ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Point-in-time exchange ETF share and size snapshots';
