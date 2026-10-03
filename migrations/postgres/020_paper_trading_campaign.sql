BEGIN;

INSERT INTO banxia.strategy_definition (
    strategy_id,
    code,
    name,
    description,
    current_config,
    enabled,
    archived,
    parent_strategy_id,
    config_changes
)
SELECT
    'ff8e167e-c458-511f-a0ba-8161cfaa063f'::uuid,
    'paper-first-board-positive-v1',
    '一进二正收益模拟盘 V1',
    '冻结 2026-10-03 一进二正收益观察参数，仅用于模拟成交和新增样本验证。',
    source.current_config,
    false,
    false,
    source.strategy_id,
    jsonb_build_object(
        'paper_campaign',
        jsonb_build_object(
            'source', source.code,
            'purpose', '30-new-sample-forward-validation'
        )
    )
FROM banxia.strategy_definition AS source
WHERE source.code = 'banxia-first-board-second-board'
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS banxia.paper_campaign (
    campaign_id UUID PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    strategy_id UUID NOT NULL
        REFERENCES banxia.strategy_definition(strategy_id),
    started_on DATE NOT NULL,
    target_sample_count INTEGER NOT NULL CHECK (target_sample_count > 0),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'cancelled')),
    config_snapshot JSONB NOT NULL,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (status = 'completed' AND completed_at IS NOT NULL)
        OR status <> 'completed'
    )
);

INSERT INTO banxia.paper_campaign (
    campaign_id,
    code,
    strategy_id,
    started_on,
    target_sample_count,
    config_snapshot
)
SELECT
    'b170b44a-4d72-532b-a28f-6654b18d7d00'::uuid,
    'first-board-positive-v1',
    strategy_id,
    CURRENT_DATE,
    30,
    current_config
FROM banxia.strategy_definition
WHERE code = 'paper-first-board-positive-v1'
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS banxia.paper_trade (
    paper_trade_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL
        REFERENCES banxia.paper_campaign(campaign_id) ON DELETE CASCADE,
    plan_id UUID NOT NULL
        REFERENCES banxia.strategy_plan(plan_id) ON DELETE CASCADE,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    name TEXT NOT NULL,
    industry TEXT,
    reference_date DATE NOT NULL,
    entry_date DATE NOT NULL,
    exit_date DATE,
    status TEXT NOT NULL CHECK (
        status IN (
            'entry_data_missing',
            'rejected',
            'open',
            'exit_data_missing',
            'closed',
            'exit_unfilled'
        )
    ),
    rejection_reason TEXT,
    entry_time TIME,
    entry_price NUMERIC(18, 4),
    entry_sample_price NUMERIC(18, 4),
    shares INTEGER CHECK (shares IS NULL OR shares > 0),
    target_price NUMERIC(18, 4),
    exit_time TIME,
    exit_price NUMERIC(18, 4),
    exit_reason TEXT,
    net_return_pct NUMERIC(12, 6),
    positive BOOLEAN,
    entry_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    exit_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, plan_id, symbol),
    CHECK (entry_date > reference_date),
    CHECK (exit_date IS NULL OR exit_date > entry_date),
    CHECK (
        status NOT IN ('open', 'exit_data_missing', 'closed', 'exit_unfilled')
        OR (
            entry_time IS NOT NULL
            AND entry_price IS NOT NULL
            AND shares IS NOT NULL
            AND target_price IS NOT NULL
        )
    ),
    CHECK (
        status <> 'closed'
        OR (
            exit_time IS NOT NULL
            AND exit_price IS NOT NULL
            AND net_return_pct IS NOT NULL
            AND positive IS NOT NULL
        )
    ),
    CHECK (status <> 'exit_unfilled' OR positive = false)
);

CREATE INDEX IF NOT EXISTS paper_trade_campaign_status
    ON banxia.paper_trade (campaign_id, status, entry_date);

COMMIT;
