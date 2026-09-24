from __future__ import annotations

from typing import Dict, Tuple


MARKET_QUOTE_SNAPSHOT = "market.quote.snapshot.v1"
MARKET_BAR_1M = "market.bar.1m.v1"
MARKET_FEATURE_REALTIME = "market.feature.realtime.v1"
STRATEGY_PLAN_CREATED = "strategy.plan.created.v1"
STRATEGY_DECISION = "strategy.decision.v1"
STRATEGY_AUDIT = "strategy.audit.v1"
REPORT_GENERATED = "report.generated.v1"

ALL_TOPICS: Tuple[str, ...] = (
    MARKET_QUOTE_SNAPSHOT,
    MARKET_BAR_1M,
    MARKET_FEATURE_REALTIME,
    STRATEGY_PLAN_CREATED,
    STRATEGY_DECISION,
    STRATEGY_AUDIT,
    REPORT_GENERATED,
)

EVENT_TOPICS: Dict[str, str] = {name: name for name in ALL_TOPICS}


def topic_for_event(event_type: str) -> str:
    try:
        return EVENT_TOPICS[event_type]
    except KeyError as exc:
        raise ValueError(f"unsupported event type: {event_type}") from exc
