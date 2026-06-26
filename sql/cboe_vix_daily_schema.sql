-- CBOE VIX 隐含波动率指数（"恐慌指数"）日频
-- 数据源：CBOE 官方公开 CSV
--   https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
-- 数据特征：
--   1990-01-02 起，9215+ 行；1990-1991 仅 CLOSE 字段（OHLC 同值），1992 起 OHLC 完整
-- 评分卡用途：
--   V5.0 scorecard 外部维度（候选 v3.4.3，需回测验证后采纳）
--   - vix_monthly_avg = AVG(close) over snapshot 当月
--   - 候选规则：> 30 → opportunity -2（恐慌底 = A 股买点；6M 命中率 85%、12M 80%）
--   - 反实证：VIX 低值（< 15）不作 risk 信号（实证后续上涨概率 >70%）
--
-- 字段说明：
--   close NOT NULL  评分卡取数字段；OHLC 其他三列允许 NULL（兼容 1990-91 早期）

CREATE TABLE IF NOT EXISTS cboe_vix_daily (
    trade_date  DATE           NOT NULL  COMMENT '美东交易日',
    open        DECIMAL(10,4)  NULL      COMMENT '开盘（1992 起）',
    high        DECIMAL(10,4)  NULL      COMMENT '最高（1992 起）',
    low         DECIMAL(10,4)  NULL      COMMENT '最低（1992 起）',
    close       DECIMAL(10,4)  NOT NULL  COMMENT '收盘（评分卡 vix_monthly_avg 取数字段）',
    source      VARCHAR(32)    NOT NULL DEFAULT 'cboe_csv',
    created_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date),
    KEY idx_trade (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='CBOE VIX 隐含波动率指数日频（CBOE 官方 CSV，1990 起）';
