-- Research is isolated from live strategy_plan/watchlist and emits no trading events.
CREATE TABLE IF NOT EXISTS banxia.research_run (
    run_id UUID PRIMARY KEY,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL CHECK (end_date >= start_date),
    created_at TIMESTAMPTZ NOT NULL,
    input_sha256 VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    assets JSONB NOT NULL,
    CHECK (payload->>'status' = 'completed')
);

CREATE TABLE IF NOT EXISTS banxia.research_daily (
    run_id UUID NOT NULL REFERENCES banxia.research_run(run_id),
    variant TEXT NOT NULL CHECK (variant IN ('baseline', 'optimized')),
    reference_date DATE NOT NULL,
    plan_date DATE NOT NULL,
    accuracy_pct NUMERIC(6,2),
    observed_count INTEGER NOT NULL CHECK (observed_count >= 0),
    hit_count INTEGER NOT NULL CHECK (hit_count >= 0 AND hit_count <= observed_count),
    payload JSONB NOT NULL,
    PRIMARY KEY (run_id, variant, reference_date),
    CHECK (accuracy_pct IS NULL OR accuracy_pct BETWEEN 0 AND 100),
    CHECK (plan_date > reference_date)
);

CREATE TABLE IF NOT EXISTS banxia.research_strategy (
    strategy_id TEXT PRIMARY KEY,
    run_id UUID NOT NULL UNIQUE REFERENCES banxia.research_run(run_id),
    revision VARCHAR(64) NOT NULL,
    config JSONB NOT NULL,
    metadata JSONB NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE CHECK (active = FALSE)
);

CREATE INDEX IF NOT EXISTS idx_research_run_created
    ON banxia.research_run (created_at DESC);
