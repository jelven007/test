from __future__ import annotations

import unittest
from datetime import date

from banxia_strategy.application.reference_sync import MarketReferenceSync
from banxia_strategy.mootdx_provider import REFERENCE_FILES
from banxia_strategy.ports.storage import ReportAsset


class FakeRepository:
    def __init__(self):
        self.calls = []

    def begin_market_reference_snapshot(self, as_of_date):
        self.calls.append(("begin", as_of_date))
        return "snapshot-1"

    def publish_market_reference_snapshot(self, snapshot_id, **kwargs):
        self.calls.append(("publish", snapshot_id, kwargs))
        return snapshot_id

    def fail_market_reference_snapshot(self, snapshot_id, error):
        self.calls.append(("fail", snapshot_id, str(error)))


class FakeObjectStore:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def put(self, *, object_key, content, content_type, metadata):
        self.calls.append((object_key, content, content_type, metadata))
        if self.fail:
            raise RuntimeError("object storage unavailable")
        return ReportAsset(
            report_id=metadata["report_id"],
            format=metadata["format"],
            object_key=object_key,
            content_hash="a" * 64,
            content_type=content_type,
            size_bytes=len(content),
        )


class FakeProvider:
    def reference_snapshot(self):
        return {
            "source_node": "example:7709",
            "raw_securities": [
                {"market": 1, "code": "600001", "name": "样本股份"}
            ],
            "expected_counts": {1: 1, 0: 0},
            "files": {
                filename: f"raw:{filename}".encode()
                for filename in REFERENCE_FILES
            },
            "securities": [
                {
                    "market": 1,
                    "exchange": "sh",
                    "instrument_type": "stock",
                    "symbol": "600001",
                    "name": "样本股份",
                    "board": "main",
                    "previous_close": 10,
                    "volume_unit": 100,
                    "decimal_point": 2,
                    "raw": {},
                }
            ],
            "memberships": [
                {
                    "block_type": "concept",
                    "source_code": "新能源车",
                    "block_name": "新能源车",
                    "symbol": "600001",
                    "exchange": "sh",
                    "source_filename": "block_gn.dat",
                    "raw": {},
                }
            ],
        }


class MarketReferenceSyncTest(unittest.TestCase):
    def test_raw_objects_are_written_before_database_publish(self):
        repository = FakeRepository()
        object_store = FakeObjectStore()
        worker = MarketReferenceSync(
            repository=repository,
            object_store=object_store,
            provider=FakeProvider(),
        )

        result = worker.run(date(2026, 9, 25))

        self.assertEqual(repository.calls[0][0], "begin")
        self.assertEqual(repository.calls[-1][0], "publish")
        self.assertEqual(len(object_store.calls), len(REFERENCE_FILES) + 2)
        self.assertIn(
            "manifests/dataset=market_reference/",
            object_store.calls[-1][0],
        )
        publish = repository.calls[-1][2]
        self.assertEqual(publish["source_node"], "example:7709")
        self.assertEqual(publish["row_count"], 2)
        self.assertEqual(result["snapshot_id"], "snapshot-1")

    def test_object_failure_marks_snapshot_failed_without_publish(self):
        repository = FakeRepository()
        worker = MarketReferenceSync(
            repository=repository,
            object_store=FakeObjectStore(fail=True),
            provider=FakeProvider(),
        )

        with self.assertRaisesRegex(RuntimeError, "object storage"):
            worker.run(date(2026, 9, 25))

        self.assertEqual(
            [call[0] for call in repository.calls],
            ["begin", "fail"],
        )


if __name__ == "__main__":
    unittest.main()
