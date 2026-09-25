from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Union
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
ACTIVE_PHASES = {"auction", "pause", "morning", "afternoon"}
PHASE_LABELS = {
    "pre": "盘前等待",
    "auction": "集合竞价",
    "pause": "竞价结束，等待开盘",
    "morning": "早盘交易",
    "lunch": "午间休市",
    "afternoon": "午盘交易",
    "closed": "已收盘",
    "weekend": "休市",
}


class DecisionState(str, Enum):
    EXPIRED = "expired"
    INELIGIBLE = "ineligible"
    PRE = "pre"
    STALE = "stale"
    NO_QUOTE = "no_quote"
    REFERENCE_CHANGED = "reference_changed"
    MISSING_RULES = "missing_rules"
    AUCTION = "auction"
    NO_OPEN = "no_open"
    REJECT_OPEN = "reject_open"
    REJECT_LOW = "reject_low"
    OUTSIDE_OPEN = "outside_open"
    WINDOW_CLOSED = "window_closed"
    SEALED = "sealed"
    AT_LIMIT = "at_limit"
    NEAR_LIMIT = "near_limit"
    WATCH = "watch"
    UNAVAILABLE = "unavailable"


IRREVERSIBLE_STATES = frozenset(
    {
        DecisionState.EXPIRED.value,
        DecisionState.INELIGIBLE.value,
        DecisionState.REJECT_OPEN.value,
        DecisionState.REJECT_LOW.value,
        DecisionState.OUTSIDE_OPEN.value,
        DecisionState.WINDOW_CLOSED.value,
    }
)


@dataclass(frozen=True)
class Advice:
    state: str
    label: str
    reason: str
    tone: str = "neutral"

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def advice(
    state: Union[DecisionState, str],
    label: str,
    reason: str,
    tone: str = "neutral",
) -> Dict[str, str]:
    value = state.value if isinstance(state, DecisionState) else state
    return Advice(value, label, reason, tone).to_dict()


def phase_at(now: datetime) -> str:
    if now.weekday() >= 5:
        return "weekend"
    minute = now.hour * 60 + now.minute
    for end, phase in [
        (555, "pre"),
        (565, "auction"),
        (570, "pause"),
        (690, "morning"),
        (780, "lunch"),
        (900, "afternoon"),
    ]:
        if minute < end:
            return phase
    return "closed"


def resolve_transition(
    previous: Mapping[str, Any],
    proposed: Mapping[str, Any],
) -> Dict[str, Any]:
    """Keep an irreversible decision when later events propose a weaker state."""
    if previous.get("state") in IRREVERSIBLE_STATES:
        return dict(previous)
    return dict(proposed)


