-- 国家队入场事件表（汇金、证金、平准基金等）
-- 数据源：央媒公开公告、汇金/证金公司公告（手工整理 / CSV seed）
-- 评分卡用途：backtest/scorecard.py 的 national_team_action 字段
--   spec §六：snapshot 之前 12 月内最近一次事件的 direction（entry/exit）
--   单条事件最终映射：entry → policy opportunity -2；exit → policy risk +2

CREATE TABLE IF NOT EXISTS national_team_actions (
    effective_date  DATE          NOT NULL  COMMENT '生效日（公告日或买入起点）',
    action_type     VARCHAR(30)   NOT NULL  COMMENT 'huijin_increase=汇金增持 / securities_co_buy=证金/券商自营 / etf_buy=ETF 增持 / verbal_support=口头表态 / verbal_+_money=表态+资金',
    direction       VARCHAR(8)    NOT NULL  COMMENT 'entry / exit（极罕见）',
    intensity       VARCHAR(10)   NOT NULL  COMMENT 'strong=危机救市强信号 / normal=年度例行/小幅增持',
    institution     VARCHAR(50)   NULL      COMMENT '中央汇金 / 证金公司 / 平准基金 / 一行两会',
    title           VARCHAR(255)  NULL      COMMENT '官方公告/事件标题',
    source_url      VARCHAR(500)  NULL      COMMENT '来源 URL',
    note            VARCHAR(500)  NULL      COMMENT '备注（背景、规模、影响）',
    created_at      TIMESTAMP     NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL  DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (effective_date, action_type),
    KEY idx_effective (effective_date),
    KEY idx_intensity (intensity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='国家队入场事件（汇金 / 证金 / ETF / 平准基金）';
