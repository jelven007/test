BEGIN;

SET LOCAL banxia.allow_initial_strategy_upgrade = 'on';

UPDATE banxia.strategy_definition
SET current_config = current_config || jsonb_build_object(
        'minimum_turnover_pct', 3.0,
        'maximum_turnover_pct', 18.0,
        'maximum_first_seal_time', '10:30'
    ),
    description = '前一日 10:30 前首封且换手率 3%–18%，次日竞价合格并在 10:00 前完成换手回封。'
WHERE code = 'banxia-first-board-second-board';

COMMIT;
