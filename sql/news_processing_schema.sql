-- News processing layer: mention tracking, event clusters, daily theme signals

CREATE TABLE IF NOT EXISTS news_mention_counter (
    content_hash        CHAR(32)        NOT NULL,
    canonical_article_id BIGINT UNSIGNED NOT NULL,
    mention_count       INT             NOT NULL DEFAULT 1,
    first_seen_at       DATETIME        NOT NULL,
    last_seen_at        DATETIME        NOT NULL,
    PRIMARY KEY (content_hash),
    KEY idx_canonical (canonical_article_id),
    CONSTRAINT fk_mention_article FOREIGN KEY (canonical_article_id)
        REFERENCES news_article(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Exact-duplicate mention counts at L0 without weakening signal';

CREATE TABLE IF NOT EXISTS news_event (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_fingerprint   CHAR(32)        NOT NULL COMMENT 'md5 normalized title prefix',
    canonical_article_id BIGINT UNSIGNED NOT NULL,
    title_norm          VARCHAR(500)    NOT NULL,
    category            VARCHAR(20)     NOT NULL,
    first_seen          DATETIME        NOT NULL,
    last_seen           DATETIME        NOT NULL,
    mention_count       INT             NOT NULL DEFAULT 1,
    unique_sources      INT             NOT NULL DEFAULT 1,
    sources_json        JSON            NULL COMMENT 'list of source names',
    duration_days       INT             NOT NULL DEFAULT 1,
    status              ENUM('open','closed') NOT NULL DEFAULT 'open',
    created_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_fingerprint_seen (event_fingerprint, last_seen),
    KEY idx_status_seen (status, last_seen),
    KEY idx_canonical (canonical_article_id),
    CONSTRAINT fk_event_canonical FOREIGN KEY (canonical_article_id)
        REFERENCES news_article(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Logical news event cluster (L1)';

CREATE TABLE IF NOT EXISTS news_event_member (
    event_id            BIGINT UNSIGNED NOT NULL,
    article_id          BIGINT UNSIGNED NOT NULL,
    source              VARCHAR(40)     NOT NULL,
    is_canonical        TINYINT(1)      NOT NULL DEFAULT 0,
    joined_at           DATETIME        NOT NULL,
    PRIMARY KEY (event_id, article_id),
    UNIQUE KEY uk_article (article_id),
    KEY idx_event (event_id),
    CONSTRAINT fk_member_event FOREIGN KEY (event_id)
        REFERENCES news_event(id) ON DELETE CASCADE,
    CONSTRAINT fk_member_article FOREIGN KEY (article_id)
        REFERENCES news_article(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Article membership in event clusters';

CREATE TABLE IF NOT EXISTS theme_news_daily (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    signal_date         DATE            NOT NULL,
    theme               VARCHAR(50)     NOT NULL,
    net_score           DECIMAL(10,4)   NOT NULL DEFAULT 0,
    bull_score          DECIMAL(10,4)   NOT NULL DEFAULT 0,
    bear_score          DECIMAL(10,4)   NOT NULL DEFAULT 0,
    event_count         INT             NOT NULL DEFAULT 0,
    mention_count       INT             NOT NULL DEFAULT 0,
    source_diversity    INT             NOT NULL DEFAULT 0,
    avg_magnitude       DECIMAL(4,2)    NULL,
    avg_confidence      DECIMAL(4,2)    NULL,
    model_version       VARCHAR(50)     NOT NULL DEFAULT 'salience_v1',
    created_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_date_theme (signal_date, theme),
    KEY idx_signal_date (signal_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Daily theme signals with salience weighting (L3)';

CREATE TABLE IF NOT EXISTS news_processing_run (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    run_date            DATE            NOT NULL,
    started_at          DATETIME        NOT NULL,
    finished_at         DATETIME        NULL,
    status              ENUM('running','success','partial','failed') NOT NULL DEFAULT 'running',
    articles_scanned    INT             NOT NULL DEFAULT 0,
    events_created      INT             NOT NULL DEFAULT 0,
    events_updated      INT             NOT NULL DEFAULT 0,
    extractions_run     INT             NOT NULL DEFAULT 0,
    daily_themes        INT             NOT NULL DEFAULT 0,
    error_msg           TEXT            NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_run_date (run_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Daily batch processing audit log';

-- Extend news_extraction with optional event link (idempotent ALTER)
-- Run via ensure_processing_schema() which catches duplicate column errors
