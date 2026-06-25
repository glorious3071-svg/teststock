-- v5.0 触发器引擎表结构
-- 实现 v4.0 v3 反应式纪律：A 再平衡 / B 政策 / C 减仓 / D 加仓

-- 触发器规则定义（版本固化，回测时锁定）
CREATE TABLE IF NOT EXISTS trigger_rule (
    rule_id        VARCHAR(8)   NOT NULL COMMENT '规则ID: A/B/C/D',
    version        VARCHAR(16)  NOT NULL DEFAULT 'v4.0_v3' COMMENT '规则版本',
    name           VARCHAR(64)  NOT NULL COMMENT '规则名称',
    direction      VARCHAR(8)   NOT NULL COMMENT 'reduce/add/rebalance',
    condition_expr TEXT         NOT NULL COMMENT '触发条件文字描述',
    action_expr    TEXT         NOT NULL COMMENT '动作描述',
    lock_months    INT          NOT NULL DEFAULT 3 COMMENT '同向锁定月数',
    is_active      TINYINT(1)   NOT NULL DEFAULT 1,
    notes          TEXT         NULL,
    created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (rule_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 触发器执行历史
CREATE TABLE IF NOT EXISTS trigger_history (
    history_id        BIGINT       AUTO_INCREMENT PRIMARY KEY,
    run_id            VARCHAR(64)  NOT NULL COMMENT '回测/实盘运行ID',
    trade_date        DATE         NOT NULL,
    year_month        CHAR(6)      NOT NULL COMMENT 'YYYYMM',
    rule_id           VARCHAR(8)   NOT NULL,
    rule_version      VARCHAR(16)  NOT NULL,
    triggered         TINYINT(1)   NOT NULL COMMENT '0=条件未触发,1=触发并执行',
    condition_met     TINYINT(1)   NOT NULL COMMENT '0=不满足条件,1=满足条件',
    lock_blocked      TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=被锁定期阻止',
    signal_snapshot   JSON         NULL COMMENT '当时所有信号快照',
    action_taken      JSON         NULL COMMENT '执行的具体动作',
    portfolio_before  JSON         NULL,
    portfolio_after   JSON         NULL,
    is_backtest       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_th_run (run_id),
    KEY idx_th_date (trade_date),
    KEY idx_th_ym (year_month),
    KEY idx_th_rule (rule_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 触发器锁定期状态（按 run_id 隔离）
CREATE TABLE IF NOT EXISTS trigger_lock (
    run_id           VARCHAR(64)  NOT NULL,
    rule_id          VARCHAR(8)   NOT NULL,
    direction        VARCHAR(8)   NOT NULL COMMENT 'reduce/add/rebalance',
    locked_until_ym  CHAR(6)      NOT NULL COMMENT '锁至 YYYYMM 末',
    last_trigger_ym  CHAR(6)      NULL,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, rule_id, direction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 月度信号聚合表（v4.0 v3 触发器直接消费）
CREATE TABLE IF NOT EXISTS signal_monthly (
    year_month        CHAR(6)      NOT NULL,
    trade_date        DATE         NOT NULL COMMENT '当月最后交易日',
    -- 估值
    cs300_pe_ttm      DECIMAL(8,2) NULL,
    cs300_pb          DECIMAL(8,2) NULL,
    -- 月度涨跌
    cs300_pct         DECIMAL(8,2) NULL COMMENT '沪深300 当月%',
    cs300_3m_pct      DECIMAL(8,2) NULL COMMENT '沪深300 近3月累计%',
    -- 政策事件标记
    rrr_cut_in_month  TINYINT(1)   NOT NULL DEFAULT 0,
    rate_cut_in_month TINYINT(1)   NOT NULL DEFAULT 0,
    policy_tone_pos   TINYINT(1)   NOT NULL DEFAULT 0,
    -- 元数据
    notes             TEXT         NULL,
    updated_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (year_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 初始化 v4.0 v3 四个触发器规则
INSERT INTO trigger_rule (rule_id, version, name, direction, condition_expr, action_expr, lock_months, notes) VALUES
('A', 'v4.0_v3', '再平衡触发器', 'rebalance',
 '任一持仓ETF实际权重偏离目标 ±5pp',
 '卖超买不足，恢复目标权重',
 0,
 '无锁定期，可月月触发'),
('B', 'v4.0_v3', '政策触发器', 'add',
 'PE_TTM < 12 AND 当月有央行降准或降息事件',
 '动用现金的 30% 加仓至当前低估行业ETF',
 3,
 '锁定期3个月，反向D/C可立即触发'),
('C', 'v4.0_v3', '减仓触发器', 'reduce',
 'PE_TTM > 16 AND (单月涨幅>15% OR 近3月累计>25%)',
 '减仓 10pp 至现金',
 3,
 '锁定期3个月，反向D/B可立即触发'),
('D', 'v4.0_v3', '加仓触发器', 'add',
 'PE_TTM < 11 AND (单月跌幅>10% OR 近3月累计<-20%)',
 '动用现金的 50% 加仓',
 3,
 '锁定期3个月，反向C可立即触发')
ON DUPLICATE KEY UPDATE
    name = VALUES(name),
    condition_expr = VALUES(condition_expr),
    action_expr = VALUES(action_expr),
    lock_months = VALUES(lock_months),
    notes = VALUES(notes);
