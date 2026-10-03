BEGIN;

CREATE TABLE IF NOT EXISTS banxia.fusion_l7_candidate (
    candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL
        REFERENCES banxia.paper_campaign(campaign_id) ON DELETE CASCADE,
    d1_date DATE NOT NULL,
    d2_date DATE NOT NULL,
    symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
    name TEXT NOT NULL,
    industry TEXT,
    d1_close NUMERIC(18, 4),
    d1_change_pct NUMERIC(12, 6),
    r_last30_pct NUMERIC(12, 6),
    close_location_day NUMERIC(12, 6),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    lhb_check_enabled BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, d1_date, symbol),
    CHECK (d2_date > d1_date)
);

CREATE INDEX IF NOT EXISTS fusion_l7_candidate_lookup
    ON banxia.fusion_l7_candidate (campaign_id, d2_date);

ALTER TABLE banxia.paper_trade
    ALTER COLUMN plan_id DROP NOT NULL;

ALTER TABLE banxia.paper_trade
    ADD COLUMN IF NOT EXISTS fusion_candidate_id UUID
        REFERENCES banxia.fusion_l7_candidate(candidate_id) ON DELETE CASCADE;

ALTER TABLE banxia.paper_trade
    DROP CONSTRAINT IF EXISTS paper_trade_plan_or_fusion;

ALTER TABLE banxia.paper_trade
    ADD CONSTRAINT paper_trade_plan_or_fusion
    CHECK (plan_id IS NOT NULL OR fusion_candidate_id IS NOT NULL);

CREATE UNIQUE INDEX IF NOT EXISTS paper_trade_fusion_unique
    ON banxia.paper_trade (campaign_id, fusion_candidate_id, symbol)
    WHERE fusion_candidate_id IS NOT NULL;

COMMIT;
