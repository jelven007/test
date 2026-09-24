from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from datetime import date
from typing import Any, Optional, Sequence

from .adapters.clickhouse import ClickHouseMarketHistoryStore
from .adapters.kafka import KafkaEventConsumer, KafkaEventPublisher
from .adapters.lease import PostgresAdvisoryLease
from .adapters.minio import MinioObjectAssetStore
from .adapters.postgres import PostgresStorage
from .adapters.redis import RedisSnapshotCache
from .adapters.wal import SQLiteEventWAL
from .api import ApiServices, create_api_app
from .application.collector import MarketCollector
from .application.features import FeatureWorker
from .application.market_sink import MarketSinkWorker
from .application.outbox import OutboxRelay
from .application.plans import ActivePlan, load_active_plan
from .application.projection import ProjectionWorker
from .application.reliable_publish import ReliableEventPublisher
from .application.report_worker import ReportWorker
from .application.strategy_engine import StrategyEventProcessor, StrategyWorker
from .config import RuntimeSettings
from .contracts.topics import (
    MARKET_BAR_1M,
    MARKET_FEATURE_REALTIME,
    MARKET_QUOTE_SNAPSHOT,
    STRATEGY_DECISION,
    STRATEGY_PLAN_CREATED,
)
from .observability import configure_logging, start_metrics_server
from .web_server import ReportStore


def _postgres(settings: RuntimeSettings) -> PostgresStorage:
    return PostgresStorage(settings.storage.postgres_dsn)


def _clickhouse(settings: RuntimeSettings) -> ClickHouseMarketHistoryStore:
    storage = settings.storage
    return ClickHouseMarketHistoryStore(
        host=storage.clickhouse_host,
        port=storage.clickhouse_port,
        database=storage.clickhouse_database,
        username=storage.clickhouse_user,
        password=storage.clickhouse_password,
    )


def _redis(settings: RuntimeSettings) -> RedisSnapshotCache:
    return RedisSnapshotCache(settings.storage.redis_url)


def _minio(settings: RuntimeSettings) -> MinioObjectAssetStore:
    storage = settings.storage
    return MinioObjectAssetStore(
        storage.minio_endpoint,
        storage.minio_access_key,
        storage.minio_secret_key,
        bucket=storage.minio_report_bucket,
        secure=storage.minio_secure,
    )


def _publisher(settings: RuntimeSettings, service_name: str) -> KafkaEventPublisher:
    return KafkaEventPublisher(
        settings.kafka_bootstrap_servers,
        client_id=service_name,
    )


def _consumer(
    settings: RuntimeSettings,
    service_name: str,
    topics: Sequence[str],
) -> KafkaEventConsumer:
    return KafkaEventConsumer(
        settings.kafka_bootstrap_servers,
        group_id=f"{settings.environment}.{service_name}",
        topics=[settings.topic(topic) for topic in topics],
    )


def _load_plan(settings: RuntimeSettings) -> ActivePlan:
    return load_active_plan(
        settings.report_dirs,
        watch_date=settings.watch_date,
        watchlist_path=settings.watchlist_path,
    )


def _prepare_plan(repository: PostgresStorage, settings: RuntimeSettings) -> tuple[ActivePlan, Any]:
    plan = _load_plan(settings)
    strategy_config = json.loads(
        settings.strategy_config_path.read_text(encoding="utf-8")
    )
    identity = repository.persist_report(
        plan.report,
        strategy_version=settings.storage.strategy_version,
        strategy_config=strategy_config,
        code_commit=settings.storage.code_commit,
    )
    if identity.plan_id is None:
        raise RuntimeError("active report did not produce a strategy plan")
    repository.persist_watchlist(identity.plan_id, plan.candidates)
    return plan, identity


def _run_polling_worker(worker: Any, logger: Any) -> None:
    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("service started")
    try:
        while not stop.is_set():
            try:
                worker.run_once(timeout=1.0)
            except Exception:
                logger.exception("worker iteration failed")
                stop.wait(1.0)
    finally:
        worker.close()
        logger.info("service stopped")


def run_collector(settings: RuntimeSettings, logger: Any) -> None:
    plan = _load_plan(settings)
    reliable = ReliableEventPublisher(
        wal=SQLiteEventWAL(
            settings.wal_path,
            max_bytes=settings.wal_max_bytes,
        ),
        publisher=_publisher(settings, "market-collector"),
    )
    collector = MarketCollector(
        candidates=plan.candidates,
        publisher=reliable,
        lease=PostgresAdvisoryLease(
            settings.storage.postgres_dsn,
            lease_key=f"{settings.environment}:market-collector:shard-0",
        ),
        quote_interval_seconds=settings.quote_interval_seconds,
        idle_interval_seconds=settings.idle_interval_seconds,
    )

    def request_stop(_signum, _frame):
        collector.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("collector started")
    try:
        collector.run_forever()
    finally:
        collector.close()
        logger.info("collector stopped")


