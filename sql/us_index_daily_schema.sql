-- 美股三大指数日频行情（SPX / IXIC / DJI）
-- 数据源：akshare index_us_stock_sina(symbol=.INX / .IXIC / .DJI)
--          → 新浪财经-美股指数
-- 评分卡用途：
--   ① V5.0 scorecard 外部维度
--      - us_monthly_pct = (本月末 close / 上月末 close − 1) × 100，主用 SPX
--        触发：< −5 → 风险 +1；> +5 → 机会 −1
--   ② 后续可扩展：年度回报、波动率、与沪深 300 相关性等
--
-- 字段说明：
--   ts_code  内部统一命名（SPX.US / IXIC.US / DJI.US），便于与 A 股 index_daily 对齐
--   amount   2024 起新浪未再回填，可能为 0；不影响 monthly_pct 计算
--   volume   单位「股」（原始）

CREATE TABLE IF NOT EXISTS us_index_daily (
    ts_code     VARCHAR(16)    NOT NULL  COMMENT '内部统一命名：SPX.US / IXIC.US / DJI.US',
    trade_date  DATE           NOT NULL  COMMENT '交易日（美东时间）',
    open        DECIMAL(14,4)  NULL      COMMENT '开盘',
    high        DECIMAL(14,4)  NULL      COMMENT '最高',
    low         DECIMAL(14,4)  NULL      COMMENT '最低',
    close       DECIMAL(14,4)  NOT NULL  COMMENT '收盘（评分卡 monthly_pct 取数字段）',
    volume      BIGINT         NULL      COMMENT '成交量（股）',
    amount      DECIMAL(20,2)  NULL      COMMENT '成交额；2024 后新浪未回填，可能为 0',
    source      VARCHAR(32)    NOT NULL DEFAULT 'akshare_sina',
    created_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, trade_date),
    KEY idx_ts_date  (ts_code, trade_date),
    KEY idx_trade    (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='美股三大指数日频行情（SPX/IXIC/DJI，akshare 新浪源）';