def evaluate(
    quote: Mapping[str, Any],
    plan: Mapping[str, Any],
    now: datetime,
    plan_date: str,
) -> Dict[str, str]:
    """Evaluate one quote without reading infrastructure or mutable process state."""
    phase = phase_at(now)
    if plan_date != now.date().isoformat():
        return advice(
            DecisionState.EXPIRED,
            "计划日期不匹配",
            "当前行情不属于这份次日计划，暂停入场判断。",
            "muted",
        )
    if plan.get("eligible") is False:
        return advice(
            DecisionState.INELIGIBLE,
            "不参与 · 静态门槛未通过",
            str(plan["eligibility_reason"]),
            "muted",
        )
    if phase in ("pre", "weekend"):
        return advice(
            DecisionState.PRE,
            "等待竞价",
            "尚无可执行的竞价结果；当前不挂买单。",
        )
    # 午休及收盘后仍保留已确认的开盘和最低价放弃条件，不被“休市”覆盖。
    if phase not in ("lunch", "closed") and not quote["fresh"]:
        return advice(
            DecisionState.STALE,
            "行情待同步",
            "行情时间过旧、当日分钟线缺失或时间无法核验，暂停判断。",
            "risk",
        )
    if not quote["price"]:
        return advice(
            DecisionState.NO_QUOTE,
            "暂无有效报价",
            "零价格不是跌停或买点；等待有效成交或竞价报价。",
        )
    if (
        quote["previous_close"] is None
        or abs(quote["previous_close"] - plan["previous_close"]) > 0.011
    ):
        return advice(
            DecisionState.REFERENCE_CHANGED,
            "昨收基准变化",
            "盘口昨收与日报不一致，可能涉及除权或数据异常，停止沿用旧价位。",
            "risk",
        )
    if any(
        plan[key] is None
        for key in ("open_min_pct", "open_max_pct", "reject_min_pct", "reject_max_pct")
    ):
        return advice(
            DecisionState.MISSING_RULES,
            "入场参数缺失",
            "日报中缺少可识别的竞价条件，不能自动判断。",
            "risk",
        )
    if phase in ("auction", "pause"):
        return advice(
            DecisionState.AUCTION,
            "等待开盘确认",
            "竞价报价仍可能变化；以最终开盘涨幅检查条件，不在此阶段给出买入确认。",
        )
    opening_pct = quote["open_change_pct"]
    if opening_pct is None:
        return advice(
            DecisionState.NO_OPEN,
            "等待有效开盘价",
            "缺少今日开盘价，无法验证竞价筛选条件。",
        )
    if opening_pct < plan["reject_min_pct"] or opening_pct > plan["reject_max_pct"]:
        return advice(
            DecisionState.REJECT_OPEN,
            "放弃 · 开盘超限",
            f"开盘涨幅{opening_pct:+.2f}%越过原策略放弃阈值。",
            "risk",
        )
    if plan.get("reject_below_previous_close", True) and quote["low"] and quote["low"] < plan["previous_close"] - 0.001:
        return advice(
            DecisionState.REJECT_LOW,
            "放弃 · 跌破昨收",
            "当日最低价已跌破昨收，保守执行放弃条件，不因随后反弹恢复入场。",
            "risk",
        )
    if not plan["open_min_pct"] <= opening_pct <= plan["open_max_pct"]:
        return advice(
            DecisionState.OUTSIDE_OPEN,
            "不参与 · 竞价未通过",
            f"开盘涨幅{opening_pct:+.2f}%不在原策略合格区间内。",
            "muted",
        )
    cutoff = str(plan.get("entry_cutoff_time", "10:00"))
    cutoff_hour, cutoff_minute = map(int, cutoff.split(":"))
    if phase in ("lunch", "closed"):
        return advice(
            DecisionState.WINDOW_CLOSED,
            "不追 · 入场窗口结束",
            f"原策略{cutoff}前入场窗口已结束，不新增接力仓位。",
            "muted",
        )
    if now.hour * 60 + now.minute >= cutoff_hour * 60 + cutoff_minute:
        return advice(
            DecisionState.WINDOW_CLOSED,
            "不追 · 入场窗口结束",
            f"已到{cutoff}或之后，按原计划不新增一进二仓位。",
            "muted",
        )
    limit = plan["limit_price"]
    if quote["price"] >= limit - 0.001:
        sealed = (
            quote["bid"] is not None
            and quote["bid"] >= limit - 0.001
            and quote["ask"] == 0
            and (quote["bid_volume"] or 0) > 0
        )
        if sealed:
            return advice(
                DecisionState.SEALED,
                "封板快照 · 待核验",
                "已触及参考涨停价且当前买一有封单；仍需核验板块助攻、量能和回封过程，不能据此自动买入。",
                "focus",
            )
        return advice(
            DecisionState.AT_LIMIT,
            "触板 · 等待封稳",
            "已触及参考涨停价，盘口尚未确认封住；不提前认定二板成功。",
            "focus",
        )
    near_limit_pct = plan.get("near_limit_pct", 1.0)
    if quote["price"] >= limit * (1 - near_limit_pct / 100):
        return advice(
            DecisionState.NEAR_LIMIT,
            "临近二板 · 等待确认",
            f"距离参考涨停价不足{near_limit_pct:g}%；观察封板与板块配合，不把接近涨停当作买点。",
            "focus",
        )
    return advice(
        DecisionState.WATCH,
        "观察 · 不提前买",
        f"竞价条件通过，但尚未形成二板确认；继续等{cutoff}前的封板或快速回封。",
    )
