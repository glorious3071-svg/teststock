-- 年度新发公募基金统计（2006-2022 年度兜底，CSV seed）
-- 数据源：Wind 公开年报、中国基金报、清华五道口 PBCSF 研究报告等公开整理
-- 用途：当 cn_fund_new_monthly 缺失某月时，评分卡按年度均分回退取值
-- 标记口径：full = 含全部基金类型；active 仅含股票+混合+被动指数

CREATE TABLE IF NOT EXISTS cn_fund_new_yearly (
    cal_year            SMALLINT       NOT NULL  COMMENT '年份',
    new_fund_count      SMALLINT       NULL      COMMENT '当年新成立基金只数',
    new_fund_billion    DECIMAL(12,2)  NOT NULL  COMMENT '当年新发募集亿元（全口径）',
    active_billion      DECIMAL(12,2)  NULL      COMMENT '股票+混合+被动指数 三项亿元（如有）',
    source              VARCHAR(40)    NOT NULL  COMMENT '数据源说明',
    source_url          VARCHAR(255)   NULL,
    notes               VARCHAR(255)   NULL,
    created_at          TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (cal_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='年度新发公募基金合计（兜底用，2006-2022）';
