-- ETF benchmark indices (Tushare etf_index, doc_id=386)
CREATE TABLE IF NOT EXISTS etf_benchmark_index (
    ts_code        VARCHAR(20)   NOT NULL COMMENT '指数代码',
    indx_name      VARCHAR(120)  NOT NULL COMMENT '指数全称',
    indx_csname    VARCHAR(60)   NULL COMMENT '指数简称',
    pub_party_name VARCHAR(80)   NULL COMMENT '发布机构',
    pub_date       DATE          NULL COMMENT '指数发布日',
    base_date      DATE          NULL COMMENT '指数基日',
    base_point     DECIMAL(16,4) NULL COMMENT '基点',
    adj_circle     VARCHAR(40)   NULL COMMENT '成分调整周期',
    created_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code),
    KEY idx_etf_benchmark_index_name (indx_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Passive index ETFs (Tushare etf_basic, doc_id=385)
CREATE TABLE IF NOT EXISTS passive_etf (
    ts_code        VARCHAR(20)   NOT NULL COMMENT 'ETF代码',
    extname        VARCHAR(120)  NOT NULL COMMENT '扩位简称',
    cname          VARCHAR(200)  NULL COMMENT '基金全称',
    index_ts_code  VARCHAR(20)   NULL COMMENT '追踪指数代码',
    index_name     VARCHAR(120)  NULL COMMENT '追踪指数名称',
    setup_date     DATE          NULL COMMENT '设立日期',
    list_date      DATE          NULL COMMENT '上市日期',
    list_status    CHAR(1)       NULL COMMENT 'L上市 D退市 P待上市',
    exchange       VARCHAR(10)   NULL COMMENT 'SH/SZ',
    mgr_name       VARCHAR(80)   NULL COMMENT '基金管理人',
    custod_name    VARCHAR(120)  NULL COMMENT '托管人',
    mgt_fee        DECIMAL(8,4)  NULL COMMENT '管理费率%',
    etf_type       VARCHAR(20)   NULL COMMENT '纯境内/QDII',
    is_enhanced    TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '是否增强型ETF',
    created_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code),
    KEY idx_passive_etf_index (index_ts_code),
    KEY idx_passive_etf_list_date (list_date),
    KEY idx_passive_etf_status (list_status),
    KEY idx_passive_etf_exchange (exchange),
    CONSTRAINT fk_passive_etf_benchmark
        FOREIGN KEY (index_ts_code) REFERENCES etf_benchmark_index (ts_code)
        ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
