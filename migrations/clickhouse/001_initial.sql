CREATE DATABASE IF NOT EXISTS banxia;

CREATE TABLE IF NOT EXISTS banxia.market_quote_snapshot
(
    trade_date Date,
    symbol LowCardinality(String),
    source_time DateTime64(3, 'Asia/Shanghai'),
    collected_at DateTime64(3, 'Asia/Shanghai'),
    published_at DateTime64(3, 'Asia/Shanghai'),
    event_id FixedString(64),
    price Nullable(Decimal64(4)),
    open Nullable(Decimal64(4)),
    high Nullable(Decimal64(4)),
    low Nullable(Decimal64(4)),
    previous_close Nullable(Decimal64(4)),
    cumulative_volume Nullable(UInt64),
    cumulative_amount_cny Nullable(Decimal128(2)),
    bid1 Nullable(Decimal64(4)),
    bid1_volume Nullable(UInt64),
    ask1 Nullable(Decimal64(4)),
    ask1_volume Nullable(UInt64),
    source_node LowCardinality(String),
    ingest_version UInt64
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (trade_date, symbol, source_time, event_id)
TTL source_time + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS banxia.market_bar_1m
(
    trade_date Date,
    symbol LowCardinality(String),
    bar_time DateTime64(0, 'Asia/Shanghai'),
    open Decimal64(4),
    high Decimal64(4),
    low Decimal64(4),
    close Decimal64(4),
    volume UInt64,
    amount_cny Decimal128(2),
    source_time DateTime64(3, 'Asia/Shanghai'),
    collected_at DateTime64(3, 'Asia/Shanghai'),
    event_id FixedString(64),
    revision UInt64
)
ENGINE = ReplacingMergeTree(revision)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (trade_date, symbol, bar_time)
TTL bar_time + INTERVAL 5 YEAR DELETE
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS banxia.market_feature_realtime
(
    trade_date Date,
    symbol LowCardinality(String),
    feature_name LowCardinality(String),
    feature_version LowCardinality(String),
    window_start DateTime64(3, 'Asia/Shanghai'),
    window_end DateTime64(3, 'Asia/Shanghai'),
    computed_at DateTime64(3, 'Asia/Shanghai'),
    event_id FixedString(64),
    value Nullable(Float64),
    attributes_json String,
    data_state LowCardinality(String),
    max_input_time DateTime64(3, 'Asia/Shanghai'),
    ingest_version UInt64
)
ENGINE = ReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (
    trade_date,
    symbol,
    feature_name,
    feature_version,
    window_end,
    event_id
)
TTL window_end + INTERVAL 2 YEAR DELETE
SETTINGS index_granularity = 8192;

CREATE VIEW IF NOT EXISTS banxia.market_quote_latest AS
SELECT
    symbol,
    argMax(source_time, collected_at) AS source_time,
    argMax(collected_at, collected_at) AS collected_at,
    argMax(event_id, collected_at) AS event_id,
    argMax(price, collected_at) AS price,
    argMax(open, collected_at) AS open,
    argMax(high, collected_at) AS high,
    argMax(low, collected_at) AS low,
    argMax(previous_close, collected_at) AS previous_close,
    argMax(cumulative_volume, collected_at) AS cumulative_volume,
    argMax(cumulative_amount_cny, collected_at) AS cumulative_amount_cny,
    argMax(bid1, collected_at) AS bid1,
    argMax(bid1_volume, collected_at) AS bid1_volume,
    argMax(ask1, collected_at) AS ask1,
    argMax(ask1_volume, collected_at) AS ask1_volume
FROM banxia.market_quote_snapshot
GROUP BY symbol;
