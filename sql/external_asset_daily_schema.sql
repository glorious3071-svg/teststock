CREATE TABLE IF NOT EXISTS external_asset_daily (
  symbol      VARCHAR(32)    NOT NULL COMMENT 'External market symbol, e.g. SPY/QQQ/TLT/^VIX',
  trade_date  DATE           NOT NULL COMMENT 'Trading date',
  open        DECIMAL(18,6)  NULL,
  high        DECIMAL(18,6)  NULL,
  low         DECIMAL(18,6)  NULL,
  close       DECIMAL(18,6)  NULL,
  adj_close   DECIMAL(18,6)  NULL COMMENT 'Adjusted close when provided by source',
  volume      BIGINT         NULL,
  source      VARCHAR(32)    NOT NULL DEFAULT 'yahoo_chart',
  updated_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (symbol, trade_date),
  KEY idx_external_asset_daily_date (trade_date),
  KEY idx_external_asset_daily_symbol_date (symbol, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='External ETF/index/proxy daily prices for portfolio hedge and defensive-asset tests';
