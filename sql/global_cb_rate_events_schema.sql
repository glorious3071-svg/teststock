-- 全球四大央行政策利率事件表（Fed / ECB / BoE / BoJ）
-- 数据源：akshare macro_bank_{usa,euro,english,japan}_interest_rate
--          → 同花顺-数据中心-宏观数据-央行决议
-- 评分卡用途：
--   ① V5.0 scorecard 外部维度
--      - fed_reversal       = 'hike_to_cut' / 'cut_to_hike'，从 cb_code='FED' 序列拐点推导
--      - fed_zero_qe        = TRUE，当 cb_code='FED' 的最近 rate_after_pct ≤ 0.25%
--      - global_stimulus    = TRUE，过去 12 个月内 ≥3 家央行（含 PBoC）降息（direction='cut'）
--   ② 视图 us_ffr_events 提供 cb_code='FED' 子集（兼容 spec 旧称）
--
-- 字段说明：
--   rate_after_pct NULL ⇨ 决议未公布（akshare 上「今值」未填）— 入库时跳过
--   direction      'hike' / 'cut' / 'hold'   依据 rate_change_pp 推导
--   PBoC 数据存于 cn_deposit_rate / cn_rrr_changes，不入此表，避免重复

CREATE TABLE IF NOT EXISTS global_cb_rate_events (
    cb_code         VARCHAR(8)    NOT NULL  COMMENT 'FED/ECB/BOE/BOJ',
    effective_date  DATE          NOT NULL  COMMENT '决议日',
    rate_before_pct DECIMAL(6,4)  NULL      COMMENT '决议前利率(%)',
    rate_after_pct  DECIMAL(6,4)  NOT NULL  COMMENT '决议后利率(%)',
    rate_change_pp  DECIMAL(6,4)  NOT NULL  COMMENT '变动幅度(pp)，正=加息/负=降息',
    direction       VARCHAR(8)    NOT NULL  COMMENT 'hike / cut / hold',
    source          VARCHAR(32)   NOT NULL DEFAULT 'akshare',
    note            VARCHAR(255)  NULL      COMMENT '备注',
    created_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (cb_code, effective_date),
    KEY idx_cb_date    (cb_code, effective_date),
    KEY idx_direction  (direction),
    KEY idx_effective  (effective_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='全球四大央行政策利率决议事件（Fed/ECB/BoE/BoJ）';

-- 兼容 V5.0 spec 中的 us_ffr_events 旧称，过滤为 Fed 子集
CREATE OR REPLACE VIEW us_ffr_events AS
SELECT
    effective_date,
    rate_before_pct,
    rate_after_pct,
    rate_change_pp,
    direction,
    source,
    note,
    created_at,
    updated_at
FROM global_cb_rate_events
WHERE cb_code = 'FED';
