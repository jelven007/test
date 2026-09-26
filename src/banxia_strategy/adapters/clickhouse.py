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

HISTORY_COLUMNS = (
    "symbol",
    "period",
    "trade_date",
    "bar_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount_cny",
    "raw_json",
    "fetched_at",
    "revision",
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

    def upsert_history_bars(
        self,
        symbol: str,
        period: str,
        bars: Iterable[Mapping[str, Any]],
    ) -> None:
        fetched_at = datetime.now().astimezone()
        revision = int(fetched_at.timestamp() * 1000)
        rows_by_year: dict[int, List[Sequence[Any]]] = {}
        for item in bars:
            bar_time = _datetime(item["time"])
            rows_by_year.setdefault(bar_time.year, []).append(
                (
                    symbol,
                    period,
                    bar_time.date(),
                    bar_time,
                    _decimal(item.get("open")),
                    _decimal(item.get("high")),
                    _decimal(item.get("low")),
                    _decimal(item.get("close")),
                    _integer(item.get("volume")),
                    _decimal(item.get("amount")),
                    json.dumps(
                        item.get("raw", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    fetched_at,
                    revision,
                )
            )
        for rows in rows_by_year.values():
            self._insert(
                "banxia.market_history_bar",
                rows,
                HISTORY_COLUMNS,
            )

    def get_history_bars(
        self,
        symbol: str,
        period: str,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 800,
    ) -> List[Mapping[str, Any]]:
        clauses = [
            "symbol = {symbol:String}",
            "period = {period:String}",
        ]
        parameters: dict[str, Any] = {
            "symbol": symbol,
            "period": period,
            "limit": max(1, min(int(limit), 20000)),
        }
        if start_date is not None:
            clauses.append("trade_date >= {start_date:Date}")
            parameters["start_date"] = start_date
        if end_date is not None:
            clauses.append("trade_date <= {end_date:Date}")
            parameters["end_date"] = end_date
        result = self.client.query(
            f"""
            SELECT
                trade_date,
                bar_time,
                argMax(open, revision) AS open,
                argMax(high, revision) AS high,
                argMax(low, revision) AS low,
                argMax(close, revision) AS close,
                argMax(volume, revision) AS volume,
                argMax(amount_cny, revision) AS amount,
                argMax(raw_json, revision) AS raw_json,
                max(fetched_at) AS fetched_at
            FROM banxia.market_history_bar
            WHERE {" AND ".join(clauses)}
            GROUP BY trade_date, bar_time
            ORDER BY bar_time DESC
            LIMIT {{limit:UInt32}}
            """,
            parameters=parameters,
        )
        columns = tuple(getattr(result, "column_names", ()))
        rows = []
        for values in reversed(getattr(result, "result_rows", ())):
            item = dict(zip(columns, values))
            item["date"] = str(item.pop("trade_date"))
            item["time"] = item.pop("bar_time").isoformat()
            item["raw"] = json.loads(item.pop("raw_json") or "{}")
            item["fetched_at"] = item["fetched_at"].isoformat()
            for key in ("open", "high", "low", "close", "amount"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
            if item.get("volume") is not None:
                item["volume"] = int(item["volume"])
            rows.append(item)
        return rows

    def mark_history_sync(
        self,
        symbol: str,
        period: str,
        *,
        row_count: int,
        completed: bool,
    ) -> None:
        fetched_at = datetime.now().astimezone()
        self._insert(
            "banxia.market_history_sync",
            [
                (
                    symbol,
                    period,
                    max(0, int(row_count)),
                    int(completed),
                    fetched_at,
                    int(fetched_at.timestamp() * 1000),
                )
            ],
            (
                "symbol",
                "period",
                "row_count",
                "completed",
                "fetched_at",
                "revision",
            ),
        )

    def get_history_sync(
        self,
        symbol: str,
        period: str,
    ) -> Optional[Mapping[str, Any]]:
        result = self.client.query(
            """
            SELECT
                argMax(row_count, revision) AS row_count,
                argMax(completed, revision) AS completed,
                max(fetched_at) AS fetched_at
            FROM banxia.market_history_sync
            WHERE symbol = {symbol:String}
              AND period = {period:String}
            HAVING count() > 0
            """,
            parameters={"symbol": symbol, "period": period},
        )
        rows = getattr(result, "result_rows", ())
        if not rows:
            return None
        row_count, completed, fetched_at = rows[0]
        return {
            "row_count": int(row_count),
            "completed": bool(completed),
            "fetched_at": fetched_at.isoformat(),
        }

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
