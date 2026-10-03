BEGIN;

INSERT INTO banxia.paper_campaign (
    campaign_id,
    code,
    strategy_id,
    started_on,
    target_sample_count,
    config_snapshot
)
SELECT
    '4b1d7c9e-5f32-4e9a-9d2b-7a0c1e34fb02'::uuid,
    'fusion-l7-v1',
    strategy_id,
    CURRENT_DATE,
    30,
    current_config || jsonb_build_object(
        'fusion_l7_v1',
        jsonb_build_object(
            'description', '半夏+炒股养家融合管线 L2+L4+L5+L6 筛选, Layer 7 TP5% / SL2.5% 分钟级动态退出, 未触发则 D3 09:31 开盘退出.',
            'rule_a_r_last30_max_pct', -0.5,
            'rule_a_close_location_max', 0.75,
            'layer5_lhb_block_reasons', jsonb_build_array('振幅', '跌幅'),
            'layer5_lhb_block_institution_sell', true,
            'layer6_gap_min_pct', -1.0,
            'layer6_gap_max_pct', 4.0,
            'layer7_take_profit_pct', 5.0,
            'layer7_stop_loss_pct', 2.5,
            'layer7_fallback_exit', 'D3 09:31 开盘',
            'purpose', '30-new-sample-forward-validation'
        )
    )
FROM banxia.strategy_definition
WHERE code = 'paper-first-board-positive-v1'
ON CONFLICT (code) DO NOTHING;

COMMIT;
