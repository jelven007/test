BEGIN;

UPDATE banxia.paper_campaign
SET
    config_snapshot = jsonb_set(
        config_snapshot,
        '{fusion_l7_v1}',
        COALESCE(config_snapshot -> 'fusion_l7_v1', '{}'::jsonb)
            || jsonb_build_object(
                'description',
                '半夏+炒股养家融合管线 L2+L4+L6 筛选（L5 龙虎榜检查关闭并放行）；D2 09:31 买入，遵守 T+1，仅在 D3 执行 TP5% / SL2.5% 分钟级动态退出，未触发则 14:55 发出强制退出指令并以 14:56 分钟价代理。',
                'layer5_lhb_check_enabled', false,
                'layer7_exit_session', 'D3',
                'layer7_t1_compliant', true,
                'layer7_fallback_exit', 'D3 14:55 指令 / 14:56 成交代理'
            ),
        true
    ),
    updated_at = now()
WHERE code = 'fusion-l7-v1';

COMMIT;
