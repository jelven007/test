CREATE TABLE IF NOT EXISTS banxia.market_history_bar
(
    symbol LowCardinality(String),
    period LowCardinality(String),
    trade_date Date,
    bar_time DateTime64(0, 'Asia/Shanghai'),
    open Nullable(Decimal64(4)),
    high Nullable(Decimal64(4)),
    low Nullable(Decimal64(4)),
    close Nullable(Decimal64(4)),
    volume Nullable(UInt64),
    amount_cny Nullable(Decimal128(2)),
    raw_json String,
    fetched_at DateTime64(3, 'Asia/Shanghai'),
    revision UInt64
)
ENGINE = ReplacingMergeTree(revision)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (symbol, period, bar_time)
TTL toDateTime(bar_time) + INTERVAL 30 YEAR DELETE
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS banxia.market_history_sync
(
    symbol LowCardinality(String),
    period LowCardinality(String),
    row_count UInt32,
    completed UInt8,
    fetched_at DateTime64(3, 'Asia/Shanghai'),
    revision UInt64
)
ENGINE = ReplacingMergeTree(revision)
ORDER BY (symbol, period)
SETTINGS index_granularity = 8192;
