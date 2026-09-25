BEGIN;

-- A strategy's display name may be corrected in place. Its executable
-- parameters and lineage remain immutable and still require a child strategy.
CREATE OR REPLACE FUNCTION banxia.prevent_strategy_definition_overwrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.current_config IS DISTINCT FROM OLD.current_config THEN
    RAISE EXCEPTION 'strategy parameters are immutable; create a new strategy';
  END IF;
  IF NEW.parent_strategy_id IS DISTINCT FROM OLD.parent_strategy_id
     OR NEW.config_changes IS DISTINCT FROM OLD.config_changes THEN
    RAISE EXCEPTION 'strategy lineage is immutable';
  END IF;
  RETURN NEW;
END;
$$;

COMMIT;
