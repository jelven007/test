from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, Mapping, Optional

from ..contracts.topics import MARKET_BAR_1M, MARKET_FEATURE_REALTIME, MARKET_QUOTE_SNAPSHOT
from ..domain.events import EventEnvelope
from ..ports.messaging import EventConsumer
from ..ports.storage import EventPublisher, MarketHistoryStore


class RealtimeFeatureProcessor:
    """Deterministic window logic shared by local replay and stream jobs."""

    def __init__(self, *, feature_version: str = "v1", max_bars: int = 6):
        if max_bars < 2:
            raise ValueError("max_bars must be at least 2")
        self.feature_version = feature_version
        self.max_bars = max_bars
        self._volumes: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=max_bars)
        )
        self._quotes: Dict[str, Mapping[str, object]] = {}

    def process(self, event: EventEnvelope) -> Optional[EventEnvelope]:
        if event.event_type not in {MARKET_QUOTE_SNAPSHOT, MARKET_BAR_1M}:
            return None
        payload = event.payload
        symbol = str(payload["symbol"])
        attributes: Dict[str, object] = {}
        value = None
        if event.event_type == MARKET_BAR_1M:
            volumes = self._volumes[symbol]
            volume = float(payload.get("volume") or 0)
            previous = tuple(volumes)
            volumes.append(volume)
            if previous:
                baseline = sum(previous[-5:]) / len(previous[-5:])
                if baseline > 0:
                    value = round(volume / baseline, 4)
                    attributes["minute_volume_ratio"] = value
        else:
            self._quotes[symbol] = payload
            price = payload.get("price")
            previous_close = payload.get("previous_close")
            if price is not None and previous_close:
                attributes["change_pct"] = round(
                    (float(price) / float(previous_close) - 1) * 100,
                    4,
                )
            industry = payload.get("industry")
            if industry:
                peers = [
                    quote
                    for quote in self._quotes.values()
                    if quote.get("industry") == industry
                    and quote.get("price") is not None
                    and quote.get("previous_close")
                ]
                rising = sum(
                    float(quote["price"]) > float(quote["previous_close"])
                    for quote in peers
                )
                attributes["sector"] = industry
                attributes["sector_sample_size"] = len(peers)
                attributes["sector_rise_ratio"] = (
                    round(rising / len(peers), 4) if peers else None
                )
        source_time = datetime.fromisoformat(
            str(payload.get("source_time") or payload.get("bar_time"))
        )
        computed_at = datetime.now(timezone.utc)
        age = (computed_at - source_time.astimezone(timezone.utc)).total_seconds()
        data_state = "fresh" if age <= 10 else "delayed" if age <= 180 else "stale"
        window_start = source_time
        if event.event_type == MARKET_BAR_1M and self._volumes[symbol]:
            window_start = source_time
        feature_payload = {
            "trade_date": str(payload["trade_date"]),
            "symbol": symbol,
            "feature_name": "realtime_bundle",
            "feature_version": self.feature_version,
            "window_start": window_start,
            "window_end": source_time,
            "computed_at": computed_at,
            "value": value,
            "attributes": attributes,
            "minute_volume_ratio": attributes.get("minute_volume_ratio"),
            "sector_rise_ratio": attributes.get("sector_rise_ratio"),
            "data_state": data_state,
            "max_input_time": source_time,
        }
        return EventEnvelope.create(
            event_type=MARKET_FEATURE_REALTIME,
            producer="realtime-feature-worker",
            occurred_at=source_time,
            identity={
                "feature_version": self.feature_version,
                "symbol": symbol,
                "window_end": source_time,
                "source_event_id": event.event_id,
            },
            payload=feature_payload,
            trace_id=event.trace_id,
        )


class FeatureWorker:
    def __init__(
        self,
        *,
        consumer: EventConsumer,
        publisher: EventPublisher,
        market_store: Optional[MarketHistoryStore] = None,
        processor: Optional[RealtimeFeatureProcessor] = None,
    ):
        self.consumer = consumer
        self.publisher = publisher
        self.market_store = market_store
        self.processor = processor or RealtimeFeatureProcessor()

    def run_once(self, timeout: float = 1.0) -> bool:
        consumed = self.consumer.poll(timeout)
        if consumed is None:
            return False
        feature = self.processor.process(consumed.event)
        if feature is not None:
            if self.market_store is not None:
                self.market_store.append_features([feature])
            self.publisher.publish(
                MARKET_FEATURE_REALTIME,
                str(feature.payload["symbol"]),
                feature,
            )
        self.consumer.commit(consumed)
        return True

    def close(self) -> None:
        for target in (self.consumer, self.publisher, self.market_store):
            if target is None:
                continue
            close = getattr(target, "close", None)
            if close is not None:
                close()
