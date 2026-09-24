ALTER TABLE banxia.outbox_event
    ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS locked_by TEXT;

CREATE INDEX IF NOT EXISTS idx_outbox_available
    ON banxia.outbox_event (created_at)
    WHERE published_at IS NULL;
