from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from banxia_strategy.domain import (
    DecisionState,
    EventEnvelope,
    advice,
    build_event_id,
    resolve_transition,
)


class EventEnvelopeTest(unittest.TestCase):
    def test_event_id_is_stable_for_equivalent_identity(self):
        first = build_event_id(
            "market.quote.snapshot.v1",
            {"symbol": "002909", "price": Decimal("8.290"), "source_time": "09:38:11"},
        )
        second = build_event_id(
            "market.quote.snapshot.v1",
            {"source_time": "09:38:11", "price": Decimal("8.290"), "symbol": "002909"},
        )
        changed = build_event_id(
            "market.quote.snapshot.v1",
            {"symbol": "002909", "price": Decimal("8.300"), "source_time": "09:38:11"},
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertEqual(len(first), 64)

    def test_envelope_requires_timezone_and_normalizes_payload(self):
        occurred_at = datetime(2026, 9, 24, 9, 38, tzinfo=timezone.utc)
        event = EventEnvelope.create(
            event_type="market.quote.snapshot.v1",
            producer="market-collector",
            occurred_at=occurred_at,
            published_at=occurred_at,
            identity={"symbol": "002909", "source_time": occurred_at},
            payload={"symbol": "002909", "price": Decimal("8.29")},
            trace_id="trace-1",
        )
        self.assertEqual(event.payload["price"], "8.29")
        self.assertEqual(event.trace_id, "trace-1")
        self.assertEqual(event.to_dict()["schema_version"], 1)

        with self.assertRaises(ValueError):
            EventEnvelope.create(
                event_type="market.quote.snapshot.v1",
                producer="market-collector",
                occurred_at=datetime(2026, 9, 24, 9, 38),
                identity={"symbol": "002909"},
                payload={},
            )
        with self.assertRaises(ValueError):
            EventEnvelope.create(
                event_type="market.quote.snapshot.v1",
                producer="market-collector",
                occurred_at=occurred_at,
                identity={"symbol": "002909"},
                payload={"price": float("nan")},
            )


class DecisionDomainTest(unittest.TestCase):
    def test_advice_accepts_enum_without_changing_public_shape(self):
        result = advice(DecisionState.WATCH, "观察", "等待确认")
        self.assertEqual(
            result,
            {
                "state": "watch",
                "label": "观察",
                "reason": "等待确认",
                "tone": "neutral",
            },
        )

    def test_irreversible_transition_cannot_be_weakened(self):
        rejected = {
            "state": "reject_low",
            "label": "放弃",
            "reason": "跌破昨收",
            "tone": "risk",
        }
        watching = {
            "state": "watch",
            "label": "观察",
            "reason": "价格反弹",
            "tone": "neutral",
        }
        self.assertEqual(resolve_transition(rejected, watching), rejected)
        self.assertEqual(resolve_transition(watching, rejected), rejected)


if __name__ == "__main__":
    unittest.main()
