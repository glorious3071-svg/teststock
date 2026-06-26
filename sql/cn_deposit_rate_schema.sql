-- 中国一年期存款基准利率（PBoC 1Y deposit benchmark）调整事件表
-- 数据源：中国人民银行历次基准利率调整公告（手工整理 / CSV seed）
-- 评分卡用途：backtest/scorecard.py 的 deposit_1y_rate
--   = 最新一条 effective_date ≤ snapshot_date 的 rate_after_pct
-- 备注：2015-10-24 之后央行不再公布存款基准利率，最后定格 1.50%

CREATE TABLE IF NOT EXISTS cn_deposit_rate (
    effective_date  DATE          NOT NULL  COMMENT '生效日',
    rate_after_pct  DECIMAL(6,4)  NOT NULL  COMMENT '调整后 1Y 定存利率 (%)',
    rate_change_pp  DECIMAL(6,4)  NULL      COMMENT '本次调整幅度(pp)，正为加息、负为降息',
    direction       VARCHAR(8)    NOT NULL  COMMENT 'hike / cut',
    announce_date   DATE          NULL      COMMENT '公告日',
    note            VARCHAR(255)  NULL      COMMENT '备注',
    created_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (effective_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='中国一年期存款基准利率调整事件（PBoC 1Y deposit benchmark）';
