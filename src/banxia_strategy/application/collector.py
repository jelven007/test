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
        config_store=None,
        candidate_loader=None,
        session_checker=None,
    ):
        if not candidates and candidate_loader is None:
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
        self.config_store = config_store
        self.candidate_loader = candidate_loader
        self.session_checker = session_checker
        self._verified_session_date = None
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
            "paused": False,
            "pause_reason": None,
            "session_date": None,
        }

    @staticmethod
    def _active(now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        minute = now.hour * 60 + now.minute
        return 9 * 60 + 15 <= minute < 11 * 60 + 30 or 13 * 60 <= minute < 15 * 60

    def reload_intervals(self):
        if self.config_store is not None:
            cfg = self.config_store.read()
            self.quote_interval_seconds = cfg.quote_interval_seconds
            self.idle_interval_seconds = cfg.idle_interval_seconds
            self.source.bar_interval = cfg.bar_interval_seconds

    def _collection_allowed(self, now: datetime) -> bool:
        session_date = now.date()
        reason = None
        error = None
        if now.weekday() >= 5:
            reason = "non_trading_day"
        elif self._verified_session_date == session_date:
            pass
        elif self.session_checker is not None:
            try:
                if self.session_checker(session_date):
                    self._verified_session_date = session_date
                else:
                    reason = "non_trading_day"
            except Exception as exc:
                reason = "trading_calendar_unavailable"
                error = str(exc)
        with self._lock:
            self._status["paused"] = reason is not None
            self._status["pause_reason"] = reason
            self._status["session_date"] = session_date.isoformat()
            if error is not None:
                self._status["last_error"] = error
        return reason is None

    def collect_once(self) -> int:
        if not self._collection_allowed(self.clock()):
            return 0
        if self.candidate_loader is not None:
            candidates = self.candidate_loader()
            unique = {str(item["code"]): dict(item) for item in candidates}
            self.candidates = tuple(unique.values())
            self.codes = tuple(unique)
        if not self.codes:
            return 0
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
                    self.reload_intervals()
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
