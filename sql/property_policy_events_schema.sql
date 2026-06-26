-- 房地产政策大转向事件表
-- 数据源：财政部、住建部、央行、政治局会议公开公告（手工整理 / CSV seed）
-- 评分卡用途：backtest/scorecard.py 的 property_policy 字段
--   spec §六：snapshot 之前 12 月内最近一次事件的 direction（tighten/loosen）
--   单条事件最终映射：tighten → policy risk +1；loosen → policy opportunity -1

CREATE TABLE IF NOT EXISTS property_policy_events (
    effective_date  DATE          NOT NULL  COMMENT '生效日（一般为公告次日）',
    event_type      VARCHAR(40)   NOT NULL  COMMENT '具体类型：mortgage_rate / down_payment / purchase_limit / red_lines / funding_relief 等',
    direction       VARCHAR(8)    NOT NULL  COMMENT 'tighten（紧地产）/ loosen（松地产）',
    intensity       VARCHAR(10)   NOT NULL  COMMENT 'strong=大转向 / normal=局部调整',
    scope           VARCHAR(40)   NULL      COMMENT 'national / first_tier / hot_cities / specific（如"昆明等热点城市"）',
    title           VARCHAR(255)  NULL      COMMENT '官方公告/事件标题',
    source_url      VARCHAR(500)  NULL      COMMENT '来源 URL',
    note            VARCHAR(500)  NULL      COMMENT '备注（背景、规模、影响）',
    created_at      TIMESTAMP     NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL  DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (effective_date, event_type),
    KEY idx_effective (effective_date),
    KEY idx_intensity (intensity)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='房地产政策大转向事件（财政部 / 住建部 / 央行 / 政治局会议）';
