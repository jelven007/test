"""Strategy catalog and one materialized record per strategy/trading day."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, replace
from datetime import date

from ..strategy_config import ConfigConflict, StrategyConfig, StrategyConfigStore


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
                        parent_strategy_id=None, config_changes=None):
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
