from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional

from ..domain.events import EventEnvelope
from ..ports.storage import (
    DecisionRecord,
    ReportAsset,
    ReportIdentity,
)


IDENTITY_NAMESPACE = uuid.UUID("4b6067a1-05ca-4eaf-9c59-ed125f79cb45")
STRATEGY_CODE = "banxia-first-board-second-board"


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


class PostgresStorage:
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
    ) -> ReportIdentity:
        if not strategy_version:
            raise ValueError("strategy_version is required")
        trade_date = str(report["as_of"])
        generated_at = _datetime(report["generated_at"])
        strategy_id_seed = _uuid(f"strategy:{STRATEGY_CODE}")
        version_id_seed = _uuid(
            f"strategy-version:{STRATEGY_CODE}:{strategy_version}"
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
                    ON CONFLICT (code) DO UPDATE SET
                        name = EXCLUDED.name,
                        description = EXCLUDED.description
                    RETURNING strategy_id
                    """,
                    (
                        strategy_id_seed,
                        STRATEGY_CODE,
                        "首板晋级二板策略",
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
        return ReportIdentity(
            run_id=run_id,
            strategy_version_id=strategy_version_id,
            plan_id=plan_id,
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

    def apply(
        self,
        *,
        input_event_id: str,
        decision: DecisionRecord,
        outbox_event: EventEnvelope,
    ) -> bool:
        offset = int(hashlib.sha256(input_event_id.encode()).hexdigest()[:15], 16)
        input_event_type = str(
            outbox_event.payload.get("source_event_type") or "unknown"
        )
        topic = f"direct:{input_event_type}"
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.inbox_event (
                        event_id, event_type, topic, partition_id, offset_value
                    ) VALUES (%s, %s, %s, 0, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        input_event_id,
                        input_event_type,
                        topic,
                        offset,
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
                        aggregate_type, aggregate_id, event_type, topic,
                        message_key, payload, occurred_at
                    ) VALUES (
                        'decision', %s, %s, 'strategy.decision.v1',
                        %s, %s::jsonb, %s
                    )
                    """,
                    (
                        f"{decision.plan_id}:{decision.symbol}",
                        outbox_event.event_type,
                        f"{decision.plan_id}:{decision.symbol}",
                        _json(outbox_event.to_dict()),
                        decision.occurred_at,
                    ),
                )
        return True

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
