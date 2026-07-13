-- Daily news collection pipeline (raw layer + extraction + run logs)

CREATE TABLE IF NOT EXISTS news_article (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    content_hash    CHAR(32)        NOT NULL COMMENT 'md5 dedup key',
    source          VARCHAR(40)     NOT NULL COMMENT 'eastmoney/sina/ths/cctv/ndrc/research/cls/futu',
    category        VARCHAR(20)     NOT NULL COMMENT 'flash/policy/research/macro/intl/industry',
    pub_time        DATETIME        NULL,
    title           VARCHAR(500)    NOT NULL,
    body_text       LONGTEXT        NULL COMMENT 'plain text body',
    url             VARCHAR(1000)   NULL,
    author          VARCHAR(200)    NULL,
    lang            CHAR(5)         NOT NULL DEFAULT 'zh',
    extra_json      JSON            NULL COMMENT 'source-specific metadata',
    fetch_status    ENUM('ok','partial','failed') NOT NULL DEFAULT 'ok',
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_content_hash (content_hash),
    KEY idx_pub_time (pub_time),
    KEY idx_source_time (source, pub_time),
    KEY idx_category_time (category, pub_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Unified raw news articles for daily collection pipeline';

CREATE TABLE IF NOT EXISTS collect_run (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    run_id          VARCHAR(36)     NOT NULL COMMENT 'UUID batch id',
    collector       VARCHAR(40)     NOT NULL,
    started_at      DATETIME        NOT NULL,
    finished_at     DATETIME        NULL,
    status          ENUM('running','success','partial','failed') NOT NULL DEFAULT 'running',
    fetched         INT             NOT NULL DEFAULT 0,
    inserted        INT             NOT NULL DEFAULT 0,
    skipped_dup     INT             NOT NULL DEFAULT 0,
    error_msg       TEXT            NULL,
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_run_id (run_id),
    KEY idx_collector_time (collector, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Collector run audit log';

CREATE TABLE IF NOT EXISTS news_extraction (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    article_id      BIGINT UNSIGNED NOT NULL,
    extracted_at    DATETIME        NOT NULL,
    model           VARCHAR(50)     NOT NULL,
    sentiment       ENUM('bullish','bearish','neutral') NULL,
    themes          JSON            NULL COMMENT 'canonical theme labels',
    industries      JSON            NULL COMMENT 'industry names',
    ts_codes        JSON            NULL COMMENT 'related index codes',
    event_type      VARCHAR(30)     NULL COMMENT 'policy/trade/geopolitics/earnings/industry',
    magnitude       TINYINT         NULL COMMENT '1-3 impact strength',
    summary         VARCHAR(500)    NULL,
    reasoning       TEXT            NULL,
    confidence      DECIMAL(3,2)    NOT NULL DEFAULT 0.80,
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_article_model (article_id, model),
    KEY idx_extracted_at (extracted_at),
    KEY idx_sentiment (sentiment),
    CONSTRAINT fk_extraction_article FOREIGN KEY (article_id)
        REFERENCES news_article(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='LLM structured extraction from news_article';
