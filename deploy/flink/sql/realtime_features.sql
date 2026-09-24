SET 'execution.runtime-mode' = 'streaming';
SET 'execution.checkpointing.interval' = '10 s';
SET 'execution.checkpointing.mode' = 'EXACTLY_ONCE';
SET 'execution.checkpointing.min-pause' = '5 s';
SET 'execution.checkpointing.timeout' = '60 s';
SET 'execution.attached' = 'false';
SET 'table.exec.source.idle-timeout' = '5 s';
SET 'pipeline.name' = 'banxia-realtime-features-v1';

CREATE TABLE market_quotes (
  event_id STRING,
  event_type STRING,
  schema_version INT,
  occurred_at STRING,
  published_at STRING,
  producer STRING,
  trace_id STRING,
  payload ROW<
    trade_date STRING,
    symbol STRING,
    name STRING,
    industry STRING,
    source_time STRING,
    collected_at STRING,
    price DOUBLE,
    `open` DOUBLE,
    high DOUBLE,
    low DOUBLE,
    previous_close DOUBLE,
    cumulative_volume DOUBLE,
    cumulative_amount_cny DOUBLE,
    bid1 DOUBLE,
    bid1_volume DOUBLE,
    ask1 DOUBLE,
    ask1_volume DOUBLE,
    source_node STRING
  >,
  source_ts TIMESTAMP_LTZ(3) METADATA FROM 'timestamp',
  WATERMARK FOR source_ts AS source_ts - INTERVAL '3' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'market.quote.snapshot.v1',
  'properties.bootstrap.servers' = '__KAFKA_BOOTSTRAP_SERVERS__',
  'properties.group.id' = 'local-compose.flink-feature-v1',
  'scan.startup.mode' = 'earliest-offset',
  'format' = 'json',
  'json.timestamp-format.standard' = 'ISO-8601',
  'json.fail-on-missing-field' = 'false',
  'json.ignore-parse-errors' = 'false'
);

CREATE TABLE realtime_sector_features (
  event_id STRING,
  event_type STRING,
  schema_version INT,
  occurred_at TIMESTAMP_LTZ(3),
  published_at TIMESTAMP_LTZ(3),
  producer STRING,
  trace_id STRING,
  payload ROW<
    trade_date STRING,
    symbol STRING,
    feature_name STRING,
    feature_version STRING,
    window_start TIMESTAMP_LTZ(3),
    window_end TIMESTAMP_LTZ(3),
    computed_at TIMESTAMP_LTZ(3),
    `value` DOUBLE,
    attributes ROW<
      change_pct DOUBLE,
      sector STRING,
      sector_sample_size BIGINT,
      sector_rise_ratio DOUBLE
    >,
    minute_volume_ratio DOUBLE,
    sector_rise_ratio DOUBLE,
    data_state STRING,
    max_input_time TIMESTAMP_LTZ(3)
  >
) WITH (
  'connector' = 'kafka',
  'topic' = 'market.feature.realtime.v1',
  'properties.bootstrap.servers' = '__KAFKA_BOOTSTRAP_SERVERS__',
  'sink.delivery-guarantee' = 'exactly-once',
  'sink.transactional-id-prefix' = 'banxia-flink-sector-v1',
  'properties.transaction.timeout.ms' = '600000',
  'format' = 'json',
  'json.timestamp-format.standard' = 'ISO-8601'
);

CREATE TABLE realtime_volume_features (
  event_id STRING,
  event_type STRING,
  schema_version INT,
  occurred_at TIMESTAMP_LTZ(3),
  published_at TIMESTAMP_LTZ(3),
  producer STRING,
  trace_id STRING,
  payload ROW<
    trade_date STRING,
    symbol STRING,
    feature_name STRING,
    feature_version STRING,
    window_start TIMESTAMP_LTZ(3),
    window_end TIMESTAMP_LTZ(3),
    computed_at TIMESTAMP_LTZ(3),
    `value` DOUBLE,
    attributes ROW<
      change_pct DOUBLE,
      sector STRING,
      sector_sample_size BIGINT,
      sector_rise_ratio DOUBLE
    >,
    minute_volume_ratio DOUBLE,
    sector_rise_ratio DOUBLE,
    data_state STRING,
    max_input_time TIMESTAMP_LTZ(3)
  >
) WITH (
  'connector' = 'kafka',
  'topic' = 'market.feature.realtime.v1',
  'properties.bootstrap.servers' = '__KAFKA_BOOTSTRAP_SERVERS__',
  'sink.delivery-guarantee' = 'exactly-once',
  'sink.transactional-id-prefix' = 'banxia-flink-volume-v1',
  'properties.transaction.timeout.ms' = '600000',
  'format' = 'json',
  'json.timestamp-format.standard' = 'ISO-8601'
);

CREATE TABLE market_bars (
  event_id STRING,
  event_type STRING,
  schema_version INT,
  occurred_at STRING,
  published_at STRING,
  producer STRING,
  trace_id STRING,
  payload ROW<
    trade_date STRING,
    symbol STRING,
    bar_time STRING,
    `open` DOUBLE,
    high DOUBLE,
    low DOUBLE,
    `close` DOUBLE,
    volume DOUBLE,
    amount_cny DOUBLE,
    source_time STRING,
    collected_at STRING,
    revision BIGINT
  >,
  bar_ts TIMESTAMP_LTZ(3) METADATA FROM 'timestamp',
  WATERMARK FOR bar_ts AS bar_ts - INTERVAL '10' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'market.bar.1m.v1',
  'properties.bootstrap.servers' = '__KAFKA_BOOTSTRAP_SERVERS__',
  'properties.group.id' = 'local-compose.flink-volume-v1',
  'scan.startup.mode' = 'earliest-offset',
  'format' = 'json',
  'json.timestamp-format.standard' = 'ISO-8601',
  'json.fail-on-missing-field' = 'false',
  'json.ignore-parse-errors' = 'false'
);

