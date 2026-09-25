from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from banxia_strategy.adapters.postgres import IDENTITY_NAMESPACE
from banxia_strategy.application.persistence import (
    build_market_persistence,
    persist_report_copy,
)
from banxia_strategy.storage_config import StorageSettings


TEST_POSTGRES_DSN = os.environ.get("BANXIA_TEST_POSTGRES_DSN")
RUN_INTEGRATION = (
    os.environ.get("BANXIA_RUN_INTEGRATION") == "1"
    and bool(TEST_POSTGRES_DSN)
)


@unittest.skipUnless(
    RUN_INTEGRATION,
    "set BANXIA_RUN_INTEGRATION=1 and BANXIA_TEST_POSTGRES_DSN",
)
class StorageIntegrationTest(unittest.TestCase):
    strategy_code = "storage-integration-test"
    strategy_version = "integration-v1"
    as_of = "2099-01-02"
    plan_date = "2099-01-03"
    strategy_version_id = str(
        uuid.uuid5(
            IDENTITY_NAMESPACE,
            f"strategy-version:{strategy_code}:{strategy_version}",
        )
    )
    plan_id = str(
        uuid.uuid5(
            IDENTITY_NAMESPACE,
            f"strategy-plan:{strategy_version_id}:{as_of}",
        )
    )

    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict

        database = conninfo_to_dict(TEST_POSTGRES_DSN).get("dbname", "")
        if "test" not in database.lower():
            raise RuntimeError(
                "BANXIA_TEST_POSTGRES_DSN must name an isolated test database"
            )
        cls.settings = StorageSettings.from_env(
            {
                **os.environ,
                "BANXIA_STORAGE_MODE": "required",
                "BANXIA_POSTGRES_DSN": TEST_POSTGRES_DSN,
                "BANXIA_STRATEGY_VERSION": cls.strategy_version,
                "BANXIA_CODE_COMMIT": "integration-test",
            }
        )
        cls.report = {
            "strategy_id": "00000000-0000-0000-0000-000000000099",
            "strategy_code": cls.strategy_code,
            "strategy_name": "存储集成测试策略",
            "as_of": cls.as_of,
            "next_session": cls.plan_date,
            "generated_at": "2099-01-02T16:20:00+08:00",
            "data_source": "mootdx",
            "data_sessions": [cls.as_of],
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
        self._cleanup_storage()

    def tearDown(self):
        self._cleanup_storage()

    def _cleanup_storage(self):
        errors = []
        for cleanup in (
            self._cleanup_database,
            self._cleanup_clickhouse,
            self._cleanup_redis,
            self._cleanup_minio,
        ):
            try:
                cleanup()
            except Exception as exc:
                errors.append(f"{cleanup.__name__}: {exc}")
        if errors:
            self.fail("; ".join(errors))

    def _cleanup_database(self):
        import psycopg

        with psycopg.connect(self.settings.postgres_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_day
                    WHERE strategy_id IN (
                        SELECT strategy_id
                        FROM banxia.strategy_definition
                        WHERE code = %s
                    )
                    """,
                    (self.strategy_code,),
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.inbox_event
                    WHERE topic LIKE %s
                    """,
                    (f"%:plan:{self.plan_id}",),
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.outbox_event AS event
                    WHERE event.aggregate_id LIKE %s
                    """,
                    (f"{self.plan_id}:%",),
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
                        JOIN banxia.strategy_definition AS strategy
                          ON strategy.strategy_id = version.strategy_id
                        WHERE strategy.code = %s
                    )
                    """,
                    (self.strategy_code,),
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_plan
                    WHERE strategy_version_id IN (
                        SELECT version.strategy_version_id
                        FROM banxia.strategy_version AS version
                        JOIN banxia.strategy_definition AS strategy
                          ON strategy.strategy_id = version.strategy_id
                        WHERE strategy.code = %s
                    )
                    """,
                    (self.strategy_code,),
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_run
                    WHERE strategy_version_id IN (
                        SELECT version.strategy_version_id
                        FROM banxia.strategy_version AS version
                        JOIN banxia.strategy_definition AS strategy
                          ON strategy.strategy_id = version.strategy_id
                        WHERE strategy.code = %s
                    )
                    """,
                    (self.strategy_code,),
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_version AS version
                    USING banxia.strategy_definition AS strategy
                    WHERE strategy.strategy_id = version.strategy_id
                      AND strategy.code = %s
                    """,
                    (self.strategy_code,),
                )
                cursor.execute(
                    """
                    DELETE FROM banxia.strategy_definition
                    WHERE code = %s
                    """,
                    (self.strategy_code,),
                )

    def _cleanup_clickhouse(self):
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=self.settings.clickhouse_host,
            port=self.settings.clickhouse_port,
            database=self.settings.clickhouse_database,
            username=self.settings.clickhouse_user,
            password=self.settings.clickhouse_password,
        )
        try:
            for table in ("market_quote_snapshot", "market_bar_1m"):
                client.command(
                    f"""
                    ALTER TABLE banxia.{table}
                    DELETE WHERE trade_date = '{self.plan_date}'
                      AND symbol = '600001'
                    SETTINGS mutations_sync = 2
                    """
                )
        finally:
            client.close()

    def _cleanup_redis(self):
        import redis

        client = redis.Redis.from_url(self.settings.redis_url)
        try:
            client.delete(
                f"banxia:monitor:snapshot:{self.plan_date}",
                f"banxia:stream:monitor:{self.plan_date}",
            )
        finally:
            client.close()

    def _cleanup_minio(self):
        from minio import Minio

        client = Minio(
            self.settings.minio_endpoint,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=self.settings.minio_secure,
        )
        prefix = (
            f"strategy_version={self.strategy_version}/"
            f"trade_date={self.as_of}/"
        )
        for item in client.list_objects(
            self.settings.minio_report_bucket,
            prefix=prefix,
            recursive=True,
            include_version=True,
        ):
            client.remove_object(
                self.settings.minio_report_bucket,
                item.object_name,
                version_id=item.version_id,
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
                         WHERE topic LIKE %s),
                        (SELECT count(*) FROM banxia.outbox_event
                         WHERE aggregate_id = %s)
                    """,
                    (
                        report_result.identity.run_id,
                        report_result.identity.plan_id,
                        report_result.identity.run_id,
                        report_result.identity.plan_id,
                        report_result.identity.plan_id,
                        f"%:plan:{report_result.identity.plan_id}",
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
