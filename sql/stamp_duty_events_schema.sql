-- 印花税 / IPO 监管事件表
-- 数据源：财政部、证监会、新华社公开公告（手工整理 / CSV seed）
-- 评分卡用途：backtest/scorecard.py 的 stamp_duty 字段
--   spec §六：snapshot 之前 12 月内最近一次事件的 direction（tighten/loosen）
--   单条事件最终映射：tighten → policy risk +1；loosen → policy opportunity -1

CREATE TABLE IF NOT EXISTS stamp_duty_events (
    effective_date  DATE          NOT NULL  COMMENT '生效日（一般为公告次日或当日 0 点）',
    event_type      VARCHAR(20)   NOT NULL  COMMENT 'stamp_duty=印花税调整 / ipo_pause=IPO 暂停 / ipo_restart=IPO 重启 / ipo_tighten=IPO 严监管 / ipo_loosen=IPO 放松',
    direction       VARCHAR(8)    NOT NULL  COMMENT 'tighten / loosen',
    rate_before     VARCHAR(20)   NULL      COMMENT '调整前税率/状态（如 "3‰双边"、"暂停"、"注册制"）',
    rate_after      VARCHAR(20)   NULL      COMMENT '调整后税率/状态',
    announce_date   DATE          NULL      COMMENT '公告日',
    title           VARCHAR(255)  NULL      COMMENT '官方公告/事件标题',
    source_url      VARCHAR(500)  NULL      COMMENT '来源 URL',
    note            VARCHAR(500)  NULL      COMMENT '备注（背景、影响）',
    created_at      TIMESTAMP     NOT NULL  DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL  DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (effective_date, event_type),
    KEY idx_effective (effective_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='印花税 / IPO 监管事件（财政部、证监会公开公告）';
