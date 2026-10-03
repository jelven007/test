BEGIN;

SET LOCAL banxia.allow_initial_strategy_upgrade = 'on';

UPDATE banxia.strategy_definition
SET current_config = current_config || jsonb_build_object(
        'maximum_turnover_pct', 15.0,
        'ideal_turnover_max_pct', 15.0,
        'minimum_first_minute_change_pct', 6.5,
        'entry_cutoff_time', '09:43',
        'next_day_take_profit_pct', 5.1,
        'next_day_force_exit_time', '14:55'
    ),
    description = '前一日 10:30 前首封且换手率 3%–15%，次日首分钟涨幅至少 6.5%，在 09:43 前等待首次开板成交窗口。'
WHERE code = 'banxia-first-board-second-board';

COMMIT;
