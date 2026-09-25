BEGIN;

-- The protected system strategy follows version-controlled initialization
-- defaults. Other strategies remain immutable and must be copied to change.
CREATE OR REPLACE FUNCTION banxia.prevent_strategy_definition_overwrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.current_config IS DISTINCT FROM OLD.current_config
     AND NOT (
       OLD.code = 'banxia-first-board-second-board'
       AND COALESCE(
         current_setting('banxia.allow_initial_strategy_upgrade', true),
         'off'
       ) = 'on'
     ) THEN
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
