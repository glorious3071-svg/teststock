-- 黄金日频价格（多 symbol 多 currency 共表）
-- 数据源（akshare）：
--   AU9999.SGE  — 上海黄金交易所 Au99.99 现货      CNY/克   spot_hist_sge('Au99.99')        2016-12-19 起 OHLC
--   GC.FOREIGN  — COMEX 黄金期货连续合约（sina）  USD/盎司 futures_foreign_hist('GC')      2016-06-27 起 OHLC
--
-- 单位/币种说明：
--   不同 symbol 价格基数差 30 倍以上（克 vs 盎司、CNY vs USD），必须先按 symbol 过滤再做对比/统计。
--   评分卡使用建议：
--     1) 月度涨跌幅、月均收益等百分比衍生指标对币种/单位不敏感（推荐）；
--     2) 绝对水平阈值（如金价 > 3000）必须显式声明 symbol & currency。
--
-- 评分卡用途（候选 v6.x，需回测验证后采纳）：
--   gold_monthly_pct = (last_close - first_close) / first_close × 100  over snapshot 当月
--   候选规则：> +8% / 6M 命中率待回测；< -8% 是潜在风险偏好回升信号
--
-- 字段说明：
--   symbol      数据系列代码（与上方 symbol 表保持一致）
--   currency    报价币种（CNY/USD）
--   unit        报价单位（gram/troy_oz）
--   close NOT NULL  评分卡取数字段；OHL 缺失或源不提供时允许 NULL
--   volume      sina 期货源不提供（恒为 0）→ 允许 NULL
--   source      数据来源标识（用于追溯）

CREATE TABLE IF NOT EXISTS gold_daily (
    symbol      VARCHAR(32)    NOT NULL  COMMENT '系列代码（AU9999.SGE / GC.FOREIGN）',
    trade_date  DATE           NOT NULL  COMMENT '交易日（本地）',
    currency    VARCHAR(8)     NOT NULL  COMMENT '报价币种 CNY/USD',
    unit        VARCHAR(16)    NOT NULL  COMMENT '报价单位 gram/troy_oz',
    open        DECIMAL(12,4)  NULL      COMMENT '开盘价',
    high        DECIMAL(12,4)  NULL      COMMENT '最高价',
    low         DECIMAL(12,4)  NULL      COMMENT '最低价',
    close       DECIMAL(12,4)  NOT NULL  COMMENT '收盘价（评分卡取数字段）',
    volume      DECIMAL(20,4)  NULL      COMMENT '成交量（sina 期货源为 NULL）',
    source      VARCHAR(32)    NOT NULL  COMMENT '来源（akshare_sge / akshare_sina_foreign）',
    created_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, trade_date),
    KEY idx_symbol_date (symbol, trade_date),
    KEY idx_trade_date  (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='黄金日频（SGE 现货 + COMEX 期货，2016 起）';
