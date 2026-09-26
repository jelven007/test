BEGIN;

CREATE TABLE IF NOT EXISTS banxia.source_snapshot (
    snapshot_id UUID PRIMARY KEY,
    dataset TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'validating', 'published', 'failed')
    ),
    source_node TEXT,
    source_version TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    content_sha256 CHAR(64),
    raw_object_key TEXT,
    row_count BIGINT CHECK (row_count IS NULL OR row_count >= 0),
    expected_count BIGINT CHECK (
        expected_count IS NULL OR expected_count >= 0
    ),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    error_summary JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        content_sha256 IS NULL
        OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CHECK (
        status NOT IN ('published', 'failed')
        OR finished_at IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS source_snapshot_content
    ON banxia.source_snapshot (
        dataset, scope_key, as_of_date, content_sha256
    )
    WHERE content_sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS source_snapshot_latest
    ON banxia.source_snapshot (
        dataset, scope_key, as_of_date DESC, finished_at DESC
    )
    WHERE status = 'published';

CREATE TABLE IF NOT EXISTS banxia.source_sync_state (
    dataset TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    published_snapshot_id UUID
        REFERENCES banxia.source_snapshot(snapshot_id),
    watermark JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_complete_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    next_retry_at TIMESTAMPTZ,
    freshness_status TEXT NOT NULL DEFAULT 'unavailable'
        CHECK (
            freshness_status IN ('fresh', 'delayed', 'stale', 'unavailable')
        ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset, scope_key)
);

CREATE TABLE IF NOT EXISTS banxia.security_master (
    instrument_id TEXT PRIMARY KEY,
    market SMALLINT NOT NULL CHECK (market IN (0, 1)),
    exchange TEXT NOT NULL CHECK (exchange IN ('sh', 'sz')),
    instrument_type TEXT NOT NULL,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    board TEXT NOT NULL,
    name TEXT NOT NULL,
    volume_unit INTEGER NOT NULL CHECK (volume_unit > 0),
    decimal_point SMALLINT NOT NULL CHECK (decimal_point >= 0),
    listing_status TEXT NOT NULL DEFAULT 'active'
        CHECK (listing_status IN ('active', 'inactive')),
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    delisted_at TIMESTAMPTZ,
    current_snapshot_id UUID NOT NULL
        REFERENCES banxia.source_snapshot(snapshot_id),
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (market, instrument_type, symbol),
    UNIQUE (exchange, instrument_type, symbol)
);

CREATE INDEX IF NOT EXISTS security_master_directory
    ON banxia.security_master (
        listing_status, instrument_type, board, exchange, symbol
    );

CREATE INDEX IF NOT EXISTS security_master_name_search
    ON banxia.security_master USING gin (
        to_tsvector('simple', name || ' ' || symbol)
    );

CREATE TABLE IF NOT EXISTS banxia.security_master_version (
    instrument_id TEXT NOT NULL
        REFERENCES banxia.security_master(instrument_id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    snapshot_id UUID NOT NULL
        REFERENCES banxia.source_snapshot(snapshot_id),
    name TEXT NOT NULL,
    board TEXT NOT NULL,
    volume_unit INTEGER NOT NULL,
    decimal_point SMALLINT NOT NULL,
    previous_close NUMERIC(18, 4),
    content_sha256 CHAR(64) NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    last_seen_at TIMESTAMPTZ NOT NULL,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (instrument_id, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS security_master_version_current
    ON banxia.security_master_version(instrument_id)
    WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS banxia.market_block (
    block_id UUID PRIMARY KEY,
    block_type TEXT NOT NULL CHECK (
        block_type IN ('default', 'concept', 'style', 'index', 'industry')
    ),
    source_code TEXT NOT NULL,
    block_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    UNIQUE (block_type, source_code, block_name)
);

CREATE INDEX IF NOT EXISTS market_block_name
    ON banxia.market_block(block_name, block_type);

CREATE TABLE IF NOT EXISTS banxia.market_block_membership_version (
    block_id UUID NOT NULL
        REFERENCES banxia.market_block(block_id),
    instrument_id TEXT NOT NULL
        REFERENCES banxia.security_master(instrument_id),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    snapshot_id UUID NOT NULL
        REFERENCES banxia.source_snapshot(snapshot_id),
    source_filename TEXT NOT NULL,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (block_id, instrument_id, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS market_block_membership_current
    ON banxia.market_block_membership_version(block_id, instrument_id)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS market_block_membership_symbol
    ON banxia.market_block_membership_version(instrument_id, valid_to);

COMMIT;
