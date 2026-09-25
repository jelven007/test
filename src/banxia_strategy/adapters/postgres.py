from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional

from ..contracts.topics import REPORT_GENERATED, STRATEGY_PLAN_CREATED
from ..domain.events import EventEnvelope
from ..ports.storage import (
    DecisionRecord,
    OutboxRecord,
    ReportAsset,
    ReportIdentity,
)
from .strategy_catalog import INITIAL_STRATEGY_CODE, StrategyCatalogMixin


IDENTITY_NAMESPACE = uuid.UUID("4b6067a1-05ca-4eaf-9c59-ed125f79cb45")
STRATEGY_CODE = INITIAL_STRATEGY_CODE


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _uuid(name: str) -> str:
    return str(uuid.uuid5(IDENTITY_NAMESPACE, name))


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value


class PostgresStorage(StrategyCatalogMixin):
    """Transactional report catalog and decision repository."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        connection_factory: Optional[Callable[[], Any]] = None,
    ):
        self._pool = None
        if connection_factory is None:
            if not dsn:
                raise ValueError("dsn is required")
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "PostgreSQL adapter requires `pip install -e '.[storage]'`"
                ) from exc
            self._pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=4,
                open=True,
                kwargs={"connect_timeout": 5},
            )
            connection_factory = self._pool.connection
        self.connection_factory = connection_factory

    def persist_report(
        self,
        report: Mapping[str, Any],
        *,
        strategy_version: str,
        strategy_config: Mapping[str, Any],
        code_commit: str,
        assets: Iterable[ReportAsset] = (),
        enqueue_events: bool = False,
    ) -> ReportIdentity:
        if not strategy_version:
            raise ValueError("strategy_version is required")
        trade_date = str(report["as_of"])
        generated_at = _datetime(report["generated_at"])
        strategy_code = report.get("strategy_code") or STRATEGY_CODE
        strategy_id_seed = report.get("strategy_id") or _uuid(f"strategy:{strategy_code}")
        version_id_seed = _uuid(
            f"strategy-version:{strategy_code}:{strategy_version}"
        )
        run_id_seed = _uuid(f"strategy-run:{version_id_seed}:{trade_date}")
        plan_id_seed = _uuid(f"strategy-plan:{version_id_seed}:{trade_date}")
        assets = tuple(assets)

        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.strategy_definition (
                        strategy_id, code, name, description
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code
                    RETURNING strategy_id
                    """,
                    (
                        strategy_id_seed,
                        strategy_code,
                        report.get("strategy_name") or "首板晋级二板策略",
                        "基于 mootdx 的沪深主板一进二条件筛选与盘中监控",
                    ),
                )
                strategy_id = str(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO banxia.strategy_version AS current (
                        strategy_version_id, strategy_id, version, config,
                        code_commit, effective_from, created_by, change_note
                    ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (strategy_id, version) DO UPDATE SET
                        version = current.version
                    WHERE
                        current.config = EXCLUDED.config
                        AND current.code_commit = EXCLUDED.code_commit
                    RETURNING strategy_version_id
                    """,
                    (
                        version_id_seed,
                        strategy_id,
                        strategy_version,
                        _json(strategy_config),
                        code_commit,
                        trade_date,
                        "banxia-strategy",
                        "Adapter-driven dual-write persistence",
                    ),
                )
                version_row = cursor.fetchone()
                if version_row is None:
                    raise ValueError(
                        "strategy version already exists with different config or commit"
                    )
                strategy_version_id = str(version_row[0])
                metadata = {
                    "data_source": report.get("data_source"),
                    "data_sessions": report.get("data_sessions", []),
                    "market": report.get("market", {}),
                    "rejected_count": report.get("rejected_count", 0),
                }
                cursor.execute(
                    """
                    INSERT INTO banxia.strategy_run (
                        run_id, trade_date, strategy_version_id, status,
                        requested_at, started_at, finished_at,
                        input_start_at, input_end_at, metadata
                    ) VALUES (
                        %s, %s, %s, 'succeeded', %s, %s, %s, %s, %s,
                        %s::jsonb
                    )
                    ON CONFLICT (trade_date, strategy_version_id) DO UPDATE SET
                        status = 'succeeded',
                        finished_at = EXCLUDED.finished_at,
                        input_start_at = EXCLUDED.input_start_at,
                        input_end_at = EXCLUDED.input_end_at,
                        error_code = NULL,
                        error_message = NULL,
                        metadata = EXCLUDED.metadata
                    RETURNING run_id
                    """,
                    (
                        run_id_seed,
                        trade_date,
                        strategy_version_id,
                        generated_at,
                        generated_at,
                        generated_at,
                        generated_at,
                        generated_at,
                        _json(metadata),
                    ),
                )
                run_id = str(cursor.fetchone()[0])
                plan_id: Optional[str] = None
                if report.get("next_session"):
                    cursor.execute(
                        """
                        INSERT INTO banxia.strategy_plan (
                            plan_id, run_id, reference_date, trade_date,
                            strategy_version_id, status
                        ) VALUES (%s, %s, %s, %s, %s, 'active')
                        ON CONFLICT (run_id) DO UPDATE SET
                            reference_date = EXCLUDED.reference_date,
                            trade_date = EXCLUDED.trade_date,
                            strategy_version_id = EXCLUDED.strategy_version_id,
                            status = 'active'
                        RETURNING plan_id
                        """,
                        (
                            plan_id_seed,
                            run_id,
                            trade_date,
                            str(report["next_session"]),
                            strategy_version_id,
                        ),
                    )
                    plan_id = str(cursor.fetchone()[0])
                    cursor.execute(
                        """
                        UPDATE banxia.strategy_plan
                        SET status = 'expired'
                        WHERE trade_date = %s
                          AND plan_id <> %s
                          AND status = 'active'
                          AND strategy_version_id IN (
                            SELECT strategy_version_id FROM banxia.strategy_version WHERE strategy_id=%s
                          )
                        """,
                        (str(report["next_session"]), plan_id, strategy_id),
                    )
                    cursor.execute(
                        "DELETE FROM banxia.candidate WHERE plan_id = %s",
                        (plan_id,),
                    )
                    for candidate in report.get("candidates", []):
                        cursor.execute(
                            """
                            INSERT INTO banxia.candidate (
                                plan_id, symbol, name, industry, rank, score,
                                strategy, latest_price, amount_cny,
                                turnover_pct, float_market_cap_cny, reasons,
                                entry_trigger, invalidation, exit_plan,
                                position_limit_pct
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s::jsonb, %s, %s, %s, %s
                            )
                            """,
                            (
                                plan_id,
                                candidate["code"],
                                candidate["name"],
                                candidate.get("industry"),
                                candidate["rank"],
                                candidate["score"],
                                candidate["strategy"],
                                candidate["latest_price"],
                                candidate["amount_cny"],
                                candidate.get("turnover_pct"),
                                candidate.get("float_market_cap_cny"),
                                _json(candidate.get("reasons", [])),
                                candidate["entry_trigger"],
                                candidate["invalidation"],
                                candidate["exit_plan"],
                                candidate["position_limit_pct"],
                            ),
                        )
                self._save_daily_report(cursor, strategy_id, {**report, "plan_id": plan_id})
                for asset in assets:
                    cursor.execute(
                        """
                        INSERT INTO banxia.report_asset (
                            run_id, format, object_key, content_hash,
                            content_type, size_bytes
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (run_id, format, content_hash) DO NOTHING
                        """,
                        (
                            run_id,
                            asset.format,
                            asset.object_key,
                            asset.content_hash,
                            asset.content_type,
                            asset.size_bytes,
                        ),
                    )
                if enqueue_events:
                    if plan_id is not None:
                        self._persist_watchlist(cursor, plan_id, report.get("candidates", []))
                    self._enqueue_report_events(
                        cursor,
                        report=report,
                        identity=ReportIdentity(
                            run_id=run_id,
                            strategy_version_id=strategy_version_id,
                            plan_id=plan_id,
                        ),
                        strategy_version=strategy_version,
                        assets=assets,
                        occurred_at=generated_at,
                    )
        return ReportIdentity(
            run_id=run_id,
            strategy_version_id=strategy_version_id,
            plan_id=plan_id,
        )

    @staticmethod
    def _insert_outbox_event(
        cursor: Any,
        *,
        aggregate_type: str,
        aggregate_id: str,
        topic: str,
        message_key: str,
        event: EventEnvelope,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO banxia.outbox_event (
                event_id, aggregate_type, aggregate_id, event_type, topic,
                message_key, payload, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event.event_id,
                aggregate_type,
                aggregate_id,
                event.event_type,
                topic,
                message_key,
                _json(event.to_dict()),
                _datetime(event.occurred_at),
            ),
        )

    def _enqueue_report_events(
        self,
        cursor: Any,
        *,
        report: Mapping[str, Any],
        identity: ReportIdentity,
        strategy_version: str,
        assets: Iterable[ReportAsset],
        occurred_at: datetime,
    ) -> None:
        if identity.plan_id is not None:
            plan_event = EventEnvelope.create(
                event_type=STRATEGY_PLAN_CREATED,
                producer="report-worker",
                occurred_at=occurred_at,
                identity={
                    "plan_id": identity.plan_id,
                    "strategy_version": strategy_version,
                    "generated_at": occurred_at,
                },
                payload={
                    "plan_id": identity.plan_id,
                    "reference_date": str(report["as_of"]),
                    "trade_date": str(report["next_session"]),
                    "strategy_version": strategy_version,
                    "symbols": [
                        str(candidate["code"])
                        for candidate in report.get("candidates", [])
                    ],
                },
            )
            self._insert_outbox_event(
                cursor,
                aggregate_type="strategy_plan",
                aggregate_id=identity.plan_id,
                topic=STRATEGY_PLAN_CREATED,
                message_key=identity.plan_id,
                event=plan_event,
            )
        report_event = EventEnvelope.create(
            event_type=REPORT_GENERATED,
            producer="report-worker",
            occurred_at=occurred_at,
            identity={
                "strategy_run_id": identity.run_id,
                "strategy_version": strategy_version,
                "generated_at": occurred_at,
            },
            payload={
                "report_id": identity.run_id,
                "strategy_run_id": identity.run_id,
                "trade_date": str(report["as_of"]),
                "strategy_version": strategy_version,
                "assets": [
                    {
                        "format": asset.format,
                        "object_key": asset.object_key,
                        "content_hash": asset.content_hash,
                    }
                    for asset in assets
                ],
            },
        )
        self._insert_outbox_event(
            cursor,
            aggregate_type="strategy_run",
            aggregate_id=identity.run_id,
            topic=REPORT_GENERATED,
            message_key=identity.run_id,
            event=report_event,
        )

    def get(self, plan_id: str, symbol: str) -> Optional[DecisionRecord]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        plan_id, symbol, state, reason_code, reason_text,
                        source_event_id, strategy_version_id, irreversible,
                        updated_at, version
                    FROM banxia.decision_state
                    WHERE plan_id = %s AND symbol = %s
                    """,
                    (plan_id, symbol),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return DecisionRecord(
            plan_id=str(row[0]),
            symbol=str(row[1]),
            state=str(row[2]),
            reason_code=str(row[3]),
            reason=str(row[4]),
            source_event_id=str(row[5]),
            strategy_version=str(row[6]),
            irreversible=bool(row[7]),
            occurred_at=_datetime(row[8]),
            version=int(row[9]),
        )

    def persist_watchlist(
        self,
        plan_id: str,
        candidates: Iterable[Mapping[str, Any]],
    ) -> str:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                return self._persist_watchlist(cursor, plan_id, candidates)

    @staticmethod
    def _persist_watchlist(cursor, plan_id, candidates):
        from ..intraday import plan_for

        watchlist_id = _uuid(f"watchlist:{plan_id}")
        cursor.execute(
            """
            INSERT INTO banxia.watchlist (watchlist_id, plan_id, source)
            VALUES (%s, %s, 'daily_strategy_and_supplement')
            ON CONFLICT (plan_id) DO UPDATE SET source = EXCLUDED.source
            RETURNING watchlist_id
            """,
            (watchlist_id, plan_id),
        )
        watchlist_id = str(cursor.fetchone()[0])
        cursor.execute("DELETE FROM banxia.watchlist_item WHERE watchlist_id = %s", (watchlist_id,))
        for index, candidate in enumerate(candidates):
            plan = plan_for(candidate)
            cursor.execute(
                """
                INSERT INTO banxia.watchlist_item (
                    watchlist_id, symbol, name, industry, origin,
                    display_order, eligible, eligibility_reason,
                    reference_close, entry_rules, invalidation_rules
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (
                    watchlist_id, str(candidate["code"]), str(candidate["name"]),
                    candidate.get("industry"), str(candidate.get("origin") or "report"),
                    index, bool(plan["eligible"]), str(plan["eligibility_reason"]),
                    plan["previous_close"], _json(plan),
                    _json({key: plan[key] for key in ("reject_min_pct", "reject_max_pct", "invalidation")}),
                ),
            )
        return watchlist_id

    def apply(
        self,
        *,
        input_event_id: str,
        decision: DecisionRecord,
        outbox_event: EventEnvelope,
        topic: Optional[str] = None,
        partition: int = 0,
        offset: Optional[int] = None,
    ) -> bool:
        offset_value = (
            int(hashlib.sha256(input_event_id.encode()).hexdigest()[:15], 16)
            if offset is None
            else offset
        )
        input_event_type = str(
            outbox_event.payload.get("source_event_type") or "unknown"
        )
        input_topic = f"{topic or f'direct:{input_event_type}'}:plan:{decision.plan_id}"
        scoped_event_id = _uuid(f"inbox:{decision.plan_id}:{input_event_id}")
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.inbox_event (
                        event_id, event_type, topic, partition_id, offset_value
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        scoped_event_id,
                        input_event_type,
                        input_topic,
                        partition,
                        offset_value,
                    ),
                )
                if cursor.fetchone() is None:
                    return False
                cursor.execute(
                    """
                    SELECT state, irreversible, version, first_entered_at
                    FROM banxia.decision_state
                    WHERE plan_id = %s AND symbol = %s
                    FOR UPDATE
                    """,
                    (decision.plan_id, decision.symbol),
                )
                current = cursor.fetchone()
                if current is not None:
                    if int(current[2]) >= decision.version:
                        return False
                    if bool(current[1]) and str(current[0]) != decision.state:
                        return False
                    first_entered_at = current[3]
                else:
                    first_entered_at = decision.occurred_at
                cursor.execute(
                    """
                    INSERT INTO banxia.decision_state (
                        plan_id, symbol, state, reason_code, reason_text,
                        source_event_id, strategy_version_id, first_entered_at,
                        updated_at, version, irreversible
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (plan_id, symbol) DO UPDATE SET
                        state = EXCLUDED.state,
                        reason_code = EXCLUDED.reason_code,
                        reason_text = EXCLUDED.reason_text,
                        source_event_id = EXCLUDED.source_event_id,
                        strategy_version_id = EXCLUDED.strategy_version_id,
                        updated_at = EXCLUDED.updated_at,
                        version = EXCLUDED.version,
                        irreversible = EXCLUDED.irreversible
                    """,
                    (
                        decision.plan_id,
                        decision.symbol,
                        decision.state,
                        decision.reason_code,
                        decision.reason,
                        decision.source_event_id,
                        decision.strategy_version,
                        first_entered_at,
                        decision.occurred_at,
                        decision.version,
                        decision.irreversible,
                    ),
                )
                previous_state = str(current[0]) if current is not None else None
                cursor.execute(
                    """
                    INSERT INTO banxia.decision_event (
                        decision_event_id, plan_id, symbol, previous_state,
                        state, reason_code, reason_text, source_event_id,
                        strategy_version_id, rule_inputs, occurred_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                    )
                    """,
                    (
                        outbox_event.event_id,
                        decision.plan_id,
                        decision.symbol,
                        previous_state,
                        decision.state,
                        decision.reason_code,
                        decision.reason,
                        decision.source_event_id,
                        decision.strategy_version,
                        _json(outbox_event.payload.get("rule_inputs", {})),
                        decision.occurred_at,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO banxia.outbox_event (
                        event_id, aggregate_type, aggregate_id, event_type, topic,
                        message_key, payload, occurred_at
                    ) VALUES (
                        %s, 'decision', %s, %s, 'strategy.decision.v1',
                        %s, %s::jsonb, %s
                    )
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (
                        outbox_event.event_id,
                        f"{decision.plan_id}:{decision.symbol}",
                        outbox_event.event_type,
                        f"{decision.plan_id}:{decision.symbol}",
                        _json(outbox_event.to_dict()),
                        decision.occurred_at,
                    ),
                )
                cursor.execute(
                    """UPDATE banxia.strategy_day d SET actuals =
                       jsonb_set(jsonb_set(d.actuals,'{status}','"live"'::jsonb),
                         ARRAY['stocks'], COALESCE(d.actuals->'stocks','{}'::jsonb) || %s::jsonb),
                       updated_at=now()
                       FROM banxia.strategy_plan p JOIN banxia.strategy_version v USING(strategy_version_id)
                       WHERE p.plan_id=%s AND d.strategy_id=v.strategy_id AND d.trade_date=p.trade_date
                       AND d.execution_plan->>'plan_id'=%s
                       AND d.trade_date=%s AND COALESCE(d.actuals->>'status','') <> 'complete'""",
                    (_json({decision.symbol: outbox_event.payload}), decision.plan_id,
                     decision.plan_id, decision.occurred_at.date()),
                )
        return True

    def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> tuple[OutboxRecord, ...]:
        if not worker_id:
            raise ValueError("worker_id is required")
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("limit and lease_seconds must be positive")
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH available AS (
                        SELECT outbox_id
                        FROM banxia.outbox_event
                        WHERE published_at IS NULL
                          AND (
                              locked_at IS NULL
                              OR locked_at < now() - (%s * interval '1 second')
                          )
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE banxia.outbox_event AS event
                    SET locked_at = now(),
                        locked_by = %s,
                        attempts = event.attempts + 1
                    FROM available
                    WHERE event.outbox_id = available.outbox_id
                    RETURNING
                        event.outbox_id,
                        event.topic,
                        event.message_key,
                        event.payload,
                        event.attempts
                    """,
                    (lease_seconds, limit, worker_id),
                )
                rows = cursor.fetchall()
        return tuple(
            OutboxRecord(
                outbox_id=str(row[0]),
                topic=str(row[1]),
                message_key=str(row[2]),
                event=EventEnvelope.from_dict(
                    json.loads(row[3]) if isinstance(row[3], str) else row[3]
                ),
                attempts=int(row[4]),
            )
            for row in rows
        )

    def mark_outbox_published(self, outbox_id: str, worker_id: str) -> bool:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.outbox_event
                    SET published_at = now(),
                        locked_at = NULL,
                        locked_by = NULL,
                        last_error = NULL
                    WHERE outbox_id = %s
                      AND locked_by = %s
                      AND published_at IS NULL
                    RETURNING outbox_id
                    """,
                    (outbox_id, worker_id),
                )
                return cursor.fetchone() is not None

    def mark_outbox_failed(
        self,
        outbox_id: str,
        worker_id: str,
        error: str,
    ) -> bool:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.outbox_event
                    SET locked_at = NULL,
                        locked_by = NULL,
                        last_error = %s
                    WHERE outbox_id = %s
                      AND locked_by = %s
                      AND published_at IS NULL
                    RETURNING outbox_id
                    """,
                    (error[:2000], outbox_id, worker_id),
                )
                return cursor.fetchone() is not None

    def ready(self) -> bool:
        try:
            with self.connection_factory() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    return cursor.fetchone() == (1,)
        except Exception:
            return False

    def get_active_plan(
        self,
        trade_date: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> Optional[Mapping[str, Any]]:
        params = (strategy_id or _uuid(f"strategy:{STRATEGY_CODE}"),)
        date_filter = ""
        if trade_date is not None:
            date_filter = "AND plan.trade_date = %s"
            params = (*params, trade_date)
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT
                        plan.plan_id,
                        plan.reference_date,
                        plan.trade_date,
                        version.version,
                        plan.strategy_version_id,
                        plan.run_id
                    FROM banxia.strategy_plan AS plan
                    JOIN banxia.strategy_version AS version
                      ON version.strategy_version_id = plan.strategy_version_id
                    WHERE plan.status = 'active'
                      AND version.strategy_id = %s
                      {date_filter}
                    ORDER BY plan.trade_date DESC, plan.created_at DESC
                    LIMIT 1
                    """,
                    params,
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                plan_id = str(row[0])
                cursor.execute(
                    """
                    SELECT
                        item.symbol, item.name, item.industry,
                        item.display_order + 1,
                        candidate.score,
                        item.reference_close,
                        candidate.amount_cny,
                        item.entry_rules,
                        item.invalidation_rules,
                        candidate.exit_plan,
                        item.origin,
                        item.eligible,
                        item.eligibility_reason
                    FROM banxia.watchlist
                    JOIN banxia.watchlist_item AS item
                      ON item.watchlist_id = watchlist.watchlist_id
                    LEFT JOIN banxia.candidate AS candidate
                      ON candidate.plan_id = watchlist.plan_id
                     AND candidate.symbol = item.symbol
                    WHERE watchlist.plan_id = %s
                    ORDER BY item.display_order
                    """,
                    (plan_id,),
                )
                candidates = [
                    {
                        "symbol": str(item[0]),
                        "name": str(item[1]),
                        "industry": item[2],
                        "rank": int(item[3]),
                        "score": float(item[4]) if item[4] is not None else None,
                        "latest_price": float(item[5]),
                        "amount_cny": (
                            float(item[6]) if item[6] is not None else None
                        ),
                        "plan": {
                            "previous_close": float(item[5]),
                            **dict(_mapping(item[7])),
                            **dict(_mapping(item[8])),
                        },
                        "entry_trigger": str(_mapping(item[7])["entry_trigger"]),
                        "invalidation": str(
                            _mapping(item[8])["invalidation"]
                        ),
                        "exit_plan": str(item[9]) if item[9] else None,
                        "position_limit_pct": (
                            int(_mapping(item[7])["position_limit_pct"])
                            if _mapping(item[7]).get("position_limit_pct")
                            is not None
                            else None
                        ),
                        "origin": str(item[10]),
                        "eligible": bool(item[11]),
                        "eligibility_reason": str(item[12]),
                    }
                    for item in cursor.fetchall()
                ]
        return {
            "plan_id": plan_id,
            "reference_date": row[1].isoformat(),
            "trade_date": row[2].isoformat(),
            "strategy_version": str(row[3]),
            "strategy_version_id": str(row[4]),
            "run_id": str(row[5]),
            "candidates": candidates,
        }

    def list_decision_events(
        self,
        *,
        plan_id: Optional[str] = None,
        symbol: Optional[str] = None,
        state: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> tuple[Mapping[str, Any], ...]:
        if limit <= 0 or limit > 201:
            raise ValueError("limit must be between 1 and 201")
        filters = []
        params = []
        for column, value in (
            ("plan_id", plan_id),
            ("symbol", symbol),
            ("state", state),
        ):
            if value is not None:
                filters.append(f"{column} = %s")
                params.append(value)
        if start_time is not None:
            filters.append("occurred_at >= %s")
            params.append(_datetime(start_time))
        if end_time is not None:
            filters.append("occurred_at <= %s")
            params.append(_datetime(end_time))
        if cursor is not None:
            try:
                cursor_time, cursor_id = cursor.rsplit("|", 1)
            except ValueError as exc:
                raise ValueError("invalid decision event cursor") from exc
            filters.append("(occurred_at, decision_event_id) < (%s, %s)")
            params.extend((_datetime(cursor_time), cursor_id))
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(limit)
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT
                        decision_event_id, plan_id, symbol, previous_state,
                        state, reason_code, reason_text, source_event_id,
                        occurred_at
                    FROM banxia.decision_event
                    {where}
                    ORDER BY occurred_at DESC, decision_event_id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cursor.fetchall()
        return tuple(
            {
                "event_id": str(row[0]),
                "plan_id": str(row[1]),
                "symbol": str(row[2]),
                "previous_state": row[3],
                "state": str(row[4]),
                "reason_code": str(row[5]),
                "reason": str(row[6]),
                "source_event_id": str(row[7]),
                "occurred_at": _datetime(row[8]).isoformat(),
            }
            for row in rows
        )

    def get_strategy_run(self, run_id: str) -> Optional[Mapping[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        run.run_id, run.trade_date, version.version, run.status,
                        run.requested_at, run.started_at, run.finished_at,
                        run.error_message
                    FROM banxia.strategy_run AS run
                    JOIN banxia.strategy_version AS version
                      ON version.strategy_version_id = run.strategy_version_id
                    WHERE run.run_id = %s
                    """,
                    (run_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row[0]),
            "trade_date": row[1].isoformat(),
            "strategy_version": str(row[2]),
            "status": str(row[3]),
            "created_at": _datetime(row[4]).isoformat(),
            "started_at": _datetime(row[5]).isoformat() if row[5] else None,
            "finished_at": _datetime(row[6]).isoformat() if row[6] else None,
            "error": row[7],
        }

    def enqueue_report_refresh(
        self,
        trade_date: str,
        *,
        requested_by: str = "web",
        strategy_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        job_id = str(uuid.uuid4())
        payload = {
            "trade_date": trade_date,
            "requested_by": requested_by,
        }
        if strategy_id:
            payload["strategy_id"] = strategy_id
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.job_execution (
                        job_id, job_type, idempotency_key, status, payload
                    ) VALUES (%s, 'report_refresh', %s, 'queued', %s::jsonb)
                    RETURNING job_id, status, payload, created_at
                    """,
                    (
                        job_id,
                        f"report-refresh:{job_id}",
                        _json(payload),
                    ),
                )
                row = cursor.fetchone()
        return {
            "job_id": str(row[0]),
            "status": str(row[1]),
            "payload": dict(_mapping(row[2])),
            "created_at": _datetime(row[3]).isoformat(),
        }

    def claim_report_refresh(self) -> Optional[Mapping[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH next_job AS (
                        SELECT job_id
                        FROM banxia.job_execution
                        WHERE job_type = 'report_refresh'
                          AND (
                            status = 'queued'
                            OR (
                              status = 'running'
                              AND started_at < now() - INTERVAL '15 minutes'
                            )
                          )
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE banxia.job_execution AS job
                    SET status = 'running',
                        attempt = job.attempt + 1,
                        started_at = now(),
                        finished_at = NULL,
                        error_message = NULL
                    FROM next_job
                    WHERE job.job_id = next_job.job_id
                    RETURNING
                        job.job_id, job.status, job.attempt, job.payload,
                        job.created_at, job.started_at
                    """
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "job_id": str(row[0]),
            "status": str(row[1]),
            "attempt": int(row[2]),
            "payload": dict(_mapping(row[3])),
            "created_at": _datetime(row[4]).isoformat(),
            "started_at": _datetime(row[5]).isoformat(),
        }

    def finish_report_refresh(
        self,
        job_id: str,
        *,
        succeeded: bool,
        result: Optional[Mapping[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        status = "succeeded" if succeeded else "failed"
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.job_execution
                    SET status = %s,
                        result = %s::jsonb,
                        error_message = %s,
                        finished_at = now()
                    WHERE job_id = %s
                      AND job_type = 'report_refresh'
                      AND status = 'running'
                    """,
                    (
                        status,
                        _json(result or {}),
                        error_message,
                        job_id,
                    ),
                )

    def get_job_execution(self, job_id: str) -> Optional[Mapping[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        job_id, job_type, status, attempt, payload, result,
                        error_message, created_at, started_at, finished_at
                    FROM banxia.job_execution
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "job_id": str(row[0]),
            "job_type": str(row[1]),
            "status": str(row[2]),
            "attempt": int(row[3]),
            "payload": dict(_mapping(row[4])),
            "result": dict(_mapping(row[5])) if row[5] is not None else None,
            "error": row[6],
            "created_at": _datetime(row[7]).isoformat(),
            "started_at": _datetime(row[8]).isoformat() if row[8] else None,
            "finished_at": _datetime(row[9]).isoformat() if row[9] else None,
        }

    def get_report_asset(
        self,
        trade_date: str,
        asset_format: str,
        strategy_id: Optional[str] = None,
    ) -> Optional[Mapping[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        asset.object_key,
                        asset.content_hash,
                        asset.content_type,
                        asset.size_bytes
                    FROM banxia.report_asset AS asset
                    JOIN banxia.strategy_run AS run
                      ON run.run_id = asset.run_id
                    JOIN banxia.strategy_version AS v ON v.strategy_version_id=run.strategy_version_id
                    WHERE run.trade_date = %s
                      AND asset.format = %s
                      AND v.strategy_id=%s
                      AND run.status = 'succeeded'
                    ORDER BY run.finished_at DESC, asset.created_at DESC
                    LIMIT 1
                    """,
                    (trade_date, asset_format, strategy_id or _uuid(f"strategy:{STRATEGY_CODE}")),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "object_key": str(row[0]),
            "content_hash": str(row[1]),
            "content_type": str(row[2]),
            "size_bytes": int(row[3]),
        }

    def list_research_runs(self):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT run_id, start_date, end_date, created_at,
                              payload->'strategy'->>'status'
                       FROM banxia.research_run ORDER BY created_at DESC LIMIT 50"""
                )
                rows = cursor.fetchall()
        return [{"run_id": str(row[0]), "start_date": str(row[1]),
                 "end_date": str(row[2]), "created_at": row[3].isoformat(),
                 "strategy_status": row[4]} for row in rows]

    def get_research_run(self, run_id):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, assets FROM banxia.research_run WHERE run_id = %s",
                    (run_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {**dict(_mapping(row[0])), "assets": row[1]}

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
