from __future__ import annotations

import copy
import hashlib
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..adapters import (
    ClickHouseMarketHistoryStore,
    MinioObjectAssetStore,
    PostgresStorage,
    RedisSnapshotCache,
)
from ..domain.events import EventEnvelope, build_event_id
from ..domain.intraday import IRREVERSIBLE_STATES
from ..ports.storage import DecisionRecord, ReportAsset, ReportIdentity
from ..storage_config import StorageSettings


CONTENT_TYPES = {
    "json": "application/json",
    "csv": "text/csv",
    "markdown": "text/markdown",
}


@dataclass(frozen=True)
class PersistenceResult:
    enabled: bool
    assets: Tuple[ReportAsset, ...] = ()
    identity: Optional[ReportIdentity] = None
    errors: Tuple[str, ...] = ()


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return cleaned or "unknown"


def _create_postgres(settings: StorageSettings) -> PostgresStorage:
    return PostgresStorage(settings.postgres_dsn)


def _create_clickhouse(
    settings: StorageSettings,
) -> ClickHouseMarketHistoryStore:
    return ClickHouseMarketHistoryStore(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        database=settings.clickhouse_database,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
    )


def _create_redis(settings: StorageSettings) -> RedisSnapshotCache:
    return RedisSnapshotCache(settings.redis_url)


def _create_minio(settings: StorageSettings) -> MinioObjectAssetStore:
    return MinioObjectAssetStore(
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key,
        bucket=settings.minio_report_bucket,
        secure=settings.minio_secure,
    )


def persist_report_copy(
    report: Mapping[str, Any],
    paths: Mapping[str, Path],
    *,
    strategy_config: Mapping[str, Any],
    settings: StorageSettings,
) -> PersistenceResult:
    """Mirror local reports to MinIO and register their metadata in PostgreSQL."""
    if not settings.enabled:
        return PersistenceResult(enabled=False)

    errors: List[str] = []
    assets: List[ReportAsset] = []
    object_store = None
    repository = None
    try:
        try:
            object_store = _create_minio(settings)
            for format_name, path in paths.items():
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                object_key = (
                    f"strategy_version={_safe_segment(settings.strategy_version)}/"
                    f"trade_date={report['as_of']}/sha256={digest}/"
                    f"{path.name}"
                )
                try:
                    assets.append(
                        object_store.put(
                            object_key=object_key,
                            content=content,
                            content_type=CONTENT_TYPES[format_name],
                            metadata={
                                "report_id": f"{report['as_of']}:{settings.strategy_version}",
                                "format": format_name,
                                "trade_date": str(report["as_of"]),
                                "strategy_version": settings.strategy_version,
                            },
                        )
                    )
                except Exception as exc:
                    errors.append(f"minio:{format_name}: {exc}")
        except Exception as exc:
            errors.append(f"minio:init: {exc}")

        identity = None
        try:
            repository = _create_postgres(settings)
            identity = repository.persist_report(
                report,
                strategy_version=settings.strategy_version,
                strategy_config=strategy_config,
                code_commit=settings.code_commit,
                assets=assets,
            )
        except Exception as exc:
            errors.append(f"postgres:report: {exc}")
    finally:
        if object_store is not None:
            object_store.close()
        if repository is not None:
            repository.close()

    if errors and settings.required:
        raise RuntimeError("; ".join(errors))
    return PersistenceResult(
        enabled=True,
        assets=tuple(assets),
        identity=identity,
        errors=tuple(errors),
    )


