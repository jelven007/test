from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from ..domain.events import EventEnvelope


QUOTE_COLUMNS = (
    "trade_date",
    "symbol",
    "source_time",
    "collected_at",
    "published_at",
    "event_id",
    "price",
    "open",
    "high",
    "low",
    "previous_close",
    "cumulative_volume",
    "cumulative_amount_cny",
    "bid1",
    "bid1_volume",
    "ask1",
    "ask1_volume",
    "source_node",
    "ingest_version",
)

BAR_COLUMNS = (
    "trade_date",
    "symbol",
    "bar_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount_cny",
    "source_time",
    "collected_at",
    "event_id",
    "revision",
)

FEATURE_COLUMNS = (
    "trade_date",
    "symbol",
    "feature_name",
    "feature_version",
    "window_start",
    "window_end",
    "computed_at",
    "event_id",
    "value",
    "attributes_json",
    "data_state",
    "max_input_time",
    "ingest_version",
)


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value))


def _integer(value: Any) -> Optional[int]:
    if value is None:
        return None
    return max(0, int(float(value)))


def _version(value: Any, fallback: datetime) -> int:
    if value is not None:
        return max(0, int(value))
    return max(0, int(fallback.timestamp() * 1000))


def _payload(event: EventEnvelope, expected_type: str) -> Mapping[str, Any]:
    if event.event_type != expected_type:
        raise ValueError(
            f"expected {expected_type}, received {event.event_type}"
        )
    return event.payload


class ClickHouseMarketHistoryStore:
    """Batch writer for normalized market events."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8123,
        database: str = "banxia",
        username: str = "banxia",
        password: str = "",
        client: Any = None,
    ):
        if client is None:
            try:
                import clickhouse_connect
            except ImportError as exc:
                raise RuntimeError(
                    "ClickHouse adapter requires `pip install -e '.[storage]'`"
                ) from exc
            client = clickhouse_connect.get_client(
                host=host,
                port=port,
                database=database,
                username=username,
                password=password,
                connect_timeout=3,
                send_receive_timeout=5,
            )
        self.client = client

    def _insert(
        self,
        table: str,
        rows: Sequence[Sequence[Any]],
        columns: Sequence[str],
    ) -> None:
        if rows:
            self.client.insert(table, list(rows), column_names=list(columns))

    def append_quote_snapshots(self, events: Iterable[EventEnvelope]) -> None:
        rows: List[Sequence[Any]] = []
        for event in events:
            data = _payload(event, "market.quote.snapshot.v1")
            published_at = _datetime(event.published_at)
            rows.append(
                (
                    _date(data["trade_date"]),
                    str(data["symbol"]),
                    _datetime(data["source_time"]),
                    _datetime(data["collected_at"]),
                    published_at,
                    event.event_id,
                    _decimal(data.get("price")),
                    _decimal(data.get("open")),
                    _decimal(data.get("high")),
                    _decimal(data.get("low")),
                    _decimal(data.get("previous_close")),
                    _integer(data.get("cumulative_volume")),
                    _decimal(data.get("cumulative_amount_cny")),
                    _decimal(data.get("bid1")),
                    _integer(data.get("bid1_volume")),
                    _decimal(data.get("ask1")),
                    _integer(data.get("ask1_volume")),
                    str(data.get("source_node") or "mootdx"),
                    _version(data.get("ingest_version"), published_at),
                )
            )
        self._insert("banxia.market_quote_snapshot", rows, QUOTE_COLUMNS)

    def upsert_minute_bars(self, events: Iterable[EventEnvelope]) -> None:
        rows: List[Sequence[Any]] = []
        for event in events:
            data = _payload(event, "market.bar.1m.v1")
            rows.append(
                (
                    _date(data["trade_date"]),
                    str(data["symbol"]),
                    _datetime(data["bar_time"]),
                    _decimal(data["open"]),
                    _decimal(data["high"]),
                    _decimal(data["low"]),
                    _decimal(data["close"]),
                    _integer(data["volume"]),
                    _decimal(data.get("amount_cny") or 0),
                    _datetime(data["source_time"]),
                    _datetime(data["collected_at"]),
                    event.event_id,
                    _version(data.get("revision"), _datetime(event.published_at)),
                )
            )
        self._insert("banxia.market_bar_1m", rows, BAR_COLUMNS)

    def append_features(self, events: Iterable[EventEnvelope]) -> None:
        rows: List[Sequence[Any]] = []
        for event in events:
            data = _payload(event, "market.feature.realtime.v1")
            computed_at = _datetime(data["computed_at"])
            attributes = json.dumps(
                data.get("attributes", {}),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            rows.append(
                (
                    _date(data["trade_date"]),
                    str(data["symbol"]),
                    str(data["feature_name"]),
                    str(data["feature_version"]),
                    _datetime(data["window_start"]),
                    _datetime(data["window_end"]),
                    computed_at,
                    event.event_id,
                    (
                        float(data["value"])
                        if data.get("value") is not None
                        else None
                    ),
                    attributes,
                    str(data.get("data_state") or "complete"),
                    _datetime(data["max_input_time"]),
                    _version(data.get("ingest_version"), computed_at),
                )
            )
        self._insert("banxia.market_feature_realtime", rows, FEATURE_COLUMNS)

    def ready(self) -> bool:
        try:
            result = self.client.query("SELECT 1")
            rows = getattr(result, "result_rows", ())
            return bool(rows and rows[0][0] == 1)
        except Exception:
            return False

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
