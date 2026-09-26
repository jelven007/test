from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence


REFERENCE_DATASET = "market_reference"
REFERENCE_SCOPE = "all"
REFERENCE_SCHEMA_VERSION = 1
REFERENCE_NAMESPACE = uuid.UUID("6af78e35-e3cd-4c62-bdba-07ac9c41e83d")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _block_id(item: Mapping[str, Any]) -> str:
    identity = ":".join(
        (
            str(item["block_type"]),
            str(item["source_code"]),
            str(item["block_name"]),
        )
    )
    return str(uuid.uuid5(REFERENCE_NAMESPACE, identity))


def _instrument_id(item: Mapping[str, Any]) -> str:
    return (
        f"{item['exchange']}:{item.get('instrument_type', 'stock')}:"
        f"{item['symbol']}"
    )


class MarketReferenceMixin:
    """PostgreSQL-backed versioned security and block reference data."""

    def begin_market_reference_snapshot(self, as_of_date: date) -> str:
        snapshot_id = str(uuid.uuid4())
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.source_snapshot (
                        snapshot_id, dataset, scope_key, as_of_date, status,
                        schema_version
                    ) VALUES (%s, %s, %s, %s, 'running', %s)
                    """,
                    (
                        snapshot_id,
                        REFERENCE_DATASET,
                        REFERENCE_SCOPE,
                        as_of_date,
                        REFERENCE_SCHEMA_VERSION,
                    ),
                )
        return snapshot_id

    def fail_market_reference_snapshot(
        self,
        snapshot_id: str,
        error: Any,
    ) -> None:
        detail = error if isinstance(error, Mapping) else {"message": str(error)}
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.source_snapshot
                    SET status = 'failed',
                        finished_at = now(),
                        error_summary = %s::jsonb
                    WHERE snapshot_id = %s
                      AND status IN ('running', 'validating')
                    """,
                    (_json(detail), snapshot_id),
                )
                cursor.execute(
                    """
                    INSERT INTO banxia.source_sync_state (
                        dataset, scope_key, consecutive_failures,
                        freshness_status, updated_at
                    ) VALUES (%s, %s, 1, 'unavailable', now())
                    ON CONFLICT (dataset, scope_key) DO UPDATE SET
                        consecutive_failures =
                            banxia.source_sync_state.consecutive_failures + 1,
                        freshness_status = CASE
                            WHEN banxia.source_sync_state.published_snapshot_id
                                IS NULL
                            THEN 'unavailable'
                            ELSE 'stale'
                        END,
                        updated_at = now()
                    """,
                    (REFERENCE_DATASET, REFERENCE_SCOPE),
                )

    def publish_market_reference_snapshot(
        self,
        snapshot_id: str,
        *,
        source_node: str,
        source_version: str,
        content_sha256: str,
        raw_object_key: str,
        row_count: int,
        expected_count: int,
        securities: Iterable[Mapping[str, Any]],
        memberships: Iterable[Mapping[str, Any]],
        daily_snapshots: Iterable[Mapping[str, Any]] = (),
        observed_at: Optional[datetime] = None,
    ) -> str:
        observed_at = observed_at or datetime.now(timezone.utc)
        security_rows = tuple(self._security_row(item) for item in securities)
        membership_rows = tuple(
            self._membership_row(item) for item in memberships
        )
        daily_rows = tuple(
            self._daily_snapshot_row(item) for item in daily_snapshots
        )
        if not security_rows:
            raise ValueError("security catalog is empty")
        if not membership_rows:
            raise ValueError("market block membership is empty")
        if len(content_sha256) != 64:
            raise ValueError("content_sha256 must be a SHA-256 hex digest")

        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT snapshot_id
                    FROM banxia.source_snapshot
                    WHERE dataset = %s
                      AND scope_key = %s
                      AND as_of_date = (
                          SELECT as_of_date
                          FROM banxia.source_snapshot
                          WHERE snapshot_id = %s
                      )
                      AND content_sha256 = %s
                      AND status = 'published'
                      AND snapshot_id <> %s
                    LIMIT 1
                    """,
                    (
                        REFERENCE_DATASET,
                        REFERENCE_SCOPE,
                        snapshot_id,
                        content_sha256,
                        snapshot_id,
                    ),
                )
                duplicate = cursor.fetchone()
                if duplicate is not None:
                    cursor.execute(
                        """
                        UPDATE banxia.source_snapshot
                        SET status = 'failed',
                            finished_at = %s,
                            error_summary = %s::jsonb
                        WHERE snapshot_id = %s
                        """,
                        (
                            observed_at,
                            _json(
                                {
                                    "reason": "duplicate_content",
                                    "published_snapshot_id": str(duplicate[0]),
                                }
                            ),
                            snapshot_id,
                        ),
                    )
                    return str(duplicate[0])

                cursor.execute(
                    """
                    UPDATE banxia.source_snapshot
                    SET status = 'validating',
                        source_node = %s,
                        source_version = %s,
                        content_sha256 = %s,
                        raw_object_key = %s,
                        row_count = %s,
                        expected_count = %s
                    WHERE snapshot_id = %s
                      AND status = 'running'
                    """,
                    (
                        source_node,
                        source_version,
                        content_sha256,
                        raw_object_key,
                        row_count,
                        expected_count,
                        snapshot_id,
                    ),
                )

                self._stage_securities(
                    cursor,
                    snapshot_id,
                    observed_at,
                    security_rows,
                )
                self._stage_daily_snapshots(
                    cursor,
                    snapshot_id,
                    daily_rows,
                )
                self._stage_memberships(
                    cursor,
                    snapshot_id,
                    observed_at,
                    membership_rows,
                )

                cursor.execute(
                    """
                    UPDATE banxia.source_snapshot
                    SET status = 'published',
                        finished_at = %s,
                        error_summary = NULL
                    WHERE snapshot_id = %s
                      AND status = 'validating'
                    """,
                    (observed_at, snapshot_id),
                )
                cursor.execute(
                    """
                    INSERT INTO banxia.source_sync_state (
                        dataset, scope_key, published_snapshot_id,
                        watermark, last_complete_at, consecutive_failures,
                        next_retry_at, freshness_status, updated_at
                    ) VALUES (
                        %s, %s, %s, %s::jsonb, %s, 0, NULL, 'fresh', %s
                    )
                    ON CONFLICT (dataset, scope_key) DO UPDATE SET
                        published_snapshot_id =
                            EXCLUDED.published_snapshot_id,
                        watermark = EXCLUDED.watermark,
                        last_complete_at = EXCLUDED.last_complete_at,
                        consecutive_failures = 0,
                        next_retry_at = NULL,
                        freshness_status = 'fresh',
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        REFERENCE_DATASET,
                        REFERENCE_SCOPE,
                        snapshot_id,
                        _json({"observed_at": observed_at.isoformat()}),
                        observed_at,
                        observed_at,
                    ),
                )
        return snapshot_id

    @staticmethod
    def _security_row(item: Mapping[str, Any]) -> tuple[Any, ...]:
        normalized = dict(item)
        normalized.setdefault("instrument_type", "stock")
        normalized["instrument_id"] = _instrument_id(normalized)
        stable_raw = dict(normalized.get("raw") or {})
        for key in ("pre_close", "last_close", "price"):
            stable_raw.pop(key, None)
        normalized["raw"] = stable_raw
        version = {
            key: normalized.get(key)
            for key in (
                "name",
                "board",
                "volume_unit",
                "decimal_point",
                "raw",
            )
        }
        return (
            normalized["instrument_id"],
            int(normalized["market"]),
            str(normalized["exchange"]),
            str(normalized["instrument_type"]),
            str(normalized["symbol"]),
            str(normalized["board"]),
            str(normalized["name"]),
            int(normalized.get("volume_unit") or 100),
            int(normalized.get("decimal_point") or 2),
            None,
            _content_hash(version),
            _json(stable_raw),
        )

    @staticmethod
    def _daily_snapshot_row(item: Mapping[str, Any]) -> tuple[Any, ...]:
        instrument_id = (
            str(item.get("instrument_id"))
            if item.get("instrument_id")
            else _instrument_id(item)
        )
        return (
            instrument_id,
            item["trade_date"],
            str(item["source_node"]),
            item.get("source_time"),
            item["collected_at"],
            item.get("open"),
            item.get("high"),
            item.get("low"),
            item.get("close"),
            item.get("previous_close"),
            item.get("change_pct"),
            item.get("volume"),
            item.get("amount"),
            str(item["data_state"]),
            _json(item.get("raw") or {}),
        )

    @staticmethod
    def _membership_row(item: Mapping[str, Any]) -> tuple[Any, ...]:
        block_id = _block_id(item)
        instrument_id = (
            f"{item['exchange']}:stock:{item['symbol']}"
        )
        return (
            block_id,
            str(item["block_type"]),
            str(item["source_code"]),
            str(item["block_name"]),
            instrument_id,
            str(item["source_filename"]),
            _json(item.get("raw") or {}),
        )

    @staticmethod
    def _stage_securities(
        cursor: Any,
        snapshot_id: str,
        observed_at: datetime,
        rows: Sequence[tuple[Any, ...]],
    ) -> None:
        cursor.execute(
            """
            CREATE TEMP TABLE incoming_security (
                instrument_id TEXT,
                market SMALLINT,
                exchange TEXT,
                instrument_type TEXT,
                symbol VARCHAR(6),
                board TEXT,
                name TEXT,
                volume_unit INTEGER,
                decimal_point SMALLINT,
                previous_close NUMERIC(18, 4),
                version_hash CHAR(64),
                raw JSONB
            ) ON COMMIT DROP
            """
        )
        cursor.executemany(
            """
            INSERT INTO incoming_security VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            """,
            rows,
        )
        cursor.execute(
            """
            UPDATE banxia.security_master_version current
            SET valid_to = %s
            FROM incoming_security incoming
            WHERE current.instrument_id = incoming.instrument_id
              AND current.valid_to IS NULL
              AND current.content_sha256 <> incoming.version_hash
            """,
            (observed_at,),
        )
        cursor.execute(
            """
            INSERT INTO banxia.security_master (
                instrument_id, market, exchange, instrument_type, symbol,
                board, name, volume_unit, decimal_point, listing_status,
                first_seen_at, last_seen_at, current_snapshot_id, raw
            )
            SELECT
                instrument_id, market, exchange, instrument_type, symbol,
                board, name, volume_unit, decimal_point, 'active',
                %s, %s, %s, raw
            FROM incoming_security
            ON CONFLICT (instrument_id) DO UPDATE SET
                board = EXCLUDED.board,
                name = EXCLUDED.name,
                volume_unit = EXCLUDED.volume_unit,
                decimal_point = EXCLUDED.decimal_point,
                listing_status = 'active',
                last_seen_at = EXCLUDED.last_seen_at,
                delisted_at = NULL,
                current_snapshot_id = EXCLUDED.current_snapshot_id,
                raw = EXCLUDED.raw
            """,
            (observed_at, observed_at, snapshot_id),
        )
        cursor.execute(
            """
            INSERT INTO banxia.security_master_version (
                instrument_id, valid_from, snapshot_id, name, board,
                volume_unit, decimal_point, previous_close,
                content_sha256, last_seen_at, raw
            )
            SELECT
                incoming.instrument_id, %s, %s, incoming.name,
                incoming.board, incoming.volume_unit,
                incoming.decimal_point, incoming.previous_close,
                incoming.version_hash, %s, incoming.raw
            FROM incoming_security incoming
            LEFT JOIN banxia.security_master_version current
              ON current.instrument_id = incoming.instrument_id
             AND current.valid_to IS NULL
            WHERE current.instrument_id IS NULL
            """,
            (observed_at, snapshot_id, observed_at),
        )
        cursor.execute(
            """
            UPDATE banxia.security_master_version current
            SET last_seen_at = %s
            FROM incoming_security incoming
            WHERE current.instrument_id = incoming.instrument_id
              AND current.valid_to IS NULL
              AND current.content_sha256 = incoming.version_hash
            """,
            (observed_at,),
        )

    @staticmethod
    def _stage_daily_snapshots(
        cursor: Any,
        snapshot_id: str,
        rows: Sequence[tuple[Any, ...]],
    ) -> None:
        if not rows:
            return
        cursor.executemany(
            """
            INSERT INTO banxia.security_daily_snapshot (
                instrument_id, trade_date, snapshot_id, source_node,
                source_time, collected_at, open, high, low, close,
                previous_close, change_pct, volume, amount, data_state, raw
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT (instrument_id, trade_date) DO UPDATE SET
                snapshot_id = EXCLUDED.snapshot_id,
                source_node = EXCLUDED.source_node,
                source_time = EXCLUDED.source_time,
                collected_at = EXCLUDED.collected_at,
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                previous_close = EXCLUDED.previous_close,
                change_pct = EXCLUDED.change_pct,
                volume = EXCLUDED.volume,
                amount = EXCLUDED.amount,
                data_state = EXCLUDED.data_state,
                raw = EXCLUDED.raw
            """,
            ((row[0], row[1], snapshot_id, *row[2:]) for row in rows),
        )

    @staticmethod
    def _stage_memberships(
        cursor: Any,
        snapshot_id: str,
        observed_at: datetime,
        rows: Sequence[tuple[Any, ...]],
    ) -> None:
        cursor.execute(
            """
            CREATE TEMP TABLE incoming_membership (
                block_id UUID,
                block_type TEXT,
                source_code TEXT,
                block_name TEXT,
                instrument_id TEXT,
                source_filename TEXT,
                raw JSONB,
                PRIMARY KEY (block_id, instrument_id)
            ) ON COMMIT DROP
            """
        )
        cursor.executemany(
            """
            INSERT INTO incoming_membership VALUES (
                %s, %s, %s, %s, %s, %s, %s::jsonb
            ) ON CONFLICT (block_id, instrument_id) DO NOTHING
            """,
            rows,
        )
        cursor.execute(
            """
            INSERT INTO banxia.market_block (
                block_id, block_type, source_code, block_name,
                active, first_seen_at, last_seen_at
            )
            SELECT DISTINCT
                block_id, block_type, source_code, block_name,
                TRUE, %s, %s
            FROM incoming_membership
            ON CONFLICT (block_id) DO UPDATE SET
                active = TRUE,
                last_seen_at = EXCLUDED.last_seen_at
            """,
            (observed_at, observed_at),
        )
        cursor.execute(
            """
            UPDATE banxia.market_block_membership_version current
            SET valid_to = %s
            WHERE current.valid_to IS NULL
              AND current.source_filename IN (
                  'block.dat', 'block_gn.dat', 'block_fg.dat',
                  'block_zs.dat', 'tdxhy.cfg'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM incoming_membership incoming
                  WHERE incoming.block_id = current.block_id
                    AND incoming.instrument_id = current.instrument_id
              )
            """,
            (observed_at,),
        )
        cursor.execute(
            """
            INSERT INTO banxia.market_block_membership_version (
                block_id, instrument_id, valid_from, snapshot_id,
                source_filename, raw
            )
            SELECT
                incoming.block_id, incoming.instrument_id, %s, %s,
                incoming.source_filename, incoming.raw
            FROM incoming_membership incoming
            LEFT JOIN banxia.market_block_membership_version current
              ON current.block_id = incoming.block_id
             AND current.instrument_id = incoming.instrument_id
             AND current.valid_to IS NULL
            WHERE current.block_id IS NULL
            """,
            (observed_at, snapshot_id),
        )
        cursor.execute(
            """
            UPDATE banxia.market_block block
            SET active = EXISTS (
                SELECT 1
                FROM banxia.market_block_membership_version membership
                WHERE membership.block_id = block.block_id
                  AND membership.valid_to IS NULL
            )
            WHERE block.block_type IN (
                'default', 'concept', 'style', 'index', 'industry'
            )
            """
        )

    def list_stock_blocks(self) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT block.block_name, COUNT(DISTINCT security.symbol)
                    FROM banxia.market_block block
                    JOIN banxia.market_block_membership_version membership
                      ON membership.block_id = block.block_id
                     AND membership.valid_to IS NULL
                    JOIN banxia.security_master security
                      ON security.instrument_id = membership.instrument_id
                     AND security.listing_status = 'active'
                     AND security.instrument_type = 'stock'
                    WHERE block.active
                    GROUP BY block.block_name
                    ORDER BY block.block_name
                    """
                )
                rows = cursor.fetchall()
        return [
            {"blockname": str(blockname), "count": int(count)}
            for blockname, count in rows
        ]

    def list_securities(
        self,
        *,
        query: Optional[str] = None,
        board: str = "all",
        market: str = "all",
        block: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions = [
            "security.listing_status = 'active'",
            "security.instrument_type = 'stock'",
        ]
        parameters: list[Any] = []
        joins = ""
        if board != "all":
            conditions.append("security.board = %s")
            parameters.append(board)
        if market != "all":
            conditions.append("security.exchange = %s")
            parameters.append(market)
        if query:
            conditions.append(
                "(security.symbol ILIKE %s OR security.name ILIKE %s)"
            )
            pattern = f"%{query}%"
            parameters.extend((pattern, pattern))
        if block:
            joins = """
                JOIN banxia.market_block_membership_version membership
                  ON membership.instrument_id = security.instrument_id
                 AND membership.valid_to IS NULL
                JOIN banxia.market_block block
                  ON block.block_id = membership.block_id
                 AND block.active
            """
            conditions.append("block.block_name = %s")
            parameters.append(block)

        where = " AND ".join(conditions)
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                if block:
                    cursor.execute(
                        """
                        SELECT 1 FROM banxia.market_block
                        WHERE block_name = %s AND active
                        LIMIT 1
                        """,
                        (block,),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError("股票板块无效")
                cursor.execute(
                    f"""
                    SELECT COUNT(DISTINCT security.instrument_id)
                    FROM banxia.security_master security
                    {joins}
                    WHERE {where}
                    """,
                    tuple(parameters),
                )
                total = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""
                    SELECT DISTINCT
                        security.symbol, security.name, security.exchange,
                        security.board, daily.previous_close,
                        security.volume_unit, security.decimal_point
                    FROM banxia.security_master security
                    JOIN banxia.security_master_version version
                      ON version.instrument_id = security.instrument_id
                     AND version.valid_to IS NULL
                    LEFT JOIN LATERAL (
                        SELECT snapshot.previous_close
                        FROM banxia.security_daily_snapshot snapshot
                        WHERE snapshot.instrument_id =
                            security.instrument_id
                          AND snapshot.data_state = 'available'
                        ORDER BY snapshot.trade_date DESC
                        LIMIT 1
                    ) daily ON TRUE
                    {joins}
                    WHERE {where}
                    ORDER BY security.symbol
                    LIMIT %s OFFSET %s
                    """,
                    tuple(parameters) + (limit, offset),
                )
                rows = cursor.fetchall()
        return {
            "total": total,
            "items": [
                {
                    "symbol": str(row[0]),
                    "name": str(row[1]),
                    "market": str(row[2]),
                    "board": str(row[3]),
                    "previous_close": (
                        float(row[4]) if row[4] is not None else None
                    ),
                    "volume_unit": int(row[5]),
                    "decimal_point": int(row[6]),
                }
                for row in rows
            ],
        }

    def get_security(self, symbol: str) -> Optional[dict[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        security.symbol, security.name, security.exchange,
                        security.board, daily.previous_close,
                        security.volume_unit, security.decimal_point
                    FROM banxia.security_master security
                    JOIN banxia.security_master_version version
                      ON version.instrument_id = security.instrument_id
                     AND version.valid_to IS NULL
                    LEFT JOIN LATERAL (
                        SELECT snapshot.previous_close
                        FROM banxia.security_daily_snapshot snapshot
                        WHERE snapshot.instrument_id =
                            security.instrument_id
                          AND snapshot.data_state = 'available'
                        ORDER BY snapshot.trade_date DESC
                        LIMIT 1
                    ) daily ON TRUE
                    WHERE security.symbol = %s
                      AND security.instrument_type = 'stock'
                      AND security.listing_status = 'active'
                    ORDER BY security.exchange
                    LIMIT 1
                    """,
                    (symbol,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "symbol": str(row[0]),
            "name": str(row[1]),
            "market": str(row[2]),
            "board": str(row[3]),
            "previous_close": float(row[4]) if row[4] is not None else None,
            "volume_unit": int(row[5]),
            "decimal_point": int(row[6]),
        }

    def get_latest_security_daily_snapshots(
        self,
        symbols: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        requested = tuple(dict.fromkeys(symbols))
        if not requested:
            return {}
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (security.symbol)
                        security.symbol, snapshot.trade_date,
                        snapshot.source_node, snapshot.source_time,
                        snapshot.collected_at, snapshot.open, snapshot.high,
                        snapshot.low, snapshot.close,
                        snapshot.previous_close, snapshot.change_pct,
                        snapshot.volume, snapshot.amount,
                        snapshot.data_state
                    FROM banxia.security_master security
                    JOIN banxia.security_daily_snapshot snapshot
                      ON snapshot.instrument_id = security.instrument_id
                    WHERE security.symbol = ANY(%s)
                      AND security.instrument_type = 'stock'
                    ORDER BY security.symbol, snapshot.trade_date DESC
                    """,
                    (list(requested),),
                )
                rows = cursor.fetchall()
        return {
            str(row[0]): self._daily_snapshot_mapping(row)
            for row in rows
        }

    def list_security_daily_history(
        self,
        symbol: str,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 250,
    ) -> list[dict[str, Any]]:
        conditions = ["security.symbol = %s"]
        parameters: list[Any] = [symbol]
        if start_date is not None:
            conditions.append("snapshot.trade_date >= %s")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("snapshot.trade_date <= %s")
            parameters.append(end_date)
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT
                        security.symbol, snapshot.trade_date,
                        snapshot.source_node, snapshot.source_time,
                        snapshot.collected_at, snapshot.open, snapshot.high,
                        snapshot.low, snapshot.close,
                        snapshot.previous_close, snapshot.change_pct,
                        snapshot.volume, snapshot.amount,
                        snapshot.data_state
                    FROM banxia.security_master security
                    JOIN banxia.security_daily_snapshot snapshot
                      ON snapshot.instrument_id = security.instrument_id
                    WHERE {' AND '.join(conditions)}
                      AND security.instrument_type = 'stock'
                    ORDER BY snapshot.trade_date DESC
                    LIMIT %s
                    """,
                    (*parameters, limit),
                )
                rows = cursor.fetchall()
        return [
            self._daily_snapshot_mapping(row)
            for row in reversed(rows)
        ]

    @staticmethod
    def _daily_snapshot_mapping(row: Sequence[Any]) -> dict[str, Any]:
        return {
            "symbol": str(row[0]),
            "trade_date": row[1].isoformat(),
            "source_node": str(row[2]),
            "source_time": row[3].isoformat() if row[3] else None,
            "collected_at": row[4].isoformat(),
            "open": float(row[5]) if row[5] is not None else None,
            "high": float(row[6]) if row[6] is not None else None,
            "low": float(row[7]) if row[7] is not None else None,
            "price": float(row[8]) if row[8] is not None else None,
            "close": float(row[8]) if row[8] is not None else None,
            "last_close": float(row[9]) if row[9] is not None else None,
            "previous_close": (
                float(row[9]) if row[9] is not None else None
            ),
            "change_pct": float(row[10]) if row[10] is not None else None,
            "volume": float(row[11]) if row[11] is not None else None,
            "amount": float(row[12]) if row[12] is not None else None,
            "data_state": str(row[13]),
        }

    def latest_market_reference_snapshot(self) -> Optional[dict[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        snapshot.snapshot_id, snapshot.as_of_date,
                        snapshot.finished_at, snapshot.source_node,
                        snapshot.source_version, snapshot.row_count,
                        snapshot.expected_count, state.freshness_status
                    FROM banxia.source_sync_state state
                    JOIN banxia.source_snapshot snapshot
                      ON snapshot.snapshot_id = state.published_snapshot_id
                    WHERE state.dataset = %s
                      AND state.scope_key = %s
                    """,
                    (REFERENCE_DATASET, REFERENCE_SCOPE),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        finished_at = row[2]
        age = datetime.now(timezone.utc) - finished_at.astimezone(timezone.utc)
        age_state = (
            "fresh"
            if age <= timedelta(days=2)
            else "delayed"
            if age <= timedelta(days=7)
            else "stale"
        )
        stored_state = str(row[7])
        state_order = {
            "fresh": 0,
            "delayed": 1,
            "stale": 2,
            "unavailable": 3,
        }
        freshness = max(
            (age_state, stored_state),
            key=lambda value: state_order[value],
        )
        return {
            "snapshot_id": str(row[0]),
            "as_of_date": row[1].isoformat(),
            "source_as_of": row[1].isoformat(),
            "finished_at": finished_at.isoformat(),
            "fetched_at": finished_at.isoformat(),
            "source_node": row[3],
            "source_version": row[4],
            "row_count": int(row[5]),
            "expected_count": int(row[6]),
            "freshness": freshness,
            "data_state": freshness,
            "stale_after": (finished_at + timedelta(days=2)).isoformat(),
        }
