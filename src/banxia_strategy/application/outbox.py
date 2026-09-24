from __future__ import annotations

import socket
import threading
import uuid
from typing import Any, Dict, Optional

from ..ports.storage import EventPublisher


class OutboxRelay:
    """Lease, publish and acknowledge PostgreSQL outbox records."""

    def __init__(
        self,
        *,
        repository: Any,
        publisher: EventPublisher,
        worker_id: Optional[str] = None,
        batch_size: int = 100,
        lease_seconds: int = 30,
        idle_seconds: float = 0.5,
    ):
        if batch_size <= 0 or lease_seconds <= 0 or idle_seconds <= 0:
            raise ValueError("relay timing and batch settings must be positive")
        self.repository = repository
        self.publisher = publisher
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "published": 0,
            "failed": 0,
            "last_error": None,
            "running": False,
        }

    def run_once(self) -> int:
        records = self.repository.claim_outbox(
            self.worker_id,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        published = 0
        for record in records:
            try:
                self.publisher.publish(
                    record.topic,
                    record.message_key,
                    record.event,
                )
                if not self.repository.mark_outbox_published(
                    record.outbox_id,
                    self.worker_id,
                ):
                    raise RuntimeError("outbox lease was lost before acknowledgement")
            except Exception as exc:
                self.repository.mark_outbox_failed(
                    record.outbox_id,
                    self.worker_id,
                    str(exc),
                )
                with self._lock:
                    self._status["failed"] += 1
                    self._status["last_error"] = str(exc)
                continue
            published += 1
            with self._lock:
                self._status["published"] += 1
                self._status["last_error"] = None
        return published

    def run_forever(self) -> None:
        with self._lock:
            self._status["running"] = True
        try:
            while not self.stop_event.is_set():
                published = self.run_once()
                if published == 0:
                    self.stop_event.wait(self.idle_seconds)
        finally:
            with self._lock:
                self._status["running"] = False

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        self.stop()
        for target in (self.publisher, self.repository):
            close = getattr(target, "close", None)
            if close is not None:
                close()