def _parse_datetime(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone: {value}")
    return parsed


def _snapshot_time(snapshot: Mapping[str, Any]) -> datetime:
    return _parse_datetime(
        snapshot.get("collected_at") or snapshot.get("server_time")
    )


def _quote_event(
    stock: Mapping[str, Any],
    collected_at: datetime,
) -> Optional[EventEnvelope]:
    quote = stock.get("quote", {})
    if not quote.get("quote_time"):
        return None
    source_time = _parse_datetime(quote["quote_time"])
    payload = {
        "trade_date": source_time.date().isoformat(),
        "symbol": stock["code"],
        "source_time": source_time,
        "collected_at": collected_at,
        "price": quote.get("price"),
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "previous_close": quote.get("previous_close"),
        "cumulative_volume": quote.get("volume"),
        "cumulative_amount_cny": quote.get("amount"),
        "bid1": quote.get("bid"),
        "bid1_volume": quote.get("bid_volume"),
        "ask1": quote.get("ask"),
        "ask1_volume": quote.get("ask_volume"),
        "source_node": "mootdx",
    }
    identity = {
        key: payload[key]
        for key in (
            "trade_date",
            "symbol",
            "source_time",
            "price",
            "cumulative_volume",
            "cumulative_amount_cny",
            "bid1",
            "bid1_volume",
            "ask1",
            "ask1_volume",
        )
    }
    return EventEnvelope.create(
        event_type="market.quote.snapshot.v1",
        producer="compat-intraday-monitor",
        occurred_at=source_time,
        identity=identity,
        payload=payload,
    )


def _bar_event(
    stock: Mapping[str, Any],
    collected_at: datetime,
) -> Optional[EventEnvelope]:
    candles = stock.get("quote", {}).get("candles") or []
    if not candles:
        return None
    candle = candles[-1]
    bar_time = _parse_datetime(candle["time"])
    close = candle.get("close", candle.get("price"))
    if close is None:
        return None
    payload = {
        "trade_date": bar_time.date().isoformat(),
        "symbol": stock["code"],
        "bar_time": bar_time,
        "open": candle.get("open", close),
        "high": candle.get("high", close),
        "low": candle.get("low", close),
        "close": close,
        "volume": candle.get("volume") or 0,
        "amount_cny": candle.get("amount") or 0,
        "source_time": bar_time,
        "collected_at": collected_at,
        "revision": int(collected_at.timestamp() * 1000),
    }
    return EventEnvelope.create(
        event_type="market.bar.1m.v1",
        producer="compat-intraday-monitor",
        occurred_at=bar_time,
        identity={
            "trade_date": payload["trade_date"],
            "symbol": stock["code"],
            "bar_time": bar_time,
        },
        payload=payload,
    )


def snapshot_market_events(
    snapshot: Mapping[str, Any],
) -> Tuple[List[EventEnvelope], List[EventEnvelope], Dict[str, str]]:
    collected_at = _snapshot_time(snapshot)
    quotes: List[EventEnvelope] = []
    bars: List[EventEnvelope] = []
    source_ids: Dict[str, str] = {}
    unavailable = bool(snapshot.get("error") or snapshot.get("delayed"))
    for stock in snapshot.get("stocks", []):
        quote_event = None if unavailable else _quote_event(stock, collected_at)
        if quote_event is not None:
            quotes.append(quote_event)
            source_ids[str(stock["code"])] = quote_event.event_id
        else:
            source_ids[str(stock["code"])] = build_event_id(
                "market.quote.unavailable.v1",
                {
                    "symbol": stock["code"],
                    "collected_at": collected_at,
                    "state": stock.get("advice", {}).get("state"),
                },
            )
        bar_event = None if unavailable else _bar_event(stock, collected_at)
        if bar_event is not None:
            bars.append(bar_event)
    return quotes, bars, source_ids


class AsyncMarketPersistence:
    """Bounded background writer used beside the authoritative local JSONL path."""

    def __init__(
        self,
        *,
        market_store: Any,
        snapshot_cache: Any,
        decision_repository: Any,
        report_identity: ReportIdentity,
        snapshot_ttl_seconds: int = 172800,
        queue_size: int = 1024,
        batch_size: int = 32,
        required: bool = False,
    ):
        self.market_store = market_store
        self.snapshot_cache = snapshot_cache
        self.decision_repository = decision_repository
        self.report_identity = report_identity
        self.snapshot_ttl_seconds = snapshot_ttl_seconds
        self.batch_size = batch_size
        self.required = required
        self.queue: "queue.Queue[Optional[Mapping[str, Any]]]" = queue.Queue(
            maxsize=queue_size
        )
        self.lock = threading.Lock()
        self._bar_event_ids = set()
        self._bar_trade_date: Optional[str] = None
        self._decision_cache: Dict[Tuple[str, str], Optional[DecisionRecord]] = {}
        self._status: Dict[str, Any] = {
            "submitted": 0,
            "processed": 0,
            "failure_count": 0,
            "dropped": 0,
            "last_error": None,
            "last_success_at": None,
            "running": True,
        }
        self.thread = threading.Thread(
            target=self._run,
            name="storage-dual-writer",
            daemon=True,
        )
        self.thread.start()

    def submit(self, snapshot: Mapping[str, Any]) -> bool:
        with self.lock:
            running = bool(self._status["running"])
        if not running:
            self._record_failure("writer is closed", dropped=True)
            if self.required:
                raise RuntimeError("storage writer is closed")
            return False
        payload = copy.deepcopy(snapshot)
        try:
            self.queue.put_nowait(payload)
        except queue.Full as exc:
            self._record_failure("writer queue is full", dropped=True)
            if self.required:
                raise RuntimeError("storage writer queue is full") from exc
            return False
        with self.lock:
            self._status["submitted"] += 1
        return True

    def status(self) -> Mapping[str, Any]:
        with self.lock:
            result = dict(self._status)
        result["queued"] = self.queue.qsize()
        return result

    def _record_failure(self, message: str, *, dropped: bool = False) -> None:
        with self.lock:
            self._status["failure_count"] += 1
            self._status["last_error"] = message
            if dropped:
                self._status["dropped"] += 1

    def _write_decisions(
        self,
        snapshot: Dict[str, Any],
        source_ids: Mapping[str, str],
    ) -> None:
        plan_id = self.report_identity.plan_id
        if plan_id is None:
            return
        occurred_at = _snapshot_time(snapshot)
        for stock in snapshot.get("stocks", []):
            symbol = str(stock["code"])
            proposed = stock.get("advice", {})
            key = (plan_id, symbol)
            if key not in self._decision_cache:
                self._decision_cache[key] = self.decision_repository.get(
                    plan_id,
                    symbol,
                )
            current = self._decision_cache[key]
            if current is not None and current.irreversible:
                stock["advice"]["state"] = current.state
                stock["advice"]["reason"] = current.reason
                stock["advice"]["tone"] = "risk"
                continue
            if (
                current is not None
                and current.state == proposed.get("state")
                and current.reason == proposed.get("reason")
            ):
                continue
            source_event_id = source_ids[symbol]
            version = 1 if current is None else current.version + 1
            state = str(proposed["state"])
            source_event_type = (
                "market.quote.snapshot.v1"
                if stock.get("quote", {}).get("quote_time")
                and not snapshot.get("error")
                and not snapshot.get("delayed")
                else "market.quote.unavailable.v1"
            )
            decision = DecisionRecord(
                plan_id=plan_id,
                symbol=symbol,
                state=state,
                reason_code=state,
                reason=str(proposed["reason"]),
                source_event_id=source_event_id,
                strategy_version=self.report_identity.strategy_version_id,
                irreversible=state in IRREVERSIBLE_STATES,
                occurred_at=occurred_at,
                version=version,
            )
            outbox_event = EventEnvelope.create(
                event_type="strategy.decision.v1",
                producer="compat-intraday-monitor",
                occurred_at=occurred_at,
                identity={
                    "plan_id": plan_id,
                    "symbol": symbol,
                    "source_event_id": source_event_id,
                    "state": state,
                },
                payload={
                    "plan_id": plan_id,
                    "symbol": symbol,
                    "state": state,
                    "reason_code": state,
                    "reason": proposed["reason"],
                    "source_event_id": source_event_id,
                    "source_event_type": source_event_type,
                    "strategy_version_id": self.report_identity.strategy_version_id,
                    "irreversible": decision.irreversible,
                    "version": version,
                    "rule_inputs": {
                        "plan": stock.get("plan", {}),
                        "quote": stock.get("quote", {}),
                    },
                },
            )
            applied = self.decision_repository.apply(
                input_event_id=source_event_id,
                decision=decision,
                outbox_event=outbox_event,
            )
            self._decision_cache[key] = (
                decision
                if applied
                else self.decision_repository.get(plan_id, symbol)
            )

    def _write_batch(self, snapshots: Sequence[Mapping[str, Any]]) -> None:
        quote_events: List[EventEnvelope] = []
        bar_events: List[EventEnvelope] = []
        batch_bar_event_ids = set()
        prepared: List[Tuple[Dict[str, Any], Dict[str, str]]] = []
        for source_snapshot in snapshots:
            snapshot = copy.deepcopy(source_snapshot)
            quotes, bars, source_ids = snapshot_market_events(snapshot)
            quote_events.extend(quotes)
            for event in bars:
                trade_date = str(event.payload["trade_date"])
                if self._bar_trade_date != trade_date:
                    self._bar_trade_date = trade_date
                    self._bar_event_ids.clear()
                if (
                    event.event_id not in self._bar_event_ids
                    and event.event_id not in batch_bar_event_ids
                ):
                    batch_bar_event_ids.add(event.event_id)
                    bar_events.append(event)
            prepared.append((snapshot, source_ids))

        errors = []
        try:
            self.market_store.append_quote_snapshots(quote_events)
        except Exception as exc:
            errors.append(f"clickhouse:quote: {exc}")
        try:
            self.market_store.upsert_minute_bars(bar_events)
            self._bar_event_ids.update(batch_bar_event_ids)
        except Exception as exc:
            errors.append(f"clickhouse:bar: {exc}")
        for snapshot, source_ids in prepared:
            decision_committed = True
            try:
                self._write_decisions(snapshot, source_ids)
            except Exception as exc:
                errors.append(f"postgres: {exc}")
                decision_committed = False
            if decision_committed:
                try:
                    trade_date = str(
                        snapshot.get("plan_date")
                        or _snapshot_time(snapshot).date().isoformat()
                    )
                    self.snapshot_cache.set_monitor_snapshot(
                        trade_date,
                        snapshot,
                        self.snapshot_ttl_seconds,
                    )
                except Exception as exc:
                    errors.append(f"redis: {exc}")
        if errors:
            self._record_failure("; ".join(errors))
            return
        with self.lock:
            self._status["last_error"] = None
            self._status["last_success_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                self.queue.task_done()
                break
            batch = [item]
            stop_after_batch = False
            while len(batch) < self.batch_size:
                try:
                    next_item = self.queue.get_nowait()
                except queue.Empty:
                    break
                if next_item is None:
                    self.queue.task_done()
                    stop_after_batch = True
                    break
                batch.append(next_item)
            try:
                self._write_batch(batch)
            except Exception as exc:
                self._record_failure(f"writer: {exc}")
            finally:
                with self.lock:
                    self._status["processed"] += len(batch)
                for _ in batch:
                    self.queue.task_done()
            if stop_after_batch:
                break
        with self.lock:
            self._status["running"] = False

    def close(self, timeout: float = 10.0) -> None:
        try:
            self.queue.put(None, timeout=timeout)
        except queue.Full:
            self._record_failure("writer queue did not accept shutdown signal")
            return
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            self._record_failure("writer did not stop before timeout")
            return
        for target in (
            self.market_store,
            self.snapshot_cache,
            self.decision_repository,
        ):
            close = getattr(target, "close", None)
            if close is not None:
                close()


def build_market_persistence(
    report: Mapping[str, Any],
    *,
    strategy_config: Mapping[str, Any],
    settings: StorageSettings,
) -> Optional[AsyncMarketPersistence]:
    if not settings.enabled:
        return None
    created: List[Any] = []
    try:
        repository = _create_postgres(settings)
        created.append(repository)
        identity = repository.persist_report(
            report,
            strategy_version=settings.strategy_version,
            strategy_config=strategy_config,
            code_commit=settings.code_commit,
        )
        market_store = _create_clickhouse(settings)
        created.append(market_store)
        snapshot_cache = _create_redis(settings)
        created.append(snapshot_cache)
        return AsyncMarketPersistence(
            market_store=market_store,
            snapshot_cache=snapshot_cache,
            decision_repository=repository,
            report_identity=identity,
            snapshot_ttl_seconds=settings.monitor_snapshot_ttl_seconds,
            queue_size=settings.writer_queue_size,
            batch_size=settings.writer_batch_size,
            required=settings.required,
        )
    except Exception:
        for target in reversed(created):
            close = getattr(target, "close", None)
            if close is not None:
                close()
        raise
