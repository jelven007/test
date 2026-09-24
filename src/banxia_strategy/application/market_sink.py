from __future__ import annotations

from ..contracts.topics import MARKET_BAR_1M, MARKET_FEATURE_REALTIME, MARKET_QUOTE_SNAPSHOT
from ..ports.messaging import EventConsumer
from ..ports.storage import MarketHistoryStore


class MarketSinkWorker:
    """Persist Kafka market events before committing their offsets."""

    def __init__(self, *, consumer: EventConsumer, store: MarketHistoryStore):
        self.consumer = consumer
        self.store = store

    def run_once(self, timeout: float = 1.0) -> bool:
        consumed = self.consumer.poll(timeout)
        if consumed is None:
            return False
        event = consumed.event
        if event.event_type == MARKET_QUOTE_SNAPSHOT:
            self.store.append_quote_snapshots([event])
        elif event.event_type == MARKET_BAR_1M:
            self.store.upsert_minute_bars([event])
        elif event.event_type == MARKET_FEATURE_REALTIME:
            self.store.append_features([event])
        else:
            raise ValueError(f"unsupported market event: {event.event_type}")
        self.consumer.commit(consumed)
        return True

    def close(self) -> None:
        for target in (self.consumer, self.store):
            close = getattr(target, "close", None)
            if close is not None:
                close()
