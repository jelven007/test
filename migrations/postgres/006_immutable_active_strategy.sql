BEGIN;

ALTER TABLE banxia.strategy_definition
  ADD COLUMN IF NOT EXISTS parent_strategy_id uuid
    REFERENCES banxia.strategy_definition(strategy_id),
  ADD COLUMN IF NOT EXISTS config_changes jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Keep the oldest enabled strategy active when upgrading a catalog that
-- previously allowed several enabled rows.
WITH ranked AS (
  SELECT strategy_id,
         row_number() OVER (ORDER BY created_at, strategy_id) AS position
  FROM banxia.strategy_definition
  WHERE enabled AND NOT archived
)
UPDATE banxia.strategy_definition AS strategy
SET enabled = false
FROM ranked
WHERE strategy.strategy_id = ranked.strategy_id
  AND ranked.position > 1;

CREATE UNIQUE INDEX IF NOT EXISTS strategy_definition_single_active
  ON banxia.strategy_definition ((enabled))
  WHERE enabled AND NOT archived;

ALTER TABLE banxia.strategy_definition
  DROP CONSTRAINT IF EXISTS strategy_definition_archived_inactive,
  ADD CONSTRAINT strategy_definition_archived_inactive
    CHECK (NOT archived OR NOT enabled);

CREATE OR REPLACE FUNCTION banxia.prevent_strategy_definition_overwrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.current_config IS DISTINCT FROM OLD.current_config THEN
    RAISE EXCEPTION 'strategy parameters are immutable; create a new strategy';
  END IF;
  IF NEW.name IS DISTINCT FROM OLD.name THEN
    RAISE EXCEPTION 'strategy name is immutable; create a new strategy';
  END IF;
  IF NEW.parent_strategy_id IS DISTINCT FROM OLD.parent_strategy_id
     OR NEW.config_changes IS DISTINCT FROM OLD.config_changes THEN
    RAISE EXCEPTION 'strategy lineage is immutable';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS strategy_definition_immutable_fields
  ON banxia.strategy_definition;
CREATE TRIGGER strategy_definition_immutable_fields
BEFORE UPDATE ON banxia.strategy_definition
FOR EACH ROW EXECUTE FUNCTION banxia.prevent_strategy_definition_overwrite();

COMMIT;
