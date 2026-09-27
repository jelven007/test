CREATE TABLE IF NOT EXISTS banxia.user_watchlist (
    user_id UUID NOT NULL
        REFERENCES banxia.app_user(user_id) ON DELETE CASCADE,
    instrument_id TEXT NOT NULL
        REFERENCES banxia.security_master(instrument_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, instrument_id)
);

CREATE INDEX IF NOT EXISTS user_watchlist_instrument
    ON banxia.user_watchlist (instrument_id);
