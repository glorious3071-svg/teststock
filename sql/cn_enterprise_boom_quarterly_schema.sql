-- 企业景气指数 + 企业家信心指数（国家统计局企业景气调查，季度）
-- 数据源：akshare macro_china_enterprise_boom_index
-- 评分卡用途：「企业贷款意愿」最佳公开代理，ρ_次4季=-0.174（全样本），早期/近期均在-0.40以上

CREATE TABLE IF NOT EXISTS cn_enterprise_boom_quarterly (
    quarter_str     VARCHAR(20)    NOT NULL  COMMENT '季度字符串（如 2024年第2季度）',
    quarter_date    DATE           NOT NULL  COMMENT '季度末日期（YYYY-MM-DD）',
    cal_year        SMALLINT       NOT NULL,
    cal_quarter     TINYINT        NOT NULL  COMMENT '1~4',
    boom_index      DECIMAL(6,2)   NULL      COMMENT '企业景气指数（100以上=景气）',
    boom_yoy        DECIMAL(6,2)   NULL      COMMENT '企业景气指数-同比变化 pp',
    boom_qoq        DECIMAL(6,2)   NULL      COMMENT '企业景气指数-环比变化 pp',
    confidence_index DECIMAL(6,2)  NULL      COMMENT '企业家信心指数（100以上=乐观）',
    confidence_yoy  DECIMAL(6,2)   NULL      COMMENT '企业家信心指数-同比变化 pp',
    confidence_qoq  DECIMAL(6,2)   NULL      COMMENT '企业家信心指数-环比变化 pp',
    created_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (quarter_date),
    KEY idx_boom_year (cal_year, cal_quarter)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='企业景气指数+企业家信心指数（国家统计局季度调查，2005Q1起）';
