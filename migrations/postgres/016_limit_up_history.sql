BEGIN;

CREATE TABLE IF NOT EXISTS banxia.limit_up_history_sync (
    run_id UUID PRIMARY KEY,
    requested_start DATE NOT NULL,
    requested_end DATE NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'succeeded', 'partial', 'failed')
    ),
    source TEXT NOT NULL DEFAULT 'mootdx',
    universe_count INTEGER,
    history_count INTEGER,
    row_count INTEGER,
    missing_symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CHECK (requested_start <= requested_end),
    CHECK (universe_count IS NULL OR universe_count >= 0),
    CHECK (history_count IS NULL OR history_count >= 0),
    CHECK (row_count IS NULL OR row_count >= 0)
);

CREATE INDEX IF NOT EXISTS limit_up_history_sync_latest
    ON banxia.limit_up_history_sync(started_at DESC);

CREATE TABLE IF NOT EXISTS banxia.limit_up_history (
    trade_date DATE NOT NULL,
    instrument_id TEXT NOT NULL,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    name TEXT NOT NULL,
    exchange TEXT NOT NULL CHECK (exchange IN ('sh', 'sz')),
    board TEXT NOT NULL CHECK (board IN ('main', 'gem', 'star')),
    industry TEXT NOT NULL,
    open NUMERIC(18, 4) NOT NULL CHECK (open > 0),
    high NUMERIC(18, 4) NOT NULL CHECK (high > 0),
    low NUMERIC(18, 4) NOT NULL CHECK (low > 0),
    close NUMERIC(18, 4) NOT NULL CHECK (close > 0),
    previous_close NUMERIC(18, 4) NOT NULL CHECK (previous_close > 0),
    limit_price NUMERIC(18, 4) NOT NULL CHECK (limit_price > 0),
    volume_hands NUMERIC(24, 4),
    amount_cny NUMERIC(24, 2),
    total_shares NUMERIC(24, 4),
    float_shares NUMERIC(24, 4),
    total_market_cap_cny NUMERIC(24, 2),
    float_market_cap_cny NUMERIC(24, 2),
    turnover_pct NUMERIC(12, 6),
    amplitude_pct NUMERIC(12, 6) NOT NULL,
    open_change_pct NUMERIC(12, 6) NOT NULL,
    change_pct NUMERIC(12, 6) NOT NULL,
    return_5d_pct NUMERIC(12, 6),
    consecutive_limit_days INTEGER NOT NULL DEFAULT 1
        CHECK (consecutive_limit_days > 0),
    capital_as_of_date DATE,
    classification_as_of_date DATE NOT NULL,
    source TEXT NOT NULL DEFAULT 'mootdx',
    collected_at TIMESTAMPTZ NOT NULL,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (trade_date, instrument_id)
);

CREATE INDEX IF NOT EXISTS limit_up_history_date
    ON banxia.limit_up_history(trade_date DESC, symbol);

CREATE INDEX IF NOT EXISTS limit_up_history_symbol
    ON banxia.limit_up_history(symbol, trade_date DESC);

CREATE INDEX IF NOT EXISTS limit_up_history_industry
    ON banxia.limit_up_history(industry, trade_date DESC);

CREATE INDEX IF NOT EXISTS limit_up_history_metrics
    ON banxia.limit_up_history(
        turnover_pct,
        amplitude_pct,
        open_change_pct,
        change_pct,
        return_5d_pct
    );

CREATE TABLE IF NOT EXISTS banxia.limit_up_history_coverage (
    trade_date DATE PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES banxia.limit_up_history_sync(run_id),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'partial')),
    universe_count INTEGER NOT NULL,
    history_count INTEGER NOT NULL,
    bar_count INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
