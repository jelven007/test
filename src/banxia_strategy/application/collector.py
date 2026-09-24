from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence

from ..contracts.topics import topic_for_event
from ..intraday import (
    MootdxLiveSource,
    normalize_quote,
    now_shanghai,
    plan_for,
)
from .persistence import snapshot_market_events
from .reliable_publish import ReliableEventPublisher


class MarketCollector:
    """Collect market facts and publish them without making strategy decisions."""

    def __init__(
        self,
        *,
        candidates: Sequence[Mapping[str, Any]],
        publisher: ReliableEventPublisher,
        source: Optional[Any] = None,
        lease: Optional[Any] = None,
        clock: Optional[Any] = None,
        quote_interval_seconds: float = 1.0,
        idle_interval_seconds: float = 60.0,
    ):
        if not candidates:
            raise ValueError("collector requires at least one watchlist candidate")
        if quote_interval_seconds <= 0 or idle_interval_seconds <= 0:
            raise ValueError("collector intervals must be positive")
        self.candidates = tuple(dict(item) for item in candidates)
        self.codes = tuple(dict.fromkeys(str(item["code"]) for item in candidates))
        self.publisher = publisher
        self.source = source or MootdxLiveSource()
        self.lease = lease
        self.clock = clock or now_shanghai
        self.quote_interval_seconds = quote_interval_seconds
        self.idle_interval_seconds = idle_interval_seconds
        self.stop_event = threading.Event()
        self._bar_event_ids = set()
        self._lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "running": False,
            "leader": lease is None,
            "collections": 0,
            "published": 0,
            "failures": 0,
            "last_success_at": None,
            "last_error": None,
        }

    @staticmethod
    def _active(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        minute = now.hour * 60 + now.minute
        return 9 * 60 + 15 <= minute < 15 * 60

    def collect_once(self) -> int:
        if self.lease is not None and not self.lease.try_acquire():
            with self._lock:
                self._status["leader"] = False
            return 0
        with self._lock:
            self._status["leader"] = True
        self.publisher.replay()
        batch = self.source.fetch(self.codes)
        collected_at = self.clock()
        stocks = []
        for candidate in self.candidates:
            code = str(candidate["code"])
            market = batch.get(code, {})
            if market.get("error") or not market.get("quote"):
                continue
            plan = plan_for(candidate)
            quote = normalize_quote(
                market["quote"],
                market.get("bars", []),
                collected_at,
                plan,
            )
            stocks.append(
                {
                    "code": code,
                    "name": candidate.get("name"),
                    "industry": candidate.get("industry"),
                    "quote": quote,
                }
            )
        snapshot = {
            "collected_at": collected_at.isoformat(timespec="milliseconds"),
            "stocks": stocks,
        }
        quotes, bars, _ = snapshot_market_events(snapshot)
        events = [*quotes]
        for event in bars:
            if event.event_id not in self._bar_event_ids:
                events.append(event)
                self._bar_event_ids.add(event.event_id)
        errors = []
        published = 0
        for event in events:
            try:
                self.publisher.publish(
                    topic_for_event(event.event_type),
                    str(event.payload["symbol"]),
                    event,
                )
                published += 1
            except Exception as exc:
                errors.append(str(exc))
        with self._lock:
            self._status["collections"] += 1
            self._status["published"] += published
            if errors:
                self._status["failures"] += len(errors)
                self._status["last_error"] = errors[-1]
            else:
                self._status["last_error"] = None
                self._status["last_success_at"] = collected_at.isoformat(
                    timespec="seconds"
                )
        if errors:
            raise RuntimeError("; ".join(errors))
        return published

    def run_forever(self) -> None:
        with self._lock:
            self._status["running"] = True
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    self.collect_once()
                except Exception:
                    pass
                interval = (
                    self.quote_interval_seconds
                    if self._active(self.clock())
                    else self.idle_interval_seconds
                )
                self.stop_event.wait(max(0.05, interval - (time.monotonic() - started)))
        finally:
            with self._lock:
                self._status["running"] = False

    def status(self) -> Dict[str, Any]:
        with self._lock:
            result = dict(self._status)
        result["messaging"] = self.publisher.status()
        return result

    def stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        self.stop()
        for target in (self.source, self.publisher, self.lease):
            if target is None:
                continue
            close = getattr(target, "close", None)
            if close is not None:
                close()
