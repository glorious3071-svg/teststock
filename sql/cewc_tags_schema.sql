-- CEWC LLM 提取的通用标签（EAV 设计，支持任意扩展）
-- 不删旧版本：同一年同一标签 (model_version, prompt_version, extracted_at) 三元组允许多行
-- 查询最新版本：用 ROW_NUMBER() OVER (PARTITION BY apply_year, tag_category, tag_name ORDER BY extracted_at DESC)

CREATE TABLE IF NOT EXISTS cewc_tags (
    id              BIGINT         NOT NULL  AUTO_INCREMENT,
    apply_year      SMALLINT       NOT NULL  COMMENT '公报指导的年份',
    tag_category    VARCHAR(50)    NOT NULL  COMMENT 'policy_stance / primary_focus / structural_reform / risk_warning / numeric_target / key_phrase',
    tag_name        VARCHAR(100)   NOT NULL  COMMENT '类别内的具体标签名（英文，便于程序化）',
    tag_value       VARCHAR(500)   NULL      COMMENT '标签值（中文枚举/字符串/数值）',
    confidence      FLOAT          NULL      COMMENT 'LLM 自评 0-1',
    evidence        VARCHAR(1000)  NULL      COMMENT '原文证据片段（人工核对用）',
    model_version   VARCHAR(50)    NULL      COMMENT '如 glm-5.1',
    prompt_version  VARCHAR(20)    NULL      COMMENT '如 v1 / v2',
    extracted_at    TIMESTAMP      NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_year_cat (apply_year, tag_category),
    KEY idx_cat_name (tag_category, tag_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='LLM 从 cewc_full_text 提取的多维度可扩展标签';
