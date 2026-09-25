"""Strategy catalog and one materialized record per strategy/trading day."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, replace
from datetime import date

from ..strategy_config import ConfigConflict, StrategyConfig, StrategyConfigStore


INITIAL_STRATEGY_CODE = "banxia-first-board-second-board"


class CatalogConfigStore(StrategyConfigStore):
    def __init__(self, repository, strategy_id):
        self.repository = repository
        self.strategy_id = strategy_id

    def read(self):
        item = self.repository.get_strategy(self.strategy_id)
        if item is None:
            raise ValueError("策略不存在")
        return StrategyConfig.from_mapping(item["config"] or {})

    def save(self, raw, revision):
        raise ConfigConflict("已保存策略不可覆盖，请将修改保存为新策略")


class SharedCollectionConfig:
    """Collect at the fastest requested interval, once for all enabled strategies."""
    def __init__(self, repository, default_store):
        self.repository, self.default_store = repository, default_store

    def read(self):
        active = self.repository.get_active_strategy()
        config = StrategyConfig.from_mapping(active["config"]) if active else self.default_store.read()
        return replace(config, **{
            key: getattr(config, key)
            for key in ("quote_interval_seconds", "idle_interval_seconds", "bar_interval_seconds")
        })


class StrategyCatalogMixin:
    def ensure_initial_strategy(self, config, *, name="首板晋级二板策略"):
        """Ensure the protected initial strategy exists and is visible."""
        values = asdict(StrategyConfig.from_mapping(config))
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('banxia-initial-strategy'))"
                )
                cursor.execute(
                    """SELECT strategy_id,archived FROM banxia.strategy_definition
                    WHERE code=%s FOR UPDATE""",
                    (INITIAL_STRATEGY_CODE,),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        """SELECT EXISTS(
                            SELECT 1 FROM banxia.strategy_definition
                            WHERE enabled AND NOT archived
                        )"""
                    )
                    has_active = bool(cursor.fetchone()[0])
                    cursor.execute(
                        """INSERT INTO banxia.strategy_definition
                        (strategy_id,code,name,description,current_config,enabled,archived)
                        VALUES (%s,%s,%s,%s,%s::jsonb,%s,false)
                        RETURNING strategy_id""",
                        (
                            str(uuid.uuid5(
                                uuid.UUID("4b6067a1-05ca-4eaf-9c59-ed125f79cb45"),
                                f"strategy:{INITIAL_STRATEGY_CODE}",
                            )),
                            INITIAL_STRATEGY_CODE,
                            name,
                            "基于 mootdx 的沪深主板一进二条件筛选与当日实盘",
                            json.dumps(values),
                            not has_active,
                        ),
                    )
                    strategy_id = str(cursor.fetchone()[0])
                else:
                    strategy_id = str(row[0])
                    cursor.execute("SET LOCAL banxia.allow_initial_strategy_upgrade = 'on'")
                    cursor.execute(
                        """UPDATE banxia.strategy_definition
                        SET current_config=%s::jsonb
                        WHERE strategy_id=%s AND current_config IS DISTINCT FROM %s::jsonb""",
                        (json.dumps(values), strategy_id, json.dumps(values)),
                    )
                    if row[1]:
                        cursor.execute(
                            """SELECT EXISTS(
                                SELECT 1 FROM banxia.strategy_definition
                                WHERE enabled AND NOT archived AND strategy_id<>%s
                            )""",
                            (strategy_id,),
                        )
                        has_active = bool(cursor.fetchone()[0])
                        cursor.execute(
                            """UPDATE banxia.strategy_definition
                            SET archived=false,enabled=%s WHERE strategy_id=%s""",
                            (not has_active, strategy_id),
                        )
        return self.get_strategy(strategy_id)

    def list_strategies(self):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT strategy_id,code,name,description,enabled,archived,current_config,
                    parent_strategy_id,config_changes
                    FROM banxia.strategy_definition WHERE NOT archived ORDER BY created_at,strategy_id""")
                return [self._strategy_row(row) for row in cursor.fetchall()]

    @staticmethod
    def _strategy_row(row):
        return dict(zip(("strategy_id", "code", "name", "description", "enabled", "archived", "config",
                         "parent_strategy_id", "config_changes"),
                        (str(row[0]), *row[1:7], str(row[7]) if row[7] else None, row[8] or {})))

    def get_strategy(self, strategy_id):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT strategy_id,code,name,description,enabled,archived,current_config,
                    parent_strategy_id,config_changes
                    FROM banxia.strategy_definition WHERE strategy_id=%s""", (strategy_id,))
                row = cursor.fetchone()
                return self._strategy_row(row) if row else None

    def get_active_strategy(self):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT strategy_id,code,name,description,enabled,archived,current_config,
                    parent_strategy_id,config_changes FROM banxia.strategy_definition
                    WHERE enabled AND NOT archived""")
                row = cursor.fetchone()
                return self._strategy_row(row) if row else None

    def create_strategy(self, name, config, *, description="", enabled=False, strategy_id=None,
                        parent_strategy_id=None, config_changes=None, materialization=None):
        name = self._validated_name(name)
        values = asdict(StrategyConfig.from_mapping(config))
        strategy_id = strategy_id or str(uuid.uuid4())
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                if parent_strategy_id:
                    cursor.execute("SELECT 1 FROM banxia.strategy_definition WHERE strategy_id=%s",
                                   (parent_strategy_id,))
                    if cursor.fetchone() is None:
                        raise ValueError("来源策略不存在")
                cursor.execute("""INSERT INTO banxia.strategy_definition
                    (strategy_id,code,name,description,current_config,enabled,parent_strategy_id,config_changes)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb)""",
                    (strategy_id, f"custom-{strategy_id}", name, description,
                     json.dumps(values), False, parent_strategy_id, json.dumps(config_changes or {})))
                if materialization:
                    payload = {
                        **materialization,
                        "strategy_id": strategy_id,
                    }
                    job_id = str(uuid.uuid4())
                    cursor.execute(
                        """INSERT INTO banxia.job_execution
                        (job_id,job_type,idempotency_key,status,payload)
                        VALUES (%s,'strategy_history',%s,'queued',%s::jsonb)""",
                        (
                            job_id,
                            f"strategy-history:{strategy_id}:{payload['start']}:{payload['end']}",
                            json.dumps(payload),
                        ),
                    )
                cursor.execute("""INSERT INTO banxia.strategy_day(strategy_id,trade_date)
                    SELECT %s,trade_date FROM banxia.trading_session
                    WHERE trade_date >= (SELECT max(trade_date) FROM banxia.trading_session WHERE trade_date<=CURRENT_DATE)
                    AND trade_date <= CURRENT_DATE ON CONFLICT DO NOTHING""", (strategy_id,))
        if enabled:
            return self.activate_strategy(strategy_id)
        return self.get_strategy(strategy_id)

    @staticmethod
    def _validated_name(name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("策略名称需为 1 至 80 个字符")
        return name.strip()

    def rename_strategy(self, strategy_id, name):
        name = self._validated_name(name)
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""UPDATE banxia.strategy_definition SET name=%s
                    WHERE strategy_id=%s AND NOT archived""", (name, strategy_id))
                if cursor.rowcount != 1:
                    raise ValueError("策略不存在或已归档")
        return self.get_strategy(strategy_id)

    def update_strategy(self, strategy_id, *, enabled, archived=False):
        if not isinstance(enabled, bool) or not isinstance(archived, bool):
            raise ValueError("启用与归档状态必须为布尔值")
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('banxia-active-strategy'))")
                if archived:
                    cursor.execute("""UPDATE banxia.strategy_definition SET enabled=false,archived=true
                        WHERE strategy_id=%s""", (strategy_id,))
                elif enabled:
                    cursor.execute("""UPDATE banxia.strategy_definition SET enabled=false
                        WHERE enabled AND NOT archived AND strategy_id<>%s""", (strategy_id,))
                    cursor.execute("""UPDATE banxia.strategy_definition SET enabled=true
                        WHERE strategy_id=%s AND NOT archived""", (strategy_id,))
                else:
                    cursor.execute("""UPDATE banxia.strategy_definition SET enabled=false
                        WHERE strategy_id=%s""", (strategy_id,))
        return self.get_strategy(strategy_id)

    def activate_strategy(self, strategy_id):
        return self.update_strategy(strategy_id, enabled=True)

    def delete_strategy(self, strategy_id, *, cleanup=None):
        """Permanently delete one strategy and all strategy-owned records."""
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT strategy_id,code FROM banxia.strategy_definition
                    WHERE strategy_id=%s FOR UPDATE""",
                    (strategy_id,),
                )
                strategy = cursor.fetchone()
                if strategy is None:
                    raise ValueError("策略不存在")
                if strategy[1] == INITIAL_STRATEGY_CODE:
                    raise PermissionError("初始策略不可删除")

                cursor.execute(
                    """SELECT v.strategy_version_id,r.run_id,p.plan_id,p.trade_date
                    FROM banxia.strategy_version v
                    LEFT JOIN banxia.strategy_run r USING(strategy_version_id)
                    LEFT JOIN banxia.strategy_plan p ON p.run_id=r.run_id
                    WHERE v.strategy_id=%s""",
                    (strategy_id,),
                )
                rows = cursor.fetchall()
                version_ids = sorted({str(row[0]) for row in rows if row[0]})
                run_ids = sorted({str(row[1]) for row in rows if row[1]})
                plan_ids = sorted({str(row[2]) for row in rows if row[2]})
                trade_dates = sorted({row[3].isoformat() for row in rows if row[3]})

                cursor.execute(
                    """SELECT object_key FROM banxia.report_asset
                    WHERE run_id=ANY(%s::uuid[])""",
                    (run_ids,),
                )
                object_keys = {str(row[0]) for row in cursor.fetchall()}

                cursor.execute(
                    """SELECT DISTINCT run.run_id,run.assets
                    FROM banxia.research_run run
                    WHERE jsonb_path_exists(
                        run.payload,
                        '$.** ? (@ == $strategy_id)',
                        jsonb_build_object('strategy_id',to_jsonb(%s::text))
                    )
                    OR EXISTS (
                        SELECT 1 FROM banxia.research_strategy research
                        WHERE research.run_id=run.run_id
                        AND (
                            research.strategy_id=%s
                            OR jsonb_path_exists(
                                research.metadata,
                                '$.** ? (@ == $strategy_id)',
                                jsonb_build_object('strategy_id',to_jsonb(%s::text))
                            )
                        )
                    )
                    OR run.run_id::text IN (
                        SELECT day.next_plan->>'research_run_id'
                        FROM banxia.strategy_day day
                        WHERE day.strategy_id=%s AND day.next_plan ? 'research_run_id'
                        UNION
                        SELECT day.execution_plan->>'research_run_id'
                        FROM banxia.strategy_day day
                        WHERE day.strategy_id=%s AND day.execution_plan ? 'research_run_id'
                    )""",
                    (
                        str(strategy_id),
                        str(strategy_id),
                        str(strategy_id),
                        strategy_id,
                        strategy_id,
                    ),
                )
                research_rows = cursor.fetchall()
                research_run_ids = sorted(str(row[0]) for row in research_rows)
                for _run_id, assets in research_rows:
                    if isinstance(assets, str):
                        assets = json.loads(assets)
                    for asset in assets or ():
                        if isinstance(asset, dict) and asset.get("object_key"):
                            object_keys.add(str(asset["object_key"]))

                # Descendant strategies remain usable, but no longer reference
                # the strategy being permanently deleted.
                cursor.execute(
                    """UPDATE banxia.strategy_definition SET parent_strategy_id=NULL
                    WHERE parent_strategy_id=%s""",
                    (strategy_id,),
                )
                detached_children = cursor.rowcount

                if research_run_ids:
                    cursor.execute(
                        "DELETE FROM banxia.research_daily WHERE run_id=ANY(%s::uuid[])",
                        (research_run_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM banxia.research_strategy WHERE run_id=ANY(%s::uuid[])",
                        (research_run_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM banxia.research_run WHERE run_id=ANY(%s::uuid[])",
                        (research_run_ids,),
                    )

                cursor.execute(
                    "DELETE FROM banxia.strategy_day WHERE strategy_id=%s",
                    (strategy_id,),
                )
                strategy_days = cursor.rowcount

                if plan_ids:
                    cursor.execute(
                        """DELETE FROM banxia.outbox_event
                        WHERE (aggregate_type='strategy_plan' AND aggregate_id=ANY(%s::text[]))
                        OR (aggregate_type='decision'
                            AND split_part(aggregate_id,':',1)=ANY(%s::text[]))""",
                        (plan_ids, plan_ids),
                    )
                    cursor.execute(
                        """DELETE FROM banxia.inbox_event inbox
                        WHERE EXISTS (
                            SELECT 1 FROM unnest(%s::text[]) plan_id
                            WHERE inbox.topic LIKE '%%:plan:' || plan_id
                        )""",
                        (plan_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM banxia.watchlist WHERE plan_id=ANY(%s::uuid[])",
                        (plan_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM banxia.strategy_plan WHERE plan_id=ANY(%s::uuid[])",
                        (plan_ids,),
                    )

                if run_ids:
                    cursor.execute(
                        """DELETE FROM banxia.outbox_event
                        WHERE aggregate_type='strategy_run' AND aggregate_id=ANY(%s::text[])""",
                        (run_ids,),
                    )
                    cursor.execute(
                        "DELETE FROM banxia.strategy_run WHERE run_id=ANY(%s::uuid[])",
                        (run_ids,),
                    )
                if version_ids:
                    cursor.execute(
                        """DELETE FROM banxia.strategy_version
                        WHERE strategy_version_id=ANY(%s::uuid[])""",
                        (version_ids,),
                    )

                cursor.execute(
                    """DELETE FROM banxia.job_execution
                    WHERE jsonb_path_exists(
                        COALESCE(payload,'{}'::jsonb),
                        '$.** ? (@ == $strategy_id)',
                        jsonb_build_object('strategy_id',to_jsonb(%s::text))
                    )
                    OR jsonb_path_exists(
                        COALESCE(result,'{}'::jsonb),
                        '$.** ? (@ == $strategy_id)',
                        jsonb_build_object('strategy_id',to_jsonb(%s::text))
                    )""",
                    (str(strategy_id), str(strategy_id)),
                )
                cursor.execute(
                    """DELETE FROM banxia.audit_log
                    WHERE resource_id=%s
                    OR before_value @> jsonb_build_object('strategy_id',%s::text)
                    OR after_value @> jsonb_build_object('strategy_id',%s::text)""",
                    (str(strategy_id), str(strategy_id), str(strategy_id)),
                )
                cursor.execute(
                    "DELETE FROM banxia.strategy_definition WHERE strategy_id=%s",
                    (strategy_id,),
                )

                manifest = {
                    "strategy_id": str(strategy_id),
                    "plan_ids": plan_ids,
                    "trade_dates": trade_dates,
                    "object_keys": sorted(object_keys),
                    "research_run_ids": research_run_ids,
                    "strategy_days": strategy_days,
                    "detached_children": detached_children,
                }
                if cleanup is not None:
                    cleanup(manifest)
                return manifest

    def enqueue_strategy_history(
        self,
        strategy_id,
        history_range,
        start,
        end,
        *,
        requested_by="system",
    ):
        payload = {
            "strategy_id": str(strategy_id),
            "history_range": history_range,
            "start": str(start),
            "end": str(end),
            "requested_by": requested_by,
            "datasets": ["next_plan", "intraday_monitor"],
        }
        coverage = self.strategy_history_coverage(strategy_id, start, end)
        complete = (
            coverage["session_count"] > 0
            and coverage["plan_count"] == coverage["session_count"]
            and coverage["execution_count"] == coverage["session_count"]
            and coverage["actual_count"] == coverage["session_count"]
        )
        status = "succeeded" if complete else "queued"
        result = {
            "strategy_id": str(strategy_id),
            "history_range": history_range,
            "start": str(start),
            "end": str(end),
            "datasets": payload["datasets"],
            **coverage,
        } if complete else None
        job_id = str(uuid.uuid4())
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO banxia.job_execution
                    (job_id,job_type,idempotency_key,status,payload,result,finished_at)
                    VALUES (%s,'strategy_history',%s,%s,%s::jsonb,%s::jsonb,
                      CASE WHEN %s='succeeded' THEN now() ELSE NULL END)
                    ON CONFLICT(idempotency_key) DO UPDATE SET
                      status=CASE
                        WHEN EXCLUDED.status='succeeded' THEN 'succeeded'
                        WHEN banxia.job_execution.status='failed' THEN 'queued'
                        ELSE banxia.job_execution.status
                      END,
                      result=CASE
                        WHEN EXCLUDED.status='succeeded' THEN EXCLUDED.result
                        ELSE banxia.job_execution.result
                      END,
                      error_message=CASE
                        WHEN EXCLUDED.status='succeeded'
                          OR banxia.job_execution.status='failed' THEN NULL
                        ELSE banxia.job_execution.error_message
                      END,
                      finished_at=CASE
                        WHEN EXCLUDED.status='succeeded' THEN EXCLUDED.finished_at
                        WHEN banxia.job_execution.status='failed' THEN NULL
                        ELSE banxia.job_execution.finished_at
                      END
                    RETURNING job_id,status,payload,created_at""",
                    (
                        job_id,
                        f"strategy-history:{strategy_id}:{start}:{end}",
                        status,
                        json.dumps(payload),
                        json.dumps(result) if result else None,
                        status,
                    ),
                )
                row = cursor.fetchone()
        return self._strategy_history_job_row(row)

    def strategy_history_coverage(self, strategy_id, start, end):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT count(*) AS session_count,
                        count(day.next_plan) AS plan_count,
                        count(day.execution_plan) AS execution_count,
                        count(*) FILTER (
                          WHERE day.actuals IS NOT NULL
                            AND day.actuals<>'{}'::jsonb
                        ) AS actual_count
                    FROM banxia.trading_session session
                    LEFT JOIN banxia.strategy_day day
                      ON day.strategy_id=%s
                     AND day.trade_date=session.trade_date
                    WHERE session.trade_date BETWEEN %s AND %s""",
                    (strategy_id, start, end),
                )
                row = cursor.fetchone()
        return {
            "session_count": int(row[0]),
            "plan_count": int(row[1]),
            "execution_count": int(row[2]),
            "actual_count": int(row[3]),
        }

    def get_latest_strategy_history(self, strategy_id):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT job_id,status,payload,created_at,started_at,finished_at,
                        result,error_message,attempt
                    FROM banxia.job_execution
                    WHERE job_type='strategy_history'
                      AND payload->>'strategy_id'=%s
                    ORDER BY created_at DESC LIMIT 1""",
                    (str(strategy_id),),
                )
                row = cursor.fetchone()
        return self._strategy_history_job_row(row) if row else None

    def claim_strategy_history(self, *, allow_automatic=True):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """WITH next_job AS (
                        SELECT job_id FROM banxia.job_execution
                        WHERE job_type='strategy_history'
                          AND (
                            %s
                            OR payload->>'requested_by' NOT IN ('startup','system')
                          )
                          AND (
                            status='queued'
                            OR (
                              status='running'
                              AND started_at < now() - INTERVAL '2 hours'
                            )
                          )
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE banxia.job_execution AS job
                    SET status='running',attempt=job.attempt+1,started_at=now(),
                        finished_at=NULL,error_message=NULL
                    FROM next_job
                    WHERE job.job_id=next_job.job_id
                    RETURNING job.job_id,job.status,job.payload,job.created_at,
                        job.started_at,job.finished_at,job.result,
                        job.error_message,job.attempt""",
                    (allow_automatic,),
                )
                row = cursor.fetchone()
        return self._strategy_history_job_row(row) if row else None

    def finish_strategy_history(self, job_id, *, succeeded, result=None, error_message=None):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE banxia.job_execution
                    SET status=%s,result=%s::jsonb,error_message=%s,finished_at=now()
                    WHERE job_id=%s AND job_type='strategy_history' AND status='running'""",
                    (
                        "succeeded" if succeeded else "failed",
                        json.dumps(result or {}),
                        error_message,
                        job_id,
                    ),
                )

    @staticmethod
    def _strategy_history_job_row(row):
        if row is None:
            return None
        values = list(row) + [None] * (9 - len(row))
        payload = values[2]
        result = values[6]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(result, str):
            result = json.loads(result)
        return {
            "job_id": str(values[0]),
            "job_type": "strategy_history",
            "status": str(values[1]),
            "payload": payload or {},
            "created_at": values[3].isoformat(),
            "started_at": values[4].isoformat() if values[4] else None,
            "finished_at": values[5].isoformat() if values[5] else None,
            "result": result,
            "error": values[7],
            "attempt": int(values[8] or 0),
        }

    def save_trading_sessions(self, sessions):
        sessions = sorted(set(str(day) for day in sessions))
        if not sessions:
            return
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.executemany("INSERT INTO banxia.trading_session VALUES (%s) ON CONFLICT DO NOTHING",
                                   [(day,) for day in sessions])
                # Replace this calendar range: a formerly guessed future session
                # must disappear once mootdx supplies a corrected holiday calendar.
                cursor.execute("""DELETE FROM banxia.trading_session
                    WHERE trade_date BETWEEN %s AND %s AND NOT(trade_date=ANY(%s::date[]))""",
                    (sessions[0], sessions[-1], sessions))
                cursor.execute("""DELETE FROM banxia.strategy_day d
                    WHERE d.trade_date BETWEEN %s AND %s
                    AND d.next_plan IS NULL AND d.execution_plan IS NULL AND d.actuals='{}'::jsonb
                    AND NOT EXISTS(SELECT 1 FROM banxia.trading_session t WHERE t.trade_date=d.trade_date)""",
                    (sessions[0], sessions[-1]))

    def is_trading_session(self, target):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM banxia.trading_session WHERE trade_date=%s)",
                    (target,),
                )
                row = cursor.fetchone()
        return bool(row and row[0])

    def previous_trading_session(self, target):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT max(trade_date) FROM banxia.trading_session
                    WHERE trade_date < %s""",
                    (target,),
                )
                row = cursor.fetchone()
        return row[0].isoformat() if row and row[0] else None

    def latest_closed_session(self, now):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT max(trade_date) FROM banxia.trading_session
                    WHERE trade_date < %s OR (trade_date=%s AND %s)""",
                    (now.date(), now.date(), now.hour >= 15))
                row = cursor.fetchone()
                return row[0].isoformat() if row and row[0] else None

    def ensure_strategy_days(self, today):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO banxia.strategy_day(strategy_id,trade_date)
                    SELECT s.strategy_id,t.trade_date FROM banxia.strategy_definition s
                    CROSS JOIN banxia.trading_session t
                    WHERE NOT s.archived AND t.trade_date=%s
                    ON CONFLICT DO NOTHING""", (today,))

    @staticmethod
    def _save_daily_report(cursor, strategy_id, report):
        cursor.execute("""INSERT INTO banxia.strategy_day(strategy_id,trade_date,next_plan)
            VALUES (%s,%s,%s::jsonb) ON CONFLICT(strategy_id,trade_date)
            DO UPDATE SET next_plan=EXCLUDED.next_plan,updated_at=now()""",
            (strategy_id, report["as_of"], json.dumps(report, ensure_ascii=False)))
        if report.get("next_session"):
            cursor.execute("""INSERT INTO banxia.strategy_day(strategy_id,trade_date,execution_plan)
                VALUES (%s,%s,%s::jsonb) ON CONFLICT(strategy_id,trade_date)
                DO UPDATE SET execution_plan=EXCLUDED.execution_plan,updated_at=now(),
                actuals=CASE WHEN banxia.strategy_day.execution_plan->>'plan_id' IS DISTINCT FROM
                  EXCLUDED.execution_plan->>'plan_id' THEN '{}'::jsonb ELSE banxia.strategy_day.actuals END""",
                (strategy_id, report["next_session"], json.dumps(report, ensure_ascii=False)))

    def save_daily_report(self, strategy_id, report):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                self._save_daily_report(cursor, strategy_id, report)

    def fill_daily_report_gaps(self, strategy_id, report):
        """Fill missing catalog projections without replacing existing history."""
        payload = json.dumps(report, ensure_ascii=False)
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO banxia.strategy_day(strategy_id,trade_date,next_plan)
                    VALUES (%s,%s,%s::jsonb) ON CONFLICT(strategy_id,trade_date)
                    DO UPDATE SET next_plan=EXCLUDED.next_plan,updated_at=now()
                    WHERE banxia.strategy_day.next_plan IS NULL""",
                    (strategy_id, report["as_of"], payload))
                if report.get("next_session"):
                    cursor.execute("""INSERT INTO banxia.strategy_day(strategy_id,trade_date,execution_plan)
                        VALUES (%s,%s,%s::jsonb) ON CONFLICT(strategy_id,trade_date)
                        DO UPDATE SET execution_plan=EXCLUDED.execution_plan,updated_at=now()
                        WHERE banxia.strategy_day.execution_plan IS NULL""",
                        (strategy_id, report["next_session"], payload))

    def save_day_actuals(self, strategy_id, trade_date, actuals, plan_id=None):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""UPDATE banxia.strategy_day SET actuals=%s::jsonb,updated_at=now()
                    WHERE strategy_id=%s AND trade_date=%s
                    AND execution_plan->>'plan_id' IS NOT DISTINCT FROM %s""",
                    (json.dumps(actuals, ensure_ascii=False), strategy_id, trade_date, plan_id))

    def list_strategy_days(self, strategy_id, limit=100, before=None):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT trade_date,next_plan,execution_plan,actuals,updated_at
                    FROM banxia.strategy_day WHERE strategy_id=%s AND (%s::date IS NULL OR trade_date<%s)
                    ORDER BY trade_date DESC LIMIT %s""", (strategy_id, before, before, limit))
                return [self._day_row(row, detail=False) for row in cursor.fetchall()]

    @staticmethod
    def _day_row(row, detail=True):
        report, execution, actuals = row[1], row[2], row[3]
        result = {
            "trade_date": row[0].isoformat(), "updated_at": row[4].isoformat(),
            "plan_date": report.get("next_session") if report else None,
            "candidate_count": len(report.get("candidates", [])) if report else None,
            "execution_count": len(execution.get("candidates", [])) if execution else None,
            "plan_status": "ready" if report else "pending",
            "actual_status": actuals.get("status", "no_candidates" if execution and not execution.get("candidates")
                                        else "pending" if execution else "no_plan"),
            "summary": actuals.get("summary", {}),
            "strategy_version": report.get("strategy_version") if report else None,
        }
        if detail:
            result.update(next_plan=report, execution_plan=execution, actuals=actuals)
        return result

    def get_strategy_day(self, strategy_id, trade_date):
        date.fromisoformat(trade_date)
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT trade_date,next_plan,execution_plan,actuals,updated_at
                    FROM banxia.strategy_day WHERE strategy_id=%s AND trade_date=%s""", (strategy_id, trade_date))
                row = cursor.fetchone()
                return self._day_row(row) if row else None

    def list_monitor_plans(self):
        """Only current/future plans; each strategy/date keeps its latest revision."""
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT DISTINCT v.strategy_id,p.trade_date
                    FROM banxia.strategy_plan p JOIN banxia.strategy_version v USING(strategy_version_id)
                    JOIN banxia.strategy_definition s USING(strategy_id)
                    WHERE p.status='active' AND s.enabled AND NOT s.archived
                      AND p.trade_date >= CURRENT_DATE AND p.trade_date <= CURRENT_DATE + 14""")
                keys = cursor.fetchall()
        return [plan for sid, day in keys if (plan := self.get_active_plan(str(day), str(sid)))]
