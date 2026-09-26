BEGIN;

CREATE TABLE IF NOT EXISTS banxia.security_daily_snapshot (
    instrument_id TEXT NOT NULL
        REFERENCES banxia.security_master(instrument_id),
    trade_date DATE NOT NULL,
    snapshot_id UUID NOT NULL
        REFERENCES banxia.source_snapshot(snapshot_id),
    source_node TEXT NOT NULL,
    source_time TIMESTAMPTZ,
    collected_at TIMESTAMPTZ NOT NULL,
    open NUMERIC(18, 4),
    high NUMERIC(18, 4),
    low NUMERIC(18, 4),
    close NUMERIC(18, 4),
    previous_close NUMERIC(18, 4),
    change_pct NUMERIC(12, 6),
    volume NUMERIC(24, 4),
    amount NUMERIC(24, 2),
    data_state TEXT NOT NULL CHECK (
        data_state IN ('available', 'missing')
    ),
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (instrument_id, trade_date),
    CHECK (
        data_state = 'missing'
        OR (
            close IS NOT NULL
            AND previous_close IS NOT NULL
            AND close > 0
            AND previous_close > 0
        )
    )
);

CREATE INDEX IF NOT EXISTS security_daily_snapshot_history
    ON banxia.security_daily_snapshot(instrument_id, trade_date DESC);

CREATE INDEX IF NOT EXISTS security_daily_snapshot_date
    ON banxia.security_daily_snapshot(trade_date DESC, data_state);

COMMIT;
