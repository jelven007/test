CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS banxia;

CREATE TABLE IF NOT EXISTS banxia.strategy_definition (
    strategy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS banxia.strategy_version (
    strategy_version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID NOT NULL
        REFERENCES banxia.strategy_definition(strategy_id),
    version TEXT NOT NULL,
    config JSONB NOT NULL,
    code_commit TEXT NOT NULL,
    effective_from DATE,
    created_by TEXT NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, version)
);

CREATE TABLE IF NOT EXISTS banxia.strategy_run (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_date DATE NOT NULL,
    strategy_version_id UUID NOT NULL
        REFERENCES banxia.strategy_version(strategy_version_id),
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    input_start_at TIMESTAMPTZ,
    input_end_at TIMESTAMPTZ,
    error_code TEXT,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (trade_date, strategy_version_id),
    CHECK (finished_at IS NULL OR started_at IS NOT NULL),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE IF NOT EXISTS banxia.strategy_plan (
    plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL UNIQUE
        REFERENCES banxia.strategy_run(run_id),
    reference_date DATE NOT NULL,
    trade_date DATE NOT NULL,
    strategy_version_id UUID NOT NULL
        REFERENCES banxia.strategy_version(strategy_version_id),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'expired', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (trade_date > reference_date)
);

CREATE TABLE IF NOT EXISTS banxia.watchlist (
    watchlist_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL UNIQUE
        REFERENCES banxia.strategy_plan(plan_id),
    source TEXT NOT NULL DEFAULT 'daily_strategy',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS banxia.watchlist_item (
    watchlist_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watchlist_id UUID NOT NULL
        REFERENCES banxia.watchlist(watchlist_id) ON DELETE CASCADE,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    name TEXT NOT NULL,
    industry TEXT,
    origin TEXT NOT NULL
        CHECK (origin IN ('report', 'supplement')),
    display_order INTEGER NOT NULL CHECK (display_order >= 0),
    eligible BOOLEAN NOT NULL,
    eligibility_reason TEXT NOT NULL,
    reference_close NUMERIC(18, 4) NOT NULL CHECK (reference_close > 0),
    entry_rules JSONB NOT NULL,
    invalidation_rules JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (watchlist_id, symbol),
    UNIQUE (watchlist_id, display_order)
);

CREATE TABLE IF NOT EXISTS banxia.candidate (
    candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL
        REFERENCES banxia.strategy_plan(plan_id) ON DELETE CASCADE,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    name TEXT NOT NULL,
    industry TEXT,
    rank INTEGER NOT NULL CHECK (rank > 0),
    score NUMERIC(6, 2) NOT NULL,
    strategy TEXT NOT NULL,
    latest_price NUMERIC(18, 4) NOT NULL CHECK (latest_price > 0),
    amount_cny NUMERIC(22, 2) NOT NULL CHECK (amount_cny >= 0),
    turnover_pct NUMERIC(10, 4),
    float_market_cap_cny NUMERIC(22, 2),
    reasons JSONB NOT NULL,
    entry_trigger TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    exit_plan TEXT NOT NULL,
    position_limit_pct INTEGER NOT NULL CHECK (
        position_limit_pct >= 0 AND position_limit_pct <= 100
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (plan_id, symbol),
    UNIQUE (plan_id, rank)
);

CREATE TABLE IF NOT EXISTS banxia.decision_state (
    decision_state_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL
        REFERENCES banxia.strategy_plan(plan_id) ON DELETE CASCADE,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    state TEXT NOT NULL CHECK (
        state IN (
            'expired',
            'ineligible',
            'pre',
            'stale',
            'no_quote',
            'reference_changed',
            'missing_rules',
            'auction',
            'no_open',
            'reject_open',
            'reject_low',
            'outside_open',
            'window_closed',
            'sealed',
            'at_limit',
            'near_limit',
            'watch',
            'unavailable'
        )
    ),
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    strategy_version_id UUID NOT NULL
        REFERENCES banxia.strategy_version(strategy_version_id),
    first_entered_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    irreversible BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (plan_id, symbol)
);

CREATE TABLE IF NOT EXISTS banxia.decision_event (
    decision_event_id TEXT PRIMARY KEY,
    plan_id UUID NOT NULL
        REFERENCES banxia.strategy_plan(plan_id) ON DELETE CASCADE,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    previous_state TEXT,
    state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    strategy_version_id UUID NOT NULL
        REFERENCES banxia.strategy_version(strategy_version_id),
    rule_inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS banxia.inbox_event (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    partition_id INTEGER NOT NULL,
    offset_value BIGINT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (topic, partition_id, offset_value)
);

CREATE TABLE IF NOT EXISTS banxia.outbox_event (
    outbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    message_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS banxia.report_asset (
    report_asset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL
        REFERENCES banxia.strategy_run(run_id) ON DELETE CASCADE,
    format TEXT NOT NULL CHECK (format IN ('markdown', 'json', 'csv')),
    object_key TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, format, content_hash),
    UNIQUE (object_key)
);

CREATE TABLE IF NOT EXISTS banxia.job_execution (
    job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS banxia.audit_log (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    request_id TEXT,
    before_value JSONB,
    after_value JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_strategy_run_status
    ON banxia.strategy_run (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_candidate_plan_score
    ON banxia.candidate (plan_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_decision_state_updated
    ON banxia.decision_state (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_event_plan_symbol_time
    ON banxia.decision_event (plan_id, symbol, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
    ON banxia.outbox_event (created_at)
    WHERE published_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_job_execution_status
    ON banxia.job_execution (status, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_resource_time
    ON banxia.audit_log (resource_type, resource_id, occurred_at DESC);
