from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from ..domain.events import EventEnvelope


class RedisSnapshotCache:
    """Redis projection cache; all values are rebuildable JSON documents."""

    def __init__(
        self,
        url: str = "redis://127.0.0.1:6379/0",
        *,
        key_prefix: str = "banxia",
        client: Any = None,
    ):
        if client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError(
                    "Redis adapter requires `pip install -e '.[storage]'`"
                ) from exc
            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=20,
            )
        self.client = client
        self.key_prefix = key_prefix.strip(":")

    def _monitor_key(self, trade_date: str) -> str:
        return f"{self.key_prefix}:monitor:snapshot:{trade_date}"

    def _event_key(self, kind: str, identity: str) -> str:
        return f"{self.key_prefix}:{kind}:latest:{identity}"

    @staticmethod
    def _serialize(value: Mapping[str, Any]) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _deserialize(raw: Any) -> Optional[Mapping[str, Any]]:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("cached value must be a JSON object")
        return value

    def get_monitor_snapshot(
        self,
        trade_date: str,
    ) -> Optional[Mapping[str, Any]]:
        raw = self.client.get(self._monitor_key(trade_date))
        return self._deserialize(raw)

    def set_monitor_snapshot(
        self,
        trade_date: str,
        snapshot: Mapping[str, Any],
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.client.set(
            self._monitor_key(trade_date),
            self._serialize(snapshot),
            ex=ttl_seconds,
        )

    def set_latest_quote(self, event: EventEnvelope, ttl_seconds: int) -> None:
        symbol = str(event.payload["symbol"])
        self.client.set(
            self._event_key("quote", symbol),
            self._serialize(event.to_dict()),
            ex=ttl_seconds,
        )

    def get_latest_quote(self, symbol: str) -> Optional[Mapping[str, Any]]:
        return self._deserialize(self.client.get(self._event_key("quote", symbol)))

    def set_latest_feature(self, event: EventEnvelope, ttl_seconds: int) -> None:
        symbol = str(event.payload["symbol"])
        key = self._event_key("feature", symbol)
        value = event.to_dict()
        current = self._deserialize(self.client.get(key))
        if current is not None:
            current_payload = dict(current.get("payload", {}))
            incoming_payload = dict(value["payload"])
            current_attributes = dict(current_payload.get("attributes", {}))
            current_attributes.update(
                {
                    key: item
                    for key, item in incoming_payload.get(
                        "attributes",
                        {},
                    ).items()
                    if item is not None
                }
            )
            current_payload.update(
                {
                    key: item
                    for key, item in incoming_payload.items()
                    if item is not None and key != "attributes"
                }
            )
            current_payload["attributes"] = current_attributes
            value["payload"] = current_payload
        self.client.set(
            key,
            self._serialize(value),
            ex=ttl_seconds,
        )

    def get_latest_feature(self, symbol: str) -> Optional[Mapping[str, Any]]:
        return self._deserialize(self.client.get(self._event_key("feature", symbol)))

    def append_minute_bar(
        self,
        event: EventEnvelope,
        ttl_seconds: int,
        *,
        max_bars: int = 240,
    ) -> None:
        if ttl_seconds <= 0 or max_bars <= 0:
            raise ValueError("bar TTL and maximum length must be positive")
        symbol = str(event.payload["symbol"])
        key = f"{self.key_prefix}:bars:1m:{symbol}"
        score = datetime.fromisoformat(str(event.payload["bar_time"])).timestamp()
        pipeline = self.client.pipeline(transaction=True)
        pipeline.zadd(key, {self._serialize(event.to_dict()): score})
        pipeline.zremrangebyrank(key, 0, -(max_bars + 1))
        pipeline.expire(key, ttl_seconds)
        pipeline.execute()

    def get_minute_bars(
        self,
        symbol: str,
        *,
        limit: int = 240,
    ) -> Iterable[Mapping[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        key = f"{self.key_prefix}:bars:1m:{symbol}"
        rows = self.client.zrange(key, -limit, -1)
        result = []
        for raw in rows:
            value = self._deserialize(raw)
            if value is not None:
                result.append(value)
        return tuple(result)

    def set_latest_decision(self, event: EventEnvelope, ttl_seconds: int) -> None:
        payload = event.payload
        identity = f"{payload['plan_id']}:{payload['symbol']}"
        self.client.set(
            self._event_key("decision", identity),
            self._serialize(event.to_dict()),
            ex=ttl_seconds,
        )

    def get_latest_decision(
        self,
        plan_id: str,
        symbol: str,
    ) -> Optional[Mapping[str, Any]]:
        return self._deserialize(
            self.client.get(
                self._event_key("decision", f"{plan_id}:{symbol}")
            )
        )

    def append_monitor_event(
        self,
        trade_date: str,
        event: EventEnvelope,
        *,
        ttl_seconds: int,
        max_length: int = 10000,
    ) -> str:
        stream = f"{self.key_prefix}:stream:monitor:{trade_date}"
        event_id = self.client.xadd(
            stream,
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": self._serialize(event.to_dict()),
            },
            maxlen=max_length,
            approximate=True,
        )
        self.client.expire(stream, ttl_seconds)
        return (
            event_id.decode("utf-8")
            if isinstance(event_id, bytes)
            else str(event_id)
        )

    def read_monitor_events(
        self,
        trade_date: str,
        last_id: str,
        *,
        block_ms: int = 15000,
        count: int = 100,
    ) -> Iterable[tuple[str, Mapping[str, Any]]]:
        stream = f"{self.key_prefix}:stream:monitor:{trade_date}"
        batches = self.client.xread(
            {stream: last_id},
            count=count,
            block=block_ms,
        )
        result = []
        for _stream_name, entries in batches:
            for entry_id, fields in entries:
                raw = fields.get("payload") or fields.get(b"payload")
                value = self._deserialize(raw)
                if value is not None:
                    result.append(
                        (
                            entry_id.decode("utf-8")
                            if isinstance(entry_id, bytes)
                            else str(entry_id),
                            value,
                        )
                    )
        return tuple(result)

    def delete_strategy_data(
        self,
        strategy_id: str,
        plan_ids: Iterable[str],
        trade_dates: Iterable[str],
    ) -> Mapping[str, int]:
        plans = {str(plan_id) for plan_id in plan_ids}
        deleted = {"decisions": 0, "snapshots": 0, "stream_events": 0}

        for plan_id in plans:
            pattern = self._event_key("decision", f"{plan_id}:*")
            keys = list(self.client.scan_iter(match=pattern))
            if keys:
                deleted["decisions"] += int(self.client.delete(*keys))

        for trade_date in set(str(value) for value in trade_dates):
            snapshot_key = self._monitor_key(trade_date)
            snapshot = self._deserialize(self.client.get(snapshot_key))
            if snapshot and (
                str(snapshot.get("strategy_id") or "") == str(strategy_id)
                or str(snapshot.get("plan_id") or "") in plans
            ):
                deleted["snapshots"] += int(self.client.delete(snapshot_key))

            stream_key = f"{self.key_prefix}:stream:monitor:{trade_date}"
            entry_ids = []
            for entry_id, fields in self.client.xrange(stream_key):
                raw = fields.get("payload") or fields.get(b"payload")
                event = self._deserialize(raw)
                payload = event.get("payload", {}) if event else {}
                if (
                    str(payload.get("strategy_id") or "") == str(strategy_id)
                    or str(payload.get("plan_id") or "") in plans
                ):
                    entry_ids.append(entry_id)
            if entry_ids:
                deleted["stream_events"] += int(
                    self.client.xdel(stream_key, *entry_ids)
                )
        return deleted

    def ready(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
