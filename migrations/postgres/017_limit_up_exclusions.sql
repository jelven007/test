BEGIN;

ALTER TABLE banxia.limit_up_history_sync
    ADD COLUMN IF NOT EXISTS excluded_symbols JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN banxia.limit_up_history_sync.excluded_symbols IS
    'Securities excluded with explicit mootdx evidence, e.g. not yet listed';

COMMIT;
