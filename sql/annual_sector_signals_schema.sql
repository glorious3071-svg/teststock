-- 年度（或季度/月度）行业信号表
-- 大模型解析政策文本（CEWC 等）后得出的行业/板块重点关注信息。
-- 每行代表一个板块方向，不下拆到具体 ETF/指数，由后续步骤完成映射。

CREATE TABLE IF NOT EXISTS annual_sector_signals (
    id              BIGINT        NOT NULL AUTO_INCREMENT,
    apply_year      SMALLINT      NOT NULL COMMENT '策略适用年份',
    as_of_date      DATE          NOT NULL COMMENT '分析基准日（年度=年初，季/月类推）',
    theme           VARCHAR(50)   NOT NULL COMMENT '板块/行业方向（科技/消费/金融/新能源/军工/医药/红利等）',
    signal_strength ENUM('强','中','弱') NOT NULL DEFAULT '中' COMMENT '政策信号强度',
    policy_basis    VARCHAR(500)  NULL     COMMENT '政策依据摘要（CEWC 原文关键提法）',
    rationale       TEXT          NULL     COMMENT '模型推理全文',
    model_version   VARCHAR(50)   NULL     COMMENT '生成模型版本',
    prompt_version  VARCHAR(20)   NULL     COMMENT 'Prompt 版本',
    generated_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_date_theme (as_of_date, theme),
    KEY idx_year       (apply_year),
    KEY idx_as_of_date (as_of_date),
    KEY idx_signal     (as_of_date, signal_strength)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='年度/季度/月度行业信号（大模型解析政策文本生成，不含具体 ETF）';