EXECUTE STATEMENT SET
BEGIN

INSERT INTO realtime_sector_features
WITH symbol_windows AS (
  SELECT
    window_start,
    window_end,
    payload.trade_date AS trade_date,
    payload.symbol AS symbol,
    payload.industry AS industry,
    MAX(source_ts) AS max_input_time,
    MAX(payload.price) AS price,
    MAX(payload.previous_close) AS previous_close,
    MAX(trace_id) AS trace_id
  FROM TABLE(
    TUMBLE(TABLE market_quotes, DESCRIPTOR(source_ts), INTERVAL '1' SECOND)
  )
  GROUP BY
    window_start,
    window_end,
    payload.trade_date,
    payload.symbol,
    payload.industry
),
sector_windows AS (
  SELECT
    window_start,
    window_end,
    payload.industry AS industry,
    COUNT(DISTINCT payload.symbol) AS sample_size,
    AVG(
      CASE
        WHEN payload.price > payload.previous_close THEN CAST(1 AS DOUBLE)
        ELSE CAST(0 AS DOUBLE)
      END
    ) AS rise_ratio
  FROM TABLE(
    TUMBLE(TABLE market_quotes, DESCRIPTOR(source_ts), INTERVAL '1' SECOND)
  )
  WHERE payload.industry IS NOT NULL
  GROUP BY window_start, window_end, payload.industry
)
SELECT
  SHA2(
    CONCAT(
      'market.feature.realtime.v1:',
      symbols.symbol,
      ':',
      CAST(symbols.window_end AS STRING)
    ),
    256
  ),
  'market.feature.realtime.v1',
  1,
  symbols.window_end,
  CURRENT_TIMESTAMP,
  'flink-realtime-features',
  symbols.trace_id,
  ROW(
    symbols.trade_date,
    symbols.symbol,
    'realtime_bundle',
    'v1',
    symbols.window_start,
    symbols.window_end,
    CURRENT_TIMESTAMP,
    CAST(NULL AS DOUBLE),
    ROW(
      CASE
        WHEN symbols.previous_close > 0
        THEN ROUND((symbols.price / symbols.previous_close - 1) * 100, 4)
        ELSE CAST(NULL AS DOUBLE)
      END,
      symbols.industry,
      sectors.sample_size,
      sectors.rise_ratio
    ),
    CAST(NULL AS DOUBLE),
    sectors.rise_ratio,
    'fresh',
    symbols.max_input_time
  )
FROM symbol_windows AS symbols
LEFT JOIN sector_windows AS sectors
  ON symbols.window_start = sectors.window_start
 AND symbols.window_end = sectors.window_end
 AND symbols.industry = sectors.industry;

INSERT INTO realtime_volume_features
WITH volume_windows AS (
  SELECT
    event_id,
    trace_id,
    payload.trade_date AS trade_date,
    payload.symbol AS symbol,
    bar_ts AS bar_time,
    payload.volume AS volume,
    CASE
      WHEN COUNT(payload.volume) OVER (
        PARTITION BY payload.symbol
        ORDER BY bar_ts
        ROWS BETWEEN 5 PRECEDING AND CURRENT ROW
      ) > 1
      THEN (
        SUM(CAST(payload.volume AS DOUBLE)) OVER (
          PARTITION BY payload.symbol
          ORDER BY bar_ts
          ROWS BETWEEN 5 PRECEDING AND CURRENT ROW
        ) - CAST(payload.volume AS DOUBLE)
      ) / (
        COUNT(payload.volume) OVER (
          PARTITION BY payload.symbol
          ORDER BY bar_ts
          ROWS BETWEEN 5 PRECEDING AND CURRENT ROW
        ) - 1
      )
      ELSE CAST(NULL AS DOUBLE)
    END AS baseline_volume
  FROM market_bars
)
SELECT
  SHA2(CONCAT('market.feature.volume.v1:', symbol, ':', CAST(bar_time AS STRING)), 256),
  'market.feature.realtime.v1',
  1,
  bar_time,
  CURRENT_TIMESTAMP,
  'flink-realtime-features',
  trace_id,
  ROW(
    trade_date,
    symbol,
    'minute_volume_ratio',
    'v1',
    bar_time - INTERVAL '5' MINUTE,
    bar_time,
    CURRENT_TIMESTAMP,
    CASE
      WHEN baseline_volume > 0
      THEN ROUND(CAST(volume AS DOUBLE) / baseline_volume, 4)
      ELSE CAST(NULL AS DOUBLE)
    END,
    ROW(
      CAST(NULL AS DOUBLE),
      CAST(NULL AS STRING),
      CAST(NULL AS BIGINT),
      CAST(NULL AS DOUBLE)
    ),
    CASE
      WHEN baseline_volume > 0
      THEN ROUND(CAST(volume AS DOUBLE) / baseline_volume, 4)
      ELSE CAST(NULL AS DOUBLE)
    END,
    CAST(NULL AS DOUBLE),
    'fresh',
    bar_time
  )
FROM volume_windows;

END;
