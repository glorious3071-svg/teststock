-- Domestic / global interest rate series for macro analysis
-- Sources: Tushare shibor (doc 149), shibor_lpr (doc 151), libor (doc 152)

CREATE TABLE IF NOT EXISTS shibor_daily (
    trade_date   DATE         NOT NULL COMMENT '交易日',
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS lpr_daily (
    trade_date   DATE         NOT NULL COMMENT '发布日',
    lpr_1y       DECIMAL(8,4) NULL COMMENT '1年期LPR',
    lpr_5y       DECIMAL(8,4) NULL COMMENT '5年期LPR',
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS libor_daily (
    trade_date   DATE         NOT NULL COMMENT '交易日',
    curr_type    VARCHAR(3)   NOT NULL DEFAULT 'USD' COMMENT '货币代码',
    rate_on      DECIMAL(8,5) NULL COMMENT '隔夜',
    rate_1w      DECIMAL(8,5) NULL COMMENT '1周',
    rate_1m      DECIMAL(8,5) NULL COMMENT '1月',
    rate_2m      DECIMAL(8,5) NULL COMMENT '2月',
    rate_3m      DECIMAL(8,5) NULL COMMENT '3月',
    rate_6m      DECIMAL(8,5) NULL COMMENT '6月',
    rate_12m     DECIMAL(8,5) NULL COMMENT '12月',
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, curr_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 年初宏观快照：每年第一个交易日前的利率状态，供战略层定方向
CREATE TABLE IF NOT EXISTS macro_annual_snapshot (
    apply_year            SMALLINT     NOT NULL COMMENT '政策适用年（与 cewc_annual.apply_year 对齐）',
    snapshot_date         DATE         NOT NULL COMMENT '取数截止日（通常上年末最后交易日）',
    shibor_on             DECIMAL(8,4) NULL COMMENT 'SHIBOR隔夜',
    shibor_3m             DECIMAL(8,4) NULL COMMENT 'SHIBOR 3月',
    shibor_1y             DECIMAL(8,4) NULL COMMENT 'SHIBOR 1年',
    shibor_3m_yoy_bp      DECIMAL(8,2) NULL COMMENT '3月SHIBOR同比变化(bp)',
    lpr_1y                DECIMAL(8,4) NULL COMMENT '1年期LPR',
    lpr_5y                DECIMAL(8,4) NULL COMMENT '5年期LPR',
    lpr_1y_yoy_bp         DECIMAL(8,2) NULL COMMENT '1年LPR同比变化(bp)',
    libor_3m_usd          DECIMAL(8,5) NULL COMMENT '美元LIBOR 3月',
    liquidity_stance      VARCHAR(20)  NULL COMMENT '宽松/中性/偏紧/紧缩',
    rate_trend            VARCHAR(20)  NULL COMMENT '下行/持平/上行',
    cewc_monetary_policy  VARCHAR(40)  NULL COMMENT '中央经济工作会议货币定调',
    policy_rate_gap       VARCHAR(40)  NULL COMMENT '政策表述 vs 利率走势是否一致',
  notes                   TEXT         NULL COMMENT '补充说明',
    created_at            TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (apply_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
