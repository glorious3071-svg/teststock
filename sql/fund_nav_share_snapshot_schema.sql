CREATE TABLE IF NOT EXISTS fund_nav_share_snapshot (
  ts_code          VARCHAR(20)    NOT NULL,
  nav_date         DATE           NOT NULL COMMENT 'Fund NAV/report date',
  ann_date         DATE           NOT NULL COMMENT 'Public announcement date; point-in-time availability boundary',
  unit_nav         DECIMAL(18,8)  NOT NULL,
  net_asset        DECIMAL(24,4)  NOT NULL COMMENT 'Reported fund net assets in CNY',
  fund_share_units DECIMAL(24,4)  NOT NULL COMMENT 'Derived units = net_asset / unit_nav',
  source           VARCHAR(32)    NOT NULL DEFAULT 'tushare_fund_nav',
  updated_at       TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, nav_date, ann_date),
  KEY idx_fund_nav_share_snapshot_ann (ann_date, ts_code),
  KEY idx_fund_nav_share_snapshot_nav (nav_date, ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Point-in-time disclosed domestic passive ETF net assets and derived shares';
