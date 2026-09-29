BEGIN;

-- Built-in archetypes are version-controlled seeds. Their executable defaults
-- may be upgraded only by the guarded startup bootstrap; user-created copies
-- remain immutable.
CREATE OR REPLACE FUNCTION banxia.prevent_strategy_definition_overwrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.current_config IS DISTINCT FROM OLD.current_config
     AND NOT (
       OLD.code LIKE 'banxia-%'
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

ALTER TABLE banxia.decision_state
    DROP CONSTRAINT IF EXISTS decision_state_state_check,
    ADD CONSTRAINT decision_state_state_check CHECK (
        state IN (
            'expired',
            'ineligible',
            'pre',
            'stale',
            'no_quote',
            'reference_changed',
            'missing_rules',
            'auction',
            'no_open',
            'reject_open',
            'reject_low',
            'outside_open',
            'window_closed',
            'sealed',
            'at_limit',
            'near_limit',
            'triggered',
            'watch',
            'unavailable'
        )
    );

COMMIT;
