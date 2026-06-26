-- 中央经济工作会议历年数据（手工整理 + 原文抓取）

CREATE TABLE IF NOT EXISTS cewc_annual (
    apply_year       SMALLINT     NOT NULL COMMENT '政策适用年份（次年1月起）',
    meeting_year     SMALLINT     NOT NULL COMMENT '会议召开年份（通常12月）',
    meeting_start    DATE         NULL COMMENT '会议开始日期',
    meeting_end      DATE         NULL COMMENT '会议结束日期',
    theme            VARCHAR(200) NOT NULL COMMENT '会议主题/标题',
    tone             VARCHAR(200) NULL COMMENT '政策基调',
    fiscal_policy    VARCHAR(40)  NULL COMMENT '财政政策取向',
    monetary_policy  VARCHAR(40)  NULL COMMENT '货币政策取向',
    keywords         VARCHAR(300) NULL COMMENT '关键词，逗号分隔',
    summary          TEXT         NULL COMMENT '要点摘要',
    source_url       VARCHAR(500) NULL COMMENT '原文来源 URL',
    primary_task     VARCHAR(300) NULL COMMENT '首要任务/工作重点',
    raw_text         MEDIUMTEXT   NULL COMMENT '公报原文全文',
    content_source   VARCHAR(100) NULL COMMENT '原文来源标识（12371/ifeng/claude_synthesized_v1 等）',
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (apply_year),
    KEY idx_cewc_meeting_year (meeting_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
