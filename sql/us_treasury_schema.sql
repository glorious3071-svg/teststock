-- US Treasury rates (Tushare docs 219-223)

CREATE TABLE IF NOT EXISTS us_tycr_daily (
    trade_date   DATE          NOT NULL COMMENT '交易日',
    m1           DECIMAL(8,4)  NULL,
    m2           DECIMAL(8,4)  NULL,
    m3           DECIMAL(8,4)  NULL,
    m4           DECIMAL(8,4)  NULL,
    m6           DECIMAL(8,4)  NULL,
    y1           DECIMAL(8,4)  NULL,
    y2           DECIMAL(8,4)  NULL,
    y3           DECIMAL(8,4)  NULL,
    y5           DECIMAL(8,4)  NULL,
    y7           DECIMAL(8,4)  NULL,
    y10          DECIMAL(8,4)  NULL COMMENT '10年期名义收益率',
    y20          DECIMAL(8,4)  NULL,
    y30          DECIMAL(8,4)  NULL,
    created_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS us_trycr_daily (
    trade_date   DATE          NOT NULL COMMENT '交易日',
    y5           DECIMAL(8,4)  NULL,
    y7           DECIMAL(8,4)  NULL,
    y10          DECIMAL(8,4)  NULL COMMENT '10年期实际收益率',
    y20          DECIMAL(8,4)  NULL,
    y30          DECIMAL(8,4)  NULL,
    created_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS us_tbr_daily (
    trade_date   DATE          NOT NULL COMMENT '交易日',
    w4_ce        DECIMAL(8,4)  NULL COMMENT '4周票面利率',
    w8_ce        DECIMAL(8,4)  NULL COMMENT '8周票面利率',
    w13_ce       DECIMAL(8,4)  NULL COMMENT '13周(3月)票面利率',
    w26_ce       DECIMAL(8,4)  NULL COMMENT '26周票面利率',
    w52_ce       DECIMAL(8,4)  NULL COMMENT '52周(1年)票面利率',
    created_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS us_tltr_daily (
    trade_date   DATE          NOT NULL COMMENT '交易日',
    ltc          DECIMAL(8,4)  NULL COMMENT '长期综合收益率',
    cmt          DECIMAL(8,4)  NULL COMMENT '20年期CMT',
    e_factor     DECIMAL(10,6) NULL COMMENT '外推因子',
    created_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS us_trltr_daily (
    trade_date   DATE          NOT NULL COMMENT '交易日',
    ltr_avg      DECIMAL(8,4)  NULL COMMENT '10年+实际平均利率',
    created_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
