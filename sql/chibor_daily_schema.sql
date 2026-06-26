-- 中国银行同业拆借市场 (CHIBOR) 利率
-- 数据源：akshare ak.rate_interbank(market='中国银行同业拆借市场', symbol='Chibor人民币')
-- 用途：V5.0 评分卡 rate_cum_bp_12m 的 SHIBOR fallback（SHIBOR 起点 2006-10-08 之前的窗口）
-- CHIBOR 起始日 2004-05-24（远早于 SHIBOR），可覆盖 2006 年评分卡所需的 2005-12-31 / 2004-12-31 对照

CREATE TABLE IF NOT EXISTS chibor_daily (
    trade_date   DATE         NOT NULL COMMENT '报告日',
    rate_on      DECIMAL(8,4) NULL COMMENT '隔夜',
    rate_1w      DECIMAL(8,4) NULL COMMENT '1周',
    rate_2w      DECIMAL(8,4) NULL COMMENT '2周',
    rate_1m      DECIMAL(8,4) NULL COMMENT '1月',
    rate_3m      DECIMAL(8,4) NULL COMMENT '3月',
    rate_6m      DECIMAL(8,4) NULL COMMENT '6月',
    rate_9m      DECIMAL(8,4) NULL COMMENT '9月',
    rate_1y      DECIMAL(8,4) NULL COMMENT '1年',
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='中国银行同业拆借市场 CHIBOR（akshare 东方财富源，SHIBOR pre-2006 fallback）';
