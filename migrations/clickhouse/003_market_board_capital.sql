CREATE TABLE IF NOT EXISTS banxia.market_board_capital_history
(
    trade_date Date,
    board_code LowCardinality(String),
    total_market_cap_cny Decimal128(2),
    float_market_cap_cny Decimal128(2),
    stock_count UInt32,
    total_cap_stock_count UInt32,
    float_cap_stock_count UInt32,
    estimated_stock_count UInt32,
    source LowCardinality(String),
    calculated_at DateTime64(3, 'Asia/Shanghai'),
    revision UInt64
)
ENGINE = ReplacingMergeTree(revision)
PARTITION BY toYear(trade_date)
ORDER BY (board_code, trade_date)
TTL toDateTime(trade_date) + INTERVAL 30 YEAR DELETE
SETTINGS index_granularity = 8192;
