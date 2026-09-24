from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol

from ..domain.events import EventEnvelope


@dataclass(frozen=True)
class DecisionRecord:
    plan_id: str
    symbol: str
    state: str
    reason_code: str
    reason: str
    source_event_id: str
    strategy_version: str
    irreversible: bool
    occurred_at: datetime
    version: int


@dataclass(frozen=True)
class ReportAsset:
    report_id: str
    format: str
    object_key: str
    content_hash: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class ReportIdentity:
    run_id: str
    strategy_version_id: str
    plan_id: Optional[str]


class EventPublisher(Protocol):
    def publish(self, topic: str, key: str, event: EventEnvelope) -> None:
        """Publish one versioned event and return only after broker acknowledgement."""


class MarketHistoryStore(Protocol):
    def append_quote_snapshots(self, events: Iterable[EventEnvelope]) -> None:
        """Persist normalized quote snapshots idempotently."""

    def upsert_minute_bars(self, events: Iterable[EventEnvelope]) -> None:
        """Persist the latest revision of each symbol-minute bar."""

    def append_features(self, events: Iterable[EventEnvelope]) -> None:
        """Persist versioned realtime features."""


class DecisionRepository(Protocol):
    def get(self, plan_id: str, symbol: str) -> Optional[DecisionRecord]:
        """Return the current authoritative decision state."""

    def apply(
        self,
        *,
        input_event_id: str,
        decision: DecisionRecord,
        outbox_event: EventEnvelope,
    ) -> bool:
        """Atomically register input, update state, append history and enqueue outbox."""


class DailyReportRepository(Protocol):
    def persist_report(
        self,
        report: Mapping[str, Any],
        *,
        strategy_version: str,
        strategy_config: Mapping[str, Any],
        code_commit: str,
        assets: Iterable[ReportAsset] = (),
    ) -> ReportIdentity:
        """Persist one generated run, optional plan, candidates and object metadata."""


class SnapshotCache(Protocol):
    def get_monitor_snapshot(self, trade_date: str) -> Optional[Mapping[str, Any]]:
        """Return a rebuildable realtime projection."""

    def set_monitor_snapshot(
        self,
        trade_date: str,
        snapshot: Mapping[str, Any],
        ttl_seconds: int,
    ) -> None:
        """Store a projection with an explicit TTL."""


class ObjectAssetStore(Protocol):
    def put(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> ReportAsset:
        """Write a versioned immutable object and return its content metadata."""
