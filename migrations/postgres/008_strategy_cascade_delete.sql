BEGIN;

-- A deleted parent must not force deletion of a still-valid descendant.
-- The application only clears this field as part of permanent deletion.
CREATE OR REPLACE FUNCTION banxia.prevent_strategy_definition_overwrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.current_config IS DISTINCT FROM OLD.current_config THEN
    RAISE EXCEPTION 'strategy parameters are immutable; create a new strategy';
  END IF;
  IF NEW.parent_strategy_id IS DISTINCT FROM OLD.parent_strategy_id
     AND NOT (
       OLD.parent_strategy_id IS NOT NULL
       AND NEW.parent_strategy_id IS NULL
     ) THEN
    RAISE EXCEPTION 'strategy lineage is immutable';
  END IF;
  IF NEW.config_changes IS DISTINCT FROM OLD.config_changes THEN
    RAISE EXCEPTION 'strategy lineage is immutable';
  END IF;
  RETURN NEW;
END;
$$;

COMMIT;
