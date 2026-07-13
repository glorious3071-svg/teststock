-- Theme-level news sentiment aggregated from news_extraction

CREATE TABLE IF NOT EXISTS theme_news_signals (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    apply_year      SMALLINT        NOT NULL COMMENT 'strategy year Y',
    as_of_date      DATE            NOT NULL COMMENT 'cutoff date (Y-01-01)',
    window_start    DATE            NOT NULL COMMENT 'news window start',
    window_end      DATE            NOT NULL COMMENT 'news window end',
    theme           VARCHAR(50)     NOT NULL COMMENT 'canonical theme label',
    net_score       DECIMAL(8,4)    NOT NULL DEFAULT 0 COMMENT 'bull-bear weighted sum',
    bull_score      DECIMAL(8,4)    NOT NULL DEFAULT 0,
    bear_score      DECIMAL(8,4)    NOT NULL DEFAULT 0,
    article_count   INT             NOT NULL DEFAULT 0,
    avg_magnitude   DECIMAL(4,2)    NULL,
    avg_confidence  DECIMAL(4,2)    NULL,
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_year_theme (apply_year, theme),
    KEY idx_as_of (as_of_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Theme-level news sentiment for CSI ranking';

CREATE TABLE IF NOT EXISTS csi_annual_recommendation (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    apply_year      SMALLINT        NOT NULL,
    as_of_date      DATE            NOT NULL,
    rank_position   INT             NOT NULL,
    ts_code         VARCHAR(20)     NOT NULL,
    index_name      VARCHAR(100)    NOT NULL,
    final_score     DECIMAL(8,6)    NOT NULL,
    policy_score    DECIMAL(8,4)    NULL,
    news_score      DECIMAL(8,4)    NULL,
    momentum        DECIMAL(10,6)   NULL,
    pb_percentile   DECIMAL(6,4)    NULL,
    best_theme      VARCHAR(50)     NULL,
    all_themes      JSON            NULL,
    model_version   VARCHAR(50)     NULL DEFAULT 'v1.0',
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_year_code (apply_year, ts_code),
    KEY idx_year_rank (apply_year, rank_position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Annual CSI index recommendation output';
