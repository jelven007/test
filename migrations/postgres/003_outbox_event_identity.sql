ALTER TABLE banxia.outbox_event
    ADD COLUMN IF NOT EXISTS event_id TEXT;

UPDATE banxia.outbox_event
SET event_id = payload->>'event_id'
WHERE event_id IS NULL;

ALTER TABLE banxia.outbox_event
    ALTER COLUMN event_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_event_id
    ON banxia.outbox_event (event_id);
