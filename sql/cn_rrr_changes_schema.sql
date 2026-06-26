-- 中国央行存款准备金率（RRR）调整事件表
-- 数据源：中国人民银行历次公告（手工整理 / CSV seed）
-- 评分卡用途：backtest/scorecard.py 的 rrr_cum_pp_12m
--   = SUM(rrr_change_pp) over (snapshot_date - 1y, snapshot_date]
--   where inst_type IN ('large', 'all')

CREATE TABLE IF NOT EXISTS cn_rrr_changes (
    effective_date  DATE          NOT NULL  COMMENT '生效日',
    inst_type       VARCHAR(10)   NOT NULL  COMMENT 'large=大型金融机构 / small=中小金融机构 / all=一刀切 / targeted=定向',
    rrr_change_pp   DECIMAL(6,3)  NOT NULL  COMMENT '本次调整幅度(pp)，正为加准、负为降准',
    rrr_after_pp    DECIMAL(6,3)  NULL      COMMENT '调整后水平(pp)',
    direction       VARCHAR(8)    NOT NULL  COMMENT 'hike / cut',
    announce_date   DATE          NULL      COMMENT '公告日',
    note            VARCHAR(255)  NULL      COMMENT '备注（来源、特殊说明）',
    created_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (effective_date, inst_type),
    KEY idx_effective (effective_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='中国央行存款准备金率调整事件（PBoC RRR changes）';
