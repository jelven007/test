from __future__ import annotations

import threading
from typing import Any, Callable, Optional


class PostgresAdvisoryLease:
    """Session-scoped PostgreSQL lease for one collector shard."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        lease_key: str,
        connection_factory: Optional[Callable[[], Any]] = None,
    ):
        if not lease_key:
            raise ValueError("lease_key is required")
        if connection_factory is None:
            if not dsn:
                raise ValueError("dsn is required")
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL lease requires `pip install -e '.[production]'`"
                ) from exc
            def connection_factory():
                return psycopg.connect(
                    dsn,
                    connect_timeout=5,
                    autocommit=True,
                )
        self.lease_key = lease_key
        self.connection_factory = connection_factory
        self._connection = None
        self._held = False
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            try:
                if self._connection is None or self._connection.closed:
                    self._connection = self.connection_factory()
                    self._held = False
                if self._held:
                    with self._connection.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        cursor.fetchone()
                    return True
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s))",
                        (self.lease_key,),
                    )
                    self._held = bool(cursor.fetchone()[0])
                    return self._held
            except Exception:
                self._close_unlocked()
                return False

    def _close_unlocked(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None
                self._held = False

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()
