-- LLM corpus / policy text sources (Tushare docs 406, 415, 465, 143, 195, 154)

CREATE TABLE IF NOT EXISTS npr_policy (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    pubtime       DATETIME        NULL COMMENT '发布时间',
    title         VARCHAR(500)    NOT NULL,
    url           VARCHAR(1000)   NULL,
    pcode         VARCHAR(100)    NULL COMMENT '发文字号',
    puborg        VARCHAR(200)    NULL COMMENT '发文机关',
    ptype         VARCHAR(100)    NULL COMMENT '主题分类',
    content_html  LONGTEXT        NULL COMMENT '正文HTML',
    created_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_npr_pubtime (pubtime),
    KEY idx_npr_puborg (puborg),
    KEY idx_npr_ptype (ptype)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS broker_research_report (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    trade_date    DATE            NULL COMMENT '研报日期',
    title         VARCHAR(500)    NOT NULL,
    abstr         TEXT            NULL COMMENT '摘要',
    report_type   VARCHAR(40)     NULL COMMENT '个股研报/行业研报',
    author        VARCHAR(200)    NULL,
    name          VARCHAR(100)    NULL COMMENT '股票名称',
    ts_code       VARCHAR(20)     NULL,
    inst_csname   VARCHAR(100)    NULL COMMENT '券商简称',
    ind_name      VARCHAR(100)    NULL COMMENT '行业名称',
    url           VARCHAR(1000)   NULL,
    created_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_rr_date (trade_date),
    KEY idx_rr_type (report_type),
    KEY idx_rr_ind (ind_name),
    KEY idx_rr_code (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS pboc_monetary_policy (
    pub_date      DATE            NOT NULL COMMENT '发布日期',
    title         VARCHAR(500)    NOT NULL,
    url           VARCHAR(1000)   NULL,
    pdf_url       VARCHAR(1000)   NULL,
    content_html  LONGTEXT        NULL,
    created_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (pub_date, title(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS news_flash (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    src           VARCHAR(40)     NOT NULL COMMENT '来源标识',
    pub_time      DATETIME        NULL,
    title         VARCHAR(500)    NOT NULL,
    content       TEXT            NULL,
    created_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_news_src_time (src, pub_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS major_news_article (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    src           VARCHAR(40)     NULL,
    pub_time      DATETIME        NULL,
    title         VARCHAR(500)    NOT NULL,
    content       LONGTEXT        NULL,
    created_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_major_src_time (src, pub_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS cctv_news_daily (
    news_date     DATE            NOT NULL,
    title         VARCHAR(500)    NOT NULL,
    content       LONGTEXT        NULL,
    created_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (news_date, title(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
