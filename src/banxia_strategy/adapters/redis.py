from __future__ import annotations

import json
from typing import Any, Mapping, Optional


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
                socket_timeout=5,
            )
        self.client = client
        self.key_prefix = key_prefix.strip(":")

    def _monitor_key(self, trade_date: str) -> str:
        return f"{self.key_prefix}:monitor:snapshot:{trade_date}"

    def get_monitor_snapshot(
        self,
        trade_date: str,
    ) -> Optional[Mapping[str, Any]]:
        raw = self.client.get(self._monitor_key(trade_date))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("cached monitor snapshot must be a JSON object")
        return value

    def set_monitor_snapshot(
        self,
        trade_date: str,
        snapshot: Mapping[str, Any],
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.client.set(
            self._monitor_key(trade_date),
            payload,
            ex=ttl_seconds,
        )

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
