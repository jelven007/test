from __future__ import annotations

import threading
from typing import Dict

from ..domain.events import EventEnvelope
from ..ports.messaging import EventWAL
from ..ports.storage import EventPublisher


class ReliableEventPublisher:
    """Persist first, publish second, delete only after broker acknowledgement."""

    def __init__(
        self,
        *,
        wal: EventWAL,
        publisher: EventPublisher,
        replay_batch_size: int = 100,
    ):
        if replay_batch_size <= 0:
            raise ValueError("replay_batch_size must be positive")
        self.wal = wal
        self.publisher = publisher
        self.replay_batch_size = replay_batch_size
        self._lock = threading.Lock()
        self._published = 0
        self._failed = 0
        self._last_error = None

    def publish(self, topic: str, key: str, event: EventEnvelope) -> None:
        self.wal.append(topic, key, event)
        try:
            self.publisher.publish(topic, key, event)
        except Exception as exc:
            self.wal.mark_failed(event.event_id, str(exc))
            with self._lock:
                self._failed += 1
                self._last_error = str(exc)
            raise
        self.wal.mark_published(event.event_id)
        with self._lock:
            self._published += 1
            self._last_error = None

    def replay(self) -> int:
        published = 0
        for pending in self.wal.pending(self.replay_batch_size):
            try:
                self.publisher.publish(
                    pending.topic,
                    pending.key,
                    pending.event,
                )
            except Exception as exc:
                self.wal.mark_failed(pending.event.event_id, str(exc))
                with self._lock:
                    self._failed += 1
                    self._last_error = str(exc)
                break
            self.wal.mark_published(pending.event.event_id)
            published += 1
            with self._lock:
                self._published += 1
                self._last_error = None
        return published

    def status(self) -> Dict[str, object]:
        with self._lock:
            result: Dict[str, object] = {
                "published": self._published,
                "failed": self._failed,
                "last_error": self._last_error,
            }
        result["wal_pending"] = self.wal.count()
        return result

    def close(self) -> None:
        for target in (self.publisher, self.wal):
            close = getattr(target, "close", None)
            if close is not None:
                close()
