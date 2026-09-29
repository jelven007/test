"""Built-in strategy archetypes and their version-controlled defaults."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class StrategyArchetype:
    key: str
    code: str
    name: str
    description: str
    stage: str
    evidence_level: str
    entry_mode: str
    setup_label: str
    config_overrides: Mapping[str, Any]


STRATEGY_ARCHETYPES = (
    StrategyArchetype(
        "first_board_second_board",
        "banxia-first-board-second-board",
        "一进二弱转强",
        "前一日首板，次日竞价合格并在 10:00 前完成换手回封。",
        "P0",
        "A/B",
        "board_reseal",
        "一进二弱转强",
        {},
    ),
    StrategyArchetype(
        "mainline_leader_relay",
        "banxia-mainline-leader-relay",
        "主线龙头接力",
        "选择强题材内连板高度和辨识度居前的龙头候选。",
        "P0",
        "A/B/C",
        "board_reseal",
        "主线龙头接力",
        {
            "minimum_score": 62.0,
            "minimum_industry_limit_up_count": 2,
            "max_per_industry": 2,
            "market_neutral_score": 55.0,
        },
    ),
    StrategyArchetype(
        "low_position_first_board",
        "banxia-low-position-first-board",
        "低位首板挖掘",
        "筛选二十日位置较低、题材形成集群且流动性合格的首次涨停。",
        "P0",
        "B/C",
        "board_reseal",
        "低位首板",
        {
            "minimum_score": 54.0,
            "maximum_amount_cny": 2_000_000_000.0,
            "maximum_float_market_cap_cny": 15_000_000_000.0,
            "max_per_industry": 3,
        },
    ),
    StrategyArchetype(
        "divergence_reseal",
        "banxia-divergence-reseal",
        "分歧转一致回封",
        "选择盘中开板换手后重新封住涨停、且炸板次数受控的强势标的。",
        "P0",
        "A/B/C",
        "board_reseal",
        "分歧转一致回封",
        {
            "minimum_score": 55.0,
            "maximum_break_count": 3,
            "single_break_penalty": 0.0,
            "persistence_weight": 14.0,
        },
    ),
    StrategyArchetype(
        "leader_first_yin",
        "banxia-leader-first-yin",
        "龙头首阴低吸",
        "研究强题材龙头首次断板收阴后的次日承接修复。",
        "P1",
        "B/C",
        "reclaim_open",
        "龙头首阴修复",
        {
            "minimum_score": 32.0,
            "minimum_industry_limit_up_count": 1,
            "minimum_turnover_pct": 0.0,
            "minimum_float_market_cap_cny": 0.0,
            "entry_open_min_pct": -1.5,
            "entry_open_max_pct": 3.0,
        },
    ),
    StrategyArchetype(
        "broken_board_reversal",
        "banxia-broken-board-reversal",
        "断板反包 / N 字",
        "研究涨停断板一至三日后重新转强并突破关键价的候选。",
        "P1",
        "B/C",
        "breakout",
        "断板反包",
        {
            "minimum_score": 34.0,
            "minimum_industry_limit_up_count": 1,
            "minimum_turnover_pct": 0.0,
            "minimum_float_market_cap_cny": 0.0,
            "entry_open_min_pct": -1.0,
            "entry_open_max_pct": 4.0,
        },
    ),
    StrategyArchetype(
        "theme_catchup",
        "banxia-theme-catchup",
        "题材卡位补涨",
        "在主线龙头打开高度后筛选题材内低位首板和顺位提升标的。",
        "P1",
        "B/C",
        "board_reseal",
        "题材卡位补涨",
        {
            "minimum_score": 58.0,
            "minimum_industry_limit_up_count": 2,
            "catchup_board_count": 3,
            "max_per_industry": 2,
        },
    ),
    StrategyArchetype(
        "capacity_trend",
        "banxia-capacity-trend",
        "容量趋势主升",
        "选择成交容量较大、短期均线多头且接近阶段高位的趋势标的。",
        "P1",
        "A/B",
        "breakout",
        "容量趋势突破",
        {
            "minimum_score": 36.0,
            "minimum_industry_limit_up_count": 1,
            "minimum_amount_cny": 800_000_000.0,
            "maximum_amount_cny": 100_000_000_000.0,
            "ideal_amount_min_cny": 1_000_000_000.0,
            "minimum_turnover_pct": 0.0,
            "maximum_turnover_pct": 35.0,
            "minimum_float_market_cap_cny": 0.0,
            "maximum_float_market_cap_cny": 1_000_000_000_000.0,
            "entry_open_min_pct": -0.5,
            "entry_open_max_pct": 3.0,
        },
    ),
    StrategyArchetype(
        "sentiment_repair",
        "banxia-sentiment-repair",
        "情绪冰点超跌修复",
        "在市场低迷阶段筛选近期强势、缩量回撤后出现承接修复的标的。",
        "P2",
        "A/B/C",
        "reclaim_open",
        "冰点修复",
        {
            "minimum_score": 30.0,
            "minimum_industry_limit_up_count": 1,
            "minimum_turnover_pct": 0.0,
            "minimum_float_market_cap_cny": 0.0,
            "entry_open_min_pct": -2.0,
            "entry_open_max_pct": 2.5,
            "market_neutral_score": 58.0,
        },
    ),
    StrategyArchetype(
        "auction_volume_breakout",
        "banxia-auction-volume-breakout",
        "竞价爆量半路确认",
        "从前一日强势股中观察竞价与早盘放量突破，不等待封板完成。",
        "P1",
        "B/C",
        "momentum_breakout",
        "竞价爆量半路",
        {
            "minimum_score": 56.0,
            "entry_open_min_pct": 1.0,
            "entry_open_max_pct": 5.0,
            "reject_open_max_pct": 6.0,
            "entry_cutoff_time": "09:50",
        },
    ),
)

ARCHETYPE_BY_KEY = {item.key: item for item in STRATEGY_ARCHETYPES}
ARCHETYPE_BY_CODE = {item.code: item for item in STRATEGY_ARCHETYPES}
ARCHETYPE_LABELS = {item.key: item.name for item in STRATEGY_ARCHETYPES}
INITIAL_STRATEGY_CODES = frozenset(ARCHETYPE_BY_CODE)
INITIAL_STRATEGY_CODE = STRATEGY_ARCHETYPES[0].code
RUNTIME_CONFIG_KEYS = (
    "report_schedule",
    "quote_interval_seconds",
    "bar_interval_seconds",
    "idle_interval_seconds",
    "report_timezone",
)


def archetype_for(value: str) -> StrategyArchetype:
    try:
        return ARCHETYPE_BY_KEY[value]
    except KeyError as exc:
        raise ValueError(f"未知策略原型：{value}") from exc


def archetype_for_code(code: str) -> StrategyArchetype | None:
    return ARCHETYPE_BY_CODE.get(code)


def is_initial_strategy_code(code: str) -> bool:
    return code in INITIAL_STRATEGY_CODES


def initial_strategy_config(
    profile: StrategyArchetype,
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    values = dict(defaults)
    values.update(profile.config_overrides)
    values["strategy_archetype"] = profile.key
    return values


def profile_metadata(archetype: str) -> dict[str, str]:
    profile = archetype_for(archetype)
    return {
        "key": profile.key,
        "name": profile.name,
        "description": profile.description,
        "stage": profile.stage,
        "evidence_level": profile.evidence_level,
        "entry_mode": profile.entry_mode,
        "setup_label": profile.setup_label,
    }


def accepts_candidate(
    archetype: str,
    row: Mapping[str, Any],
    industry: Mapping[str, Any],
    market: Mapping[str, Any],
) -> bool:
    board_count = int(row.get("board_count") or 0)
    position_20d = float(row.get("position_20d") or 1.0)
    return_20d = float(row.get("return_20d_pct") or 0.0)
    days_since_limit = row.get("days_since_limit_up")
    previous_board_count = int(row.get("previous_board_count") or 0)

    if archetype == "first_board_second_board":
        return board_count == 1
    if archetype == "mainline_leader_relay":
        return (
            board_count >= 2
            and int(industry.get("today_count") or 0) >= 2
            and board_count >= int(industry.get("max_board") or board_count) - 1
        )
    if archetype == "low_position_first_board":
        return board_count == 1 and position_20d <= 0.70 and return_20d <= 30.0
    if archetype == "divergence_reseal":
        return board_count >= 1 and int(row.get("break_count") or 0) >= 1
    if archetype == "leader_first_yin":
        return (
            board_count == 0
            and previous_board_count >= 2
            and bool(row.get("is_bearish"))
            and days_since_limit == 1
        )
    if archetype == "broken_board_reversal":
        return (
            board_count == 0
            and isinstance(days_since_limit, int)
            and 1 <= days_since_limit <= 3
            and bool(row.get("reversal_confirmed"))
        )
    if archetype == "theme_catchup":
        return (
            board_count == 1
            and int(industry.get("max_board") or 0) >= 3
            and int(industry.get("today_count") or 0) >= 2
        )
    if archetype == "capacity_trend":
        return board_count == 0 and bool(row.get("trend_confirmed"))
    if archetype == "sentiment_repair":
        return (
            board_count == 0
            and isinstance(days_since_limit, int)
            and 1 <= days_since_limit <= 5
            and bool(row.get("repair_confirmed"))
            and float(market.get("score") or 0) < 65
        )
    if archetype == "auction_volume_breakout":
        return board_count == 1
    return False


def setup_bonus(archetype: str, row: Mapping[str, Any]) -> float:
    if archetype == "low_position_first_board":
        return max(0.0, 10.0 * (1.0 - float(row.get("position_20d") or 1.0)))
    if archetype == "divergence_reseal":
        return min(8.0, 3.0 + float(row.get("break_count") or 0))
    if archetype == "leader_first_yin":
        return min(15.0, 5.0 + 2.0 * float(row.get("previous_board_count") or 0))
    if archetype == "broken_board_reversal":
        return 12.0
    if archetype == "theme_catchup":
        return 8.0
    if archetype == "capacity_trend":
        return min(15.0, 5.0 + max(0.0, float(row.get("return_20d_pct") or 0.0)) / 4)
    if archetype == "sentiment_repair":
        return 12.0
    if archetype == "auction_volume_breakout":
        return 6.0
    if archetype == "mainline_leader_relay":
        return min(12.0, 3.0 * float(row.get("board_count") or 0))
    return 0.0
