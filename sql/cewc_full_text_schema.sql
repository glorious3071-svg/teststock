-- CEWC（中央经济工作会议）公报全文存储
-- 一年一篇，全文为 LLM 标签提取的输入源

CREATE TABLE IF NOT EXISTS cewc_full_text (
    apply_year     SMALLINT       NOT NULL  COMMENT '公报指导的年份（meeting_year + 1）',
    meeting_date   DATE           NULL      COMMENT '会议结束日（公报发布日）',
    raw_text       MEDIUMTEXT     NOT NULL  COMMENT '公报全文（清洗后纯文本）',
    source_url     VARCHAR(500)   NULL      COMMENT '原始 URL',
    source_name    VARCHAR(100)   NULL      COMMENT 'xinhuanet / gov.cn / 12371 / wikipedia / manual / derived_from_cewc_annual',
    text_bytes     INT            NULL      COMMENT '原文字节数（诊断用）',
    fetched_at     TIMESTAMP      NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP      NOT NULL  DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (apply_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='历年中央经济工作会议公报全文';