def run_market_sink(settings: RuntimeSettings, logger: Any) -> None:
    worker = MarketSinkWorker(
        consumer=_consumer(
            settings,
            "market-sink",
            (MARKET_QUOTE_SNAPSHOT, MARKET_BAR_1M, MARKET_FEATURE_REALTIME),
        ),
        store=_clickhouse(settings),
    )
    _run_polling_worker(worker, logger)


def run_feature_worker(settings: RuntimeSettings, logger: Any) -> None:
    worker = FeatureWorker(
        consumer=_consumer(
            settings,
            "feature-worker",
            (MARKET_QUOTE_SNAPSHOT, MARKET_BAR_1M),
        ),
        publisher=_publisher(settings, "feature-worker"),
    )
    _run_polling_worker(worker, logger)


def run_strategy_engine(settings: RuntimeSettings, logger: Any) -> None:
    repository = _postgres(settings)
    plan, identity = _prepare_plan(repository, settings)
    processor = StrategyEventProcessor(
        repository=repository,
        plan_id=identity.plan_id,
        strategy_version_id=identity.strategy_version_id,
        strategy_version=settings.storage.strategy_version,
        candidates=plan.candidates,
    )
    worker = StrategyWorker(
        consumer=_consumer(
            settings,
            "strategy-engine",
            (
                MARKET_QUOTE_SNAPSHOT,
                MARKET_FEATURE_REALTIME,
                STRATEGY_PLAN_CREATED,
            ),
        ),
        processor=processor,
        plan_loader=repository.get_active_plan,
    )
    _run_polling_worker(worker, logger)


def run_outbox_relay(settings: RuntimeSettings, logger: Any) -> None:
    relay = OutboxRelay(
        repository=_postgres(settings),
        publisher=_publisher(settings, "outbox-relay"),
    )

    def request_stop(_signum, _frame):
        relay.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("outbox relay started")
    try:
        relay.run_forever()
    finally:
        relay.close()
        logger.info("outbox relay stopped")


def run_projection_worker(settings: RuntimeSettings, logger: Any) -> None:
    worker = ProjectionWorker(
        consumer=_consumer(
            settings,
            "projection-worker",
            (
                MARKET_QUOTE_SNAPSHOT,
                MARKET_BAR_1M,
                MARKET_FEATURE_REALTIME,
                STRATEGY_DECISION,
            ),
        ),
        cache=_redis(settings),
    )
    _run_polling_worker(worker, logger)


def run_report_worker(settings: RuntimeSettings, logger: Any) -> None:
    requested_date = (
        date.fromisoformat(settings.report_date)
        if settings.report_date
        else None
    )
    worker = ReportWorker(
        strategy_config_path=settings.strategy_config_path,
        output_dir=settings.report_output_dir,
        storage_settings=settings.storage,
    )
    report, paths, persistence = worker.run(requested_date)
    logger.info(
        "report generated",
        extra={
            "trade_date": report.as_of,
            "plan_date": report.next_session,
            "candidate_count": len(report.candidates),
            "run_id": persistence.identity.run_id,
            "assets": [str(path) for path in paths.values()],
        },
    )


def run_api(settings: RuntimeSettings, logger: Any) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "API service requires `pip install -e '.[production]'`"
        ) from exc
    repository = _postgres(settings)
    _prepare_plan(repository, settings)
    cache = _redis(settings)
    object_store = _minio(settings)
    kafka = _publisher(settings, "api-readiness")
    clickhouse = _clickhouse(settings)
    services = ApiServices(
        reports=ReportStore(settings.report_dirs),
        repository=repository,
        cache=cache,
        object_store=object_store,
        api_token=settings.api_token,
        kafka_ready=kafka.ready,
        clickhouse_ready=clickhouse.ready,
    )
    app = create_api_app(services)
    logger.info("api starting")
    try:
        uvicorn.run(
            app,
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
        )
    finally:
        for target in (kafka, clickhouse, object_store, cache, repository):
            target.close()


RUNNERS = {
    "market-collector": run_collector,
    "market-sink": run_market_sink,
    "feature-worker": run_feature_worker,
    "strategy-engine": run_strategy_engine,
    "outbox-relay": run_outbox_relay,
    "projection-worker": run_projection_worker,
    "report-worker": run_report_worker,
    "api": run_api,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="banxia-service")
    parser.add_argument("service", choices=sorted(RUNNERS))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = RuntimeSettings.from_env(service_name=args.service)
    logger = configure_logging(args.service, settings.log_level)
    try:
        if settings.metrics_port is not None:
            start_metrics_server(settings.metrics_port)
            logger.info(
                "metrics server started",
                extra={"metrics_port": settings.metrics_port},
            )
        RUNNERS[args.service](settings, logger)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
