from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("event datetimes must include a timezone")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def build_event_id(event_type: str, identity: Mapping[str, Any]) -> str:
    """Build a stable id from fields that define one logical event."""
    if not event_type or not identity:
        raise ValueError("event_type and identity are required")
    canonical = json.dumps(
        {"event_type": event_type, "identity": _canonical(identity)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    schema_version: int
    occurred_at: str
    published_at: str
    producer: str
    trace_id: str
    payload: Dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        producer: str,
        occurred_at: datetime,
        identity: Mapping[str, Any],
        payload: Mapping[str, Any],
        schema_version: int = 1,
        trace_id: Optional[str] = None,
        published_at: Optional[datetime] = None,
    ) -> "EventEnvelope":
        if schema_version < 1:
            raise ValueError("schema_version must be positive")
        if not producer:
            raise ValueError("producer is required")
        if occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        published_at = published_at or datetime.now(timezone.utc)
        if published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        normalized_payload = _canonical(payload)
        json.dumps(normalized_payload, allow_nan=False)
        return cls(
            event_id=build_event_id(event_type, identity),
            event_type=event_type,
            schema_version=schema_version,
            occurred_at=occurred_at.isoformat(),
            published_at=published_at.isoformat(),
            producer=producer,
            trace_id=trace_id or uuid.uuid4().hex,
            payload=dict(normalized_payload),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        required = {
            "event_id",
            "event_type",
            "schema_version",
            "occurred_at",
            "published_at",
            "producer",
            "trace_id",
            "payload",
        }
        missing = sorted(required - set(value))
        if missing:
            raise ValueError(f"event envelope missing fields: {', '.join(missing)}")
        payload = value["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be an object")
        for field in ("occurred_at", "published_at"):
            parsed = datetime.fromisoformat(str(value[field]))
            if parsed.tzinfo is None:
                raise ValueError(f"{field} must include a timezone")
        schema_version = int(value["schema_version"])
        if schema_version < 1:
            raise ValueError("schema_version must be positive")
        event_type = str(value["event_type"])
        event_id = str(value["event_id"])
        if len(event_id) != 64:
            raise ValueError("event_id must be a sha256 hex digest")
        return cls(
            event_id=event_id,
            event_type=event_type,
            schema_version=schema_version,
            occurred_at=str(value["occurred_at"]),
            published_at=str(value["published_at"]),
            producer=str(value["producer"]),
            trace_id=str(value["trace_id"]),
            payload=dict(payload),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
