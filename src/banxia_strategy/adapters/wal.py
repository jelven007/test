from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..domain.events import EventEnvelope
from ..ports.messaging import PendingEvent


class WalCapacityError(RuntimeError):
    pass


class SQLiteEventWAL:
    """Durable local outbox used before Kafka acknowledgement.

    SQLite is the portable local implementation. The EventWAL port allows a
    RocksDB implementation to replace it in production without changing the
    collector.
    """

    def __init__(self, path: Path, *, max_bytes: int = 512 * 1024 * 1024):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            timeout=5,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_event (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                topic TEXT NOT NULL,
                message_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def _allocated_bytes(self) -> int:
        page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        return page_count * page_size

    def append(self, topic: str, key: str, event: EventEnvelope) -> bool:
        if not topic or not key:
            raise ValueError("topic and key are required")
        payload = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            existing = self._connection.execute(
                "SELECT 1 FROM pending_event WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                return False
            if self._allocated_bytes() + len(payload.encode("utf-8")) > self.max_bytes:
                raise WalCapacityError(
                    f"event WAL reached its {self.max_bytes}-byte capacity"
                )
            self._connection.execute(
                """
                INSERT INTO pending_event (
                    event_id, topic, message_key, payload, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    topic,
                    key,
                    payload,
                    datetime.now().astimezone().isoformat(timespec="milliseconds"),
                ),
            )
            self._connection.commit()
        return True

    def pending(self, limit: int = 100) -> Iterable[PendingEvent]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT sequence, topic, message_key, payload, attempts,
                       created_at, last_error
                FROM pending_event
                ORDER BY sequence
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            PendingEvent(
                sequence=int(row[0]),
                topic=str(row[1]),
                key=str(row[2]),
                event=EventEnvelope.from_dict(json.loads(row[3])),
                attempts=int(row[4]),
                created_at=datetime.fromisoformat(row[5]),
                last_error=row[6],
            )
            for row in rows
        )

    def mark_published(self, event_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM pending_event WHERE event_id = ?",
                (event_id,),
            )
            self._connection.commit()

    def mark_failed(self, event_id: str, error: str) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE pending_event
                SET attempts = attempts + 1, last_error = ?
                WHERE event_id = ?
                """,
                (error[:2000], event_id),
            )
            self._connection.commit()

    def count(self) -> int:
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT count(*) FROM pending_event"
                ).fetchone()[0]
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
