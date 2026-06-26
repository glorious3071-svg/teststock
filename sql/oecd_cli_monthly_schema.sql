-- OECD CLI（Composite Leading Indicator，综合先行指标）月频
-- 数据源：OECD SDMX REST v2 公开 API
--   https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI,4.1/{REF_AREA}.M.LI...AA...H?...
--   dataflow: OECD.SDD.STES / DSD_STES@DF_CLI / 4.1（active vintage；4.0 已冻结停在 2024-01）
--   filter:   FREQ=M  MEASURE=LI  ADJUSTMENT=AA  METHODOLOGY=H（标准化幅度调整）
-- 数据特征：
--   1955-01 起，月频；每经济体 770-857 行（含 2026-05 最新数据）
--   OBS_VALUE 长期围绕 100 振荡；阈值 100 = 长期趋势线；> 100 扩张倾向、< 100 收缩倾向
-- 评分卡用途：
--   V5.0 scorecard 外部维度 global_recession 取数表
--   评分卡 spec §六行 178: USA / G4E / CHN / JPN / G7 五经济体当月 recession_signal=1 投票≥2票
--   recession_signal 由 adapter 动态计算（不固化在表里，便于迭代规则）
--   候选规则: cli < 100 AND cli(M) < cli(M-1) < cli(M-2)（持续下行进入收缩区）
--
-- 字段说明：
--   ref_area    REF_AREA (USA/CHN/JPN/G4E/G7 等)
--   period      数据期，DATE 类型，月初（如 2008-12-01 代表 2008-12 月度值）
--   cli_value   OBS_VALUE，DECIMAL(8,4)（值域 ~92-104，4 位小数足够）
--   methodology METHODOLOGY 字段值（H = headline，标准化幅度调整），便于未来扩展其他口径

CREATE TABLE IF NOT EXISTS oecd_cli_monthly (
    ref_area     VARCHAR(8)     NOT NULL  COMMENT 'OECD REF_AREA 代码（USA/CHN/JPN/G4E/G7 等）',
    period       DATE           NOT NULL  COMMENT '数据期（月初日期，如 2008-12-01 代表 2008-12 月度值）',
    cli_value    DECIMAL(8,4)   NOT NULL  COMMENT '综合先行指标值（长期趋势线=100）',
    methodology  VARCHAR(8)     NOT NULL DEFAULT 'H' COMMENT 'OECD METHODOLOGY: H=headline 标准化幅度调整',
    source       VARCHAR(32)    NOT NULL DEFAULT 'oecd_sdmx_v2',
    created_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ref_area, period),
    KEY idx_period (period),
    KEY idx_area (ref_area)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='OECD Composite Leading Indicator 月频（SDMX v2，1955 起，评分卡 global_recession 取数源）';
