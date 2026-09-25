from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import date, datetime, time
from typing import Any, Dict, Literal, Optional, Sequence, Union

from .adapters.clickhouse import ClickHouseMarketHistoryStore
from .adapters.kafka import KafkaEventConsumer, KafkaEventPublisher
from .adapters.lease import PostgresAdvisoryLease
from .adapters.minio import MinioObjectAssetStore
from .adapters.postgres import PostgresStorage, STRATEGY_CODE, _uuid
from .adapters.strategy_catalog import CatalogConfigStore, SharedCollectionConfig
from .adapters.redis import RedisSnapshotCache
from .adapters.wal import SQLiteEventWAL
from .api import ApiServices, create_api_app
from .catalog_bootstrap import ensure_initial_catalog
from .application.collector import MarketCollector
from .application.features import FeatureWorker
from .application.market_sink import MarketSinkWorker
from .application.outbox import OutboxRelay
from .application.plans import ActivePlan, load_active_plan
from .application.projection import ProjectionWorker
from .application.reliable_publish import ReliableEventPublisher
from .application.report_scheduler import (
    SHANGHAI,
    next_scheduled_at,
    parse_schedule,
    recent_scheduled_slots,
    report_is_fresh,
)
from .application.report_worker import NonTradingDayError, ReportWorker
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
from .ports.storage import ReportIdentity
from .strategy_config import StrategyConfig, StrategyConfigStore, revision_for, version_for
from .strategy_history import materialize_strategy_history


ReportRunResult = Union[Dict[str, Any], Literal[False]]


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
    if isinstance(repository, PostgresStorage):
        day = repository.get_strategy_day(_uuid(f"strategy:{STRATEGY_CODE}"), plan.report["as_of"])
        saved = day.get("next_plan") if day else None
        if saved and saved.get("generated_at", "") >= plan.report.get("generated_at", ""):
            plan = ActivePlan(report=saved, candidates=tuple(
                {**item, "plan_date": saved["next_session"], "reference_date": saved["as_of"]}
                for item in plan.candidates
            ))
    existing = repository.get_active_plan(plan.report.get("next_session"))
    if (
        existing and existing["reference_date"] == plan.report["as_of"]
        and (not plan.report.get("strategy_version") or existing["strategy_version"] == plan.report["strategy_version"])
    ):
        return ActivePlan(
            report={**plan.report, "strategy_version": existing["strategy_version"]},
            candidates=plan.candidates,
        ), ReportIdentity(
            run_id=existing["run_id"], plan_id=existing["plan_id"],
            strategy_version_id=existing["strategy_version_id"],
        )
    from dataclasses import asdict
    strategy_config = plan.report.get("strategy_config") or asdict(StrategyConfig())
    commit = plan.report.get("code_commit") or settings.storage.code_commit
    version = plan.report.get("strategy_version") or version_for(strategy_config, settings.storage.strategy_version, commit)
    identity = repository.persist_report(
        plan.report,
        strategy_version=version,
        strategy_config=strategy_config,
        code_commit=commit,
    )
    if identity.plan_id is None:
        raise RuntimeError("active report did not produce a strategy plan")
    repository.persist_watchlist(identity.plan_id, plan.candidates)
    return ActivePlan(report={**plan.report, "strategy_version": version}, candidates=plan.candidates), identity


