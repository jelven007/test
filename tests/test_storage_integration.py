from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from banxia_strategy.application.persistence import (
    build_market_persistence,
    persist_report_copy,
)
from banxia_strategy.storage_config import StorageSettings


RUN_INTEGRATION = os.environ.get("BANXIA_RUN_INTEGRATION") == "1"


@unittest.skipUnless(RUN_INTEGRATION, "set BANXIA_RUN_INTEGRATION=1")
class StorageIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = StorageSettings.from_env(
            {
                **os.environ,
                "BANXIA_STORAGE_MODE": "required",
                "BANXIA_STRATEGY_VERSION": "integration-v1",
                "BANXIA_CODE_COMMIT": "integration-test",
            }
        )
        cls.report = {
            "as_of": "2099-01-02",
            "next_session": "2099-01-03",
            "generated_at": "2099-01-02T16:20:00+08:00",
            "data_source": "mootdx",
            "data_sessions": ["2099-01-02"],
            "market": {"regime": "integration", "score": 80},
            "rejected_count": 0,
            "candidates": [
                {
                    "code": "600001",
                    "name": "集成测试",
                    "industry": "测试",
                    "rank": 1,
                    "score": 80.0,
                    "strategy": "一进二试错",
                    "latest_price": 10.0,
                    "amount_cny": 300_000_000,
                    "turnover_pct": 8.0,
                    "float_market_cap_cny": 8_000_000_000,
                    "reasons": ["真实组件双写验证"],
                    "entry_trigger": "测试触发",
                    "invalidation": "测试放弃",
                    "exit_plan": "测试退出",
                    "position_limit_pct": 20,
                }
            ],
        }

    def setUp(self):
        self._cleanup_database()

    def tearDown(self):
        self._cleanup_database()

    def _cleanup_database(self):
        import psycopg

        with psycopg.connect(self.settings.postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM banxia.inbox_event
                    WHERE event_id IN (
                        SELECT state.source_event_id
                        FROM banxia.decision_state AS state
                        JOIN banxia.strategy_plan AS plan
                          ON plan.plan_id = state.plan_id
                        JOIN banxia.strategy_version AS version
                          ON version.strategy_version_id =
                             plan.strategy_version_id
                        WHERE version.version = 'integration-v1'
                    )
                    """
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.outbox_event AS event
                    WHERE event.event_id IN (
                        SELECT decision.decision_event_id
                        FROM banxia.decision_event AS decision
                        JOIN banxia.strategy_plan AS plan
                          ON plan.plan_id = decision.plan_id
                        JOIN banxia.strategy_version AS version
                          ON version.strategy_version_id =
                             plan.strategy_version_id
                        WHERE version.version = 'integration-v1'
                    )
                    """
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.watchlist
                    WHERE plan_id IN (
                        SELECT plan.plan_id
                        FROM banxia.strategy_plan AS plan
                        JOIN banxia.strategy_version AS version
                          ON version.strategy_version_id =
                             plan.strategy_version_id
                        WHERE version.version = 'integration-v1'
                    )
                    """
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_plan
                    WHERE strategy_version_id IN (
                        SELECT strategy_version_id
                        FROM banxia.strategy_version
                        WHERE version = 'integration-v1'
                    )
                    """
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_run
                    WHERE strategy_version_id IN (
                        SELECT strategy_version_id
                        FROM banxia.strategy_version
                        WHERE version = 'integration-v1'
                    )
                    """
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_version
                    WHERE version = 'integration-v1'
                    """
                )

    def test_report_and_intraday_dual_write(self):
        import clickhouse_connect
        import psycopg
        import redis
        from minio import Minio

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "json": root / "candidates.json",
                "csv": root / "candidates.csv",
                "markdown": root / "report.md",
            }
            paths["json"].write_text(
                json.dumps(self.report, ensure_ascii=False),
                encoding="utf-8",
            )
            paths["csv"].write_text("code,name\n600001,集成测试\n", encoding="utf-8")
            paths["markdown"].write_text("# 集成测试\n", encoding="utf-8")

            report_result = persist_report_copy(
                self.report,
                paths,
                strategy_config={"minimum_amount_cny": 200_000_000},
                settings=self.settings,
            )

            self.assertEqual(report_result.errors, ())
            self.assertEqual(len(report_result.assets), 3)
            self.assertIsNotNone(report_result.identity)

            minio_client = Minio(
                self.settings.minio_endpoint,
                access_key=self.settings.minio_access_key,
                secret_key=self.settings.minio_secret_key,
                secure=self.settings.minio_secure,
            )
            for asset in report_result.assets:
                response = minio_client.get_object(
                    self.settings.minio_report_bucket,
                    asset.object_key,
                )
                try:
                    content = response.read()
                finally:
                    response.close()
                    response.release_conn()
                self.assertEqual(hashlib.sha256(content).hexdigest(), asset.content_hash)

        persistence = build_market_persistence(
            self.report,
            strategy_config={"minimum_amount_cny": 200_000_000},
            settings=self.settings,
        )
        self.assertIsNotNone(persistence)
        snapshot = {
            "collected_at": "2099-01-03T09:45:00+08:00",
            "plan_date": "2099-01-03",
            "stocks": [
                {
                    "code": "600001",
                    "plan": {"previous_close": 10.0},
                    "quote": {
                        "quote_time": "2099-01-03T09:45:00+08:00",
                        "price": 10.5,
                        "open": 10.2,
                        "high": 10.6,
                        "low": 10.1,
                        "previous_close": 10.0,
                        "amount": 300_000_000,
                        "volume": 1_000_000,
                        "bid": 10.49,
                        "ask": 10.5,
                        "bid_volume": 1000,
                        "ask_volume": 800,
                        "candles": [
                            {
                                "time": "2099-01-03T09:44:00+08:00",
                                "open": 10.4,
                                "high": 10.6,
                                "low": 10.3,
                                "close": 10.5,
                                "price": 10.5,
                                "volume": 10000,
                                "amount": 105000,
                            }
                        ],
                    },
                    "advice": {
                        "state": "watch",
                        "label": "观察",
                        "reason": "集成测试等待确认",
                        "tone": "neutral",
                    },
                }
            ],
        }
        try:
            self.assertTrue(persistence.submit(snapshot))
            self.assertTrue(persistence.submit(snapshot))
        finally:
            persistence.close()
        self.assertEqual(persistence.status()["failure_count"], 0)

        clickhouse = clickhouse_connect.get_client(
            host=self.settings.clickhouse_host,
            port=self.settings.clickhouse_port,
            database=self.settings.clickhouse_database,
            username=self.settings.clickhouse_user,
            password=self.settings.clickhouse_password,
        )
        try:
            quote_count = clickhouse.query(
                """
                SELECT count()
                FROM banxia.market_quote_snapshot FINAL
                WHERE trade_date = '2099-01-03' AND symbol = '600001'
                """
            ).result_rows[0][0]
            bar_count = clickhouse.query(
                """
                SELECT count()
                FROM banxia.market_bar_1m FINAL
                WHERE trade_date = '2099-01-03' AND symbol = '600001'
                """
            ).result_rows[0][0]
            latest_price = clickhouse.query(
                """
                SELECT price
                FROM banxia.market_quote_latest
                WHERE symbol = '600001'
                """
            ).result_rows[0][0]
        finally:
            clickhouse.close()
        self.assertEqual(quote_count, 1)
        self.assertEqual(bar_count, 1)
        self.assertEqual(float(latest_price), 10.5)

        with psycopg.connect(self.settings.postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM banxia.strategy_run
                         WHERE run_id = %s),
                        (SELECT count(*) FROM banxia.candidate
                         WHERE plan_id = %s AND symbol = '600001'),
                        (SELECT count(*) FROM banxia.report_asset
                         WHERE run_id = %s),
                        (SELECT count(*) FROM banxia.decision_state
                         WHERE plan_id = %s
                           AND symbol = '600001'
                           AND state = 'watch'),
                        (SELECT count(*) FROM banxia.decision_event
                         WHERE plan_id = %s AND symbol = '600001'),
                        (SELECT count(*) FROM banxia.inbox_event
                         WHERE event_id = (
                             SELECT source_event_id
                             FROM banxia.decision_state
                             WHERE plan_id = %s AND symbol = '600001'
                         )),
                        (SELECT count(*) FROM banxia.outbox_event
                         WHERE aggregate_id = %s)
                    """,
                    (
                        report_result.identity.run_id,
                        report_result.identity.plan_id,
                        report_result.identity.run_id,
                        report_result.identity.plan_id,
                        report_result.identity.plan_id,
                        report_result.identity.plan_id,
                        f"{report_result.identity.plan_id}:600001",
                    ),
                )
                counts = cursor.fetchone()
        self.assertEqual(counts, (1, 1, 3, 1, 1, 1, 1))

        redis_client = redis.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
        )
        try:
            key = "banxia:monitor:snapshot:2099-01-03"
            cached = json.loads(redis_client.get(key))
            ttl = redis_client.ttl(key)
        finally:
            redis_client.close()
        self.assertEqual(cached["stocks"][0]["advice"]["state"], "watch")
        self.assertGreater(ttl, 0)


if __name__ == "__main__":
    unittest.main()
