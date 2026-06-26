-- 月度新发公募基金统计（精确月度）
-- 数据源：akshare fund_new_found_em（东方财富 fund.eastmoney.com）
-- 注意：稳定覆盖 2023-08 起；2006-2023 的兜底数据见 cn_fund_new_yearly
-- 评分卡用途：scorecard.py sentiment 维度
--   new_fund_billion > 1500 → risk +1
--   new_fund_billion < 200  → opportunity -1

CREATE TABLE IF NOT EXISTS cn_fund_new_monthly (
    month               CHAR(6)        NOT NULL  COMMENT '月份 YYYYMM',
    cal_year            SMALLINT       NOT NULL,
    cal_month           TINYINT        NOT NULL,
    new_fund_count      SMALLINT       NOT NULL  COMMENT '当月新成立基金只数',
    new_fund_billion    DECIMAL(10,2)  NOT NULL  COMMENT '当月新发募集亿元（全口径）',
    active_billion      DECIMAL(10,2)  NULL      COMMENT '股票+混合+被动指数 三项亿元',
    bond_billion        DECIMAL(10,2)  NULL      COMMENT '债券型亿元',
    qdii_billion        DECIMAL(10,2)  NULL      COMMENT 'QDII 亿元',
    by_type_json        JSON           NULL      COMMENT '按规范化类型分桶 {equity,mixed,index,bond,qdii,fof,...}',
    source              VARCHAR(20)    NOT NULL  COMMENT '数据源 em / amac / wind / estimate',
    source_url          VARCHAR(255)   NULL,
    notes               VARCHAR(255)   NULL,
    created_at          TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (month),
    KEY idx_fund_new_year (cal_year, cal_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='月度新发公募基金（精确月度，akshare 东财源）';