def _config_store(settings: RuntimeSettings):
    return StrategyConfigStore(settings.strategy_config_path, {
        "report_schedule": ",".join(settings.report_schedule),
        "quote_interval_seconds": settings.quote_interval_seconds,
        "idle_interval_seconds": settings.idle_interval_seconds,
    })


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
    repository = _postgres(settings)
    def candidates():
        return [{**item, "code": item["symbol"], "plan_date": current["trade_date"]}
                for current in repository.list_monitor_plans() for item in current["candidates"]]
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
        config_store=SharedCollectionConfig(repository, _config_store(settings)),
        candidate_loader=candidates,
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
        repository.close()
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
    processor = StrategyEventProcessor(
        repository=repository,
        plan_id="",
        strategy_version_id="",
        strategy_version="",
        candidates=(),
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
        plans_loader=repository.list_monitor_plans,
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


def _generate_report(
    settings: RuntimeSettings,
    logger: Any,
    requested_date: date,
    strategy_id: Optional[str] = None,
) -> ReportRunResult:
    worker = ReportWorker(
        strategy_config_path=settings.strategy_config_path,
        output_dir=settings.report_output_dir,
        storage_settings=settings.storage,
        **({"strategy_id": strategy_id} if strategy_id else {}),
    )
    try:
        report, paths, persistence = worker.run(requested_date)
    except NonTradingDayError as exc:
        logger.info(
            "report skipped",
            extra={
                "trade_date": requested_date.isoformat(),
                "reason": str(exc),
            },
        )
        return False
    logger.info(
        "report generated",
        extra={
            "trade_date": report.as_of,
            "plan_date": report.next_session,
            "candidate_count": len(report.candidates),
            "run_id": persistence.identity.run_id,
            "strategy_version": report.strategy_version,
            "strategy_revision": revision_for(report.strategy_config),
            "assets": [str(path) for path in paths.values()],
        },
    )
    return {
        "trade_date": report.as_of,
        "strategy_id": report.strategy_id,
        "generated_at": report.generated_at,
        "run_id": persistence.identity.run_id,
        "strategy_version": report.strategy_version,
        "strategy_revision": revision_for(report.strategy_config),
    }


def run_report_worker(settings: RuntimeSettings, logger: Any) -> None:
    requested_date = (
        date.fromisoformat(settings.report_date)
        if settings.report_date
        else datetime.now(SHANGHAI).date()
    )
    _generate_report(settings, logger, requested_date)


def _generate_scheduled_report(
    settings: RuntimeSettings,
    logger: Any,
    requested_date: date,
    scheduled_at: datetime,
    stop: threading.Event,
    retry_delays: Sequence[float] = (5.0, 15.0, 45.0),
    strategy_id: Optional[str] = None,
) -> Optional[ReportRunResult]:
    for attempt in range(len(retry_delays) + 1):
        try:
            return _generate_report(settings, logger, requested_date,
                                    **({"strategy_id": strategy_id} if strategy_id else {}))
        except Exception:
            if attempt >= len(retry_delays):
                logger.exception(
                    "scheduled report failed",
                    extra={
                        "scheduled_at": scheduled_at.isoformat(),
                        "attempt": attempt + 1,
                    },
                )
                return None
            delay = retry_delays[attempt]
            logger.exception(
                "scheduled report attempt failed; retrying",
                extra={
                    "scheduled_at": scheduled_at.isoformat(),
                    "attempt": attempt + 1,
                    "retry_in_seconds": delay,
                },
            )
            if stop.wait(delay):
                return None
    return None


def _catch_up_report(
    settings: RuntimeSettings,
    logger: Any,
    schedule: Sequence[time],
    now: datetime,
    stop: threading.Event,
) -> None:
    for scheduled_at in recent_scheduled_slots(now, schedule):
        if report_is_fresh(settings.report_output_dir, scheduled_at):
            return
        logger.warning(
            "missed report schedule detected",
            extra={"scheduled_at": scheduled_at.isoformat()},
        )
        result = _generate_scheduled_report(
            settings,
            logger,
            scheduled_at.date(),
            scheduled_at,
            stop,
        )
        if result:
            logger.info(
                "missed report schedule recovered",
                extra={"scheduled_at": scheduled_at.isoformat()},
            )
            return
        if result is None:
            return


def _consume_report_refresh(
    repository: PostgresStorage,
    settings: RuntimeSettings,
    logger: Any,
    stop: threading.Event,
) -> bool:
    job = repository.claim_report_refresh()
    if job is None:
        return False
    job_id = str(job["job_id"])
    try:
        requested_date = date.fromisoformat(str(job["payload"]["trade_date"]))
        requested_at = datetime.now(SHANGHAI)
        strategy_id = job["payload"].get("strategy_id")
        if strategy_id:
            strategy = repository.get_strategy(strategy_id)
            if strategy is None:
                raise ValueError("所选策略不存在或已删除")
        else:
            get_active = getattr(repository, "get_active_strategy", None)
            strategy = get_active() if get_active else None
            if get_active and strategy is None:
                raise ValueError("当前没有激活策略，请先激活策略后再刷新")
            strategy_id = strategy["strategy_id"] if isinstance(strategy, dict) else None
        result = _generate_scheduled_report(
            settings,
            logger,
            requested_date,
            requested_at,
            stop,
            **({"strategy_id": strategy_id} if strategy_id else {}),
        )
        if result:
            repository.finish_report_refresh(
                job_id,
                succeeded=True,
                result=result,
            )
            logger.info(
                "report refresh completed",
                extra={
                    "job_id": job_id,
                    "trade_date": requested_date.isoformat(),
                },
            )
        else:
            message = (
                "report date is not a trading day"
                if result is False
                else "report generation failed after retries"
            )
            repository.finish_report_refresh(
                job_id,
                succeeded=False,
                error_message=message,
            )
            logger.error(
                "report refresh failed",
                extra={
                    "job_id": job_id,
                    "trade_date": requested_date.isoformat(),
                    "reason": message,
                },
            )
    except Exception as exc:
        logger.exception(
            "report refresh job failed",
            extra={"job_id": job_id},
        )
        repository.finish_report_refresh(
            job_id,
            succeeded=False,
            error_message=str(exc),
        )
    return True


def _consume_strategy_history(
    repository: PostgresStorage,
    settings: RuntimeSettings,
    logger: Any,
) -> bool:
    job = repository.claim_strategy_history()
    if job is None:
        return False
    job_id = job["job_id"]
    payload = job["payload"]
    try:
        strategy = repository.get_strategy(payload["strategy_id"])
        if strategy is None:
            raise ValueError("策略不存在或已删除")
        result = materialize_strategy_history(
            repository,
            strategy,
            history_range=payload["history_range"],
            start=date.fromisoformat(payload["start"]),
            end=date.fromisoformat(payload["end"]),
            output_root=settings.report_output_dir,
            commit=settings.storage.code_commit,
        )
        repository.finish_strategy_history(
            job_id,
            succeeded=True,
            result=result,
        )
        logger.info(
            "strategy history generated",
            extra={
                "job_id": job_id,
                "strategy_id": strategy["strategy_id"],
                "history_range": payload["history_range"],
            },
        )
    except Exception as exc:
        logger.exception(
            "strategy history generation failed",
            extra={"job_id": job_id},
        )
        repository.finish_strategy_history(
            job_id,
            succeeded=False,
            error_message=str(exc),
        )
    return True


def run_report_scheduler(settings: RuntimeSettings, logger: Any) -> None:
    stop = threading.Event()
    repository = _postgres(settings)
    def request_stop(_signum, _frame):
        stop.set()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    calendar_date = None
    calendar_attempt = 0
    checked = {}
    logger.info("multi-strategy report scheduler started")
    try:
        ensure_initial_catalog(repository, settings.strategy_config_path)
        while not stop.is_set():
            try:
                _consume_report_refresh(repository, settings, logger, stop)
                _consume_strategy_history(repository, settings, logger)
                now = datetime.now(SHANGHAI)
                if calendar_date != now.date() and now.timestamp() - calendar_attempt >= 300:
                    from .mootdx_provider import MootdxProvider
                    calendar_attempt = now.timestamp()
                    try:
                        repository.save_trading_sessions(MootdxProvider().trading_dates())
                        calendar_date = now.date()
                    except Exception:
                        logger.exception("calendar refresh failed; retaining last verified calendar")
                repository.ensure_strategy_days(now.date())
                latest = repository.latest_closed_session(now)
                strategy = repository.get_active_strategy()
                for strategy in ([strategy] if strategy else []):
                    if not latest or stop.is_set():
                        continue
                    sid = strategy["strategy_id"]
                    store = CatalogConfigStore(repository, sid)
                    schedule = parse_schedule(store.read().report_schedule.split(","))
                    due = [datetime.combine(date.fromisoformat(latest), slot, tzinfo=SHANGHAI)
                           for slot in schedule]
                    due = [slot for slot in due if slot <= now]
                    if not due:
                        continue
                    slot = max(due)
                    # Retry failed slots after five minutes, without creating duplicate daily rows.
                    if (now.timestamp() - checked.get((sid, slot), 0)) < 300:
                        continue
                    output = settings.report_output_dir / "strategies" / sid
                    if report_is_fresh(output, slot):
                        continue
                    checked[(sid, slot)] = now.timestamp()
                    _generate_scheduled_report(settings, logger, slot.date(), slot, stop, strategy_id=sid)
            except Exception:
                logger.exception("multi-strategy scheduler iteration failed")
            stop.wait(1.0)
    finally:
        repository.close()
        logger.info("report scheduler stopped")


def run_api(settings: RuntimeSettings, logger: Any) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "API service requires `pip install -e '.[production]'`"
        ) from exc
    repository = _postgres(settings)
    initial, history_job = ensure_initial_catalog(
        repository,
        settings.strategy_config_path,
    )
    logger.info(
        "initial strategy ready",
        extra={
            "strategy_id": initial["strategy_id"],
            "history_job_id": history_job["job_id"],
            "history_job_status": history_job["status"],
        },
    )
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
        strategy_version=settings.storage.strategy_version,
        config_store=_config_store(settings),
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
    "report-scheduler": run_report_scheduler,
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
