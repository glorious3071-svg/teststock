-- L0b retrieval: theme keyword dictionary + article prefilter columns + FULLTEXT

CREATE TABLE IF NOT EXISTS theme_keywords (
    id              INT UNSIGNED NOT NULL AUTO_INCREMENT,
    theme           VARCHAR(50)  NOT NULL,
    keyword         VARCHAR(100) NOT NULL,
    weight          DECIMAL(4,2) NOT NULL DEFAULT 1.00,
    lang            CHAR(5)      NOT NULL DEFAULT 'zh',
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_theme_kw (theme, keyword),
    KEY idx_theme (theme)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Theme keyword dictionary for prefilter';

CREATE TABLE IF NOT EXISTS theme_news_weekly (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    week_start          DATE            NOT NULL,
    theme               VARCHAR(50)     NOT NULL,
    net_score           DECIMAL(10,4)   NOT NULL DEFAULT 0,
    bull_score          DECIMAL(10,4)   NOT NULL DEFAULT 0,
    bear_score          DECIMAL(10,4)   NOT NULL DEFAULT 0,
    event_count         INT             NOT NULL DEFAULT 0,
    mention_count       INT             NOT NULL DEFAULT 0,
    source_diversity    INT             NOT NULL DEFAULT 0,
    model_version       VARCHAR(50)     NOT NULL DEFAULT 'salience_v1',
    created_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_week_theme (week_start, theme),
    KEY idx_week (week_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Weekly theme signal rollup from daily';

-- article prefilter columns applied via ensure_retrieval_schema()
