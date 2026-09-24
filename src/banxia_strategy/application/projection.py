from __future__ import annotations

from datetime import datetime
from typing import Any

from ..contracts.topics import (
    MARKET_BAR_1M,
    MARKET_FEATURE_REALTIME,
    MARKET_QUOTE_SNAPSHOT,
    STRATEGY_DECISION,
)
from ..ports.messaging import EventConsumer


class ProjectionWorker:
    """Build disposable Redis projections from acknowledged Kafka events."""

    def __init__(
        self,
        *,
        consumer: EventConsumer,
        cache: Any,
        quote_ttl_seconds: int = 172800,
        decision_ttl_seconds: int = 604800,
        stream_ttl_seconds: int = 172800,
    ):
        self.consumer = consumer
        self.cache = cache
        self.quote_ttl_seconds = quote_ttl_seconds
        self.decision_ttl_seconds = decision_ttl_seconds
        self.stream_ttl_seconds = stream_ttl_seconds

    def run_once(self, timeout: float = 1.0) -> bool:
        consumed = self.consumer.poll(timeout)
        if consumed is None:
            return False
        event = consumed.event
        trade_date = str(
            event.payload.get("trade_date")
            or datetime.fromisoformat(event.occurred_at).date().isoformat()
        )
        if event.event_type == MARKET_QUOTE_SNAPSHOT:
            self.cache.set_latest_quote(event, self.quote_ttl_seconds)
        elif event.event_type == MARKET_BAR_1M:
            self.cache.append_minute_bar(event, self.quote_ttl_seconds)
        elif event.event_type == MARKET_FEATURE_REALTIME:
            self.cache.set_latest_feature(event, self.quote_ttl_seconds)
        elif event.event_type == STRATEGY_DECISION:
            self.cache.set_latest_decision(event, self.decision_ttl_seconds)
        else:
            raise ValueError(f"unsupported projection event: {event.event_type}")
        self.cache.append_monitor_event(
            trade_date,
            event,
            ttl_seconds=self.stream_ttl_seconds,
        )
        self.consumer.commit(consumed)
        return True

    def close(self) -> None:
        for target in (self.consumer, self.cache):
            close = getattr(target, "close", None)
            if close is not None:
                close()
