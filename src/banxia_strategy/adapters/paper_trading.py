"""PostgreSQL persistence for forward-only paper-trading campaigns."""
from __future__ import annotations

import json
import math
from datetime import date
from typing import Any, Mapping, Optional


def _number(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


def _wilson(successes: int, count: int) -> tuple[float, float]:
    if count == 0:
        return (0.0, 1.0)
    z = 1.959963984540054
    probability = successes / count
    denominator = 1 + z * z / count
    center = (
        probability + z * z / (2 * count)
    ) / denominator
    half = (
        z
        * math.sqrt(
            probability * (1 - probability) / count
            + z * z / (4 * count * count)
        )
        / denominator
    )
    return (center - half, center + half)


class PaperTradingMixin:
    def ensure_paper_campaign(
        self,
        code: str = "first-board-positive-v1",
        *,
        target_sample_count: int = 30,
    ) -> Mapping[str, Any]:
        if target_sample_count <= 0:
            raise ValueError("paper target sample count must be positive")
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT strategy_id
                    FROM banxia.strategy_definition
                    WHERE code = 'paper-first-board-positive-v1'
                    """
                )
                strategy = cursor.fetchone()
                if strategy is None:
                    cursor.execute(
                        """
                        SELECT strategy_id, current_config
                        FROM banxia.strategy_definition
                        WHERE code = 'banxia-first-board-second-board'
                          AND NOT archived
                        FOR UPDATE
                        """
                    )
                    source = cursor.fetchone()
                    if source is None:
                        raise ValueError(
                            "first-board source strategy is unavailable"
                        )
                    cursor.execute(
                        """
                        INSERT INTO banxia.strategy_definition (
                            strategy_id, code, name, description,
                            current_config, enabled, archived,
                            parent_strategy_id, config_changes
                        ) VALUES (
                            'ff8e167e-c458-511f-a0ba-8161cfaa063f',
                            'paper-first-board-positive-v1',
                            '一进二正收益模拟盘 V1',
                            '冻结一进二正收益观察参数，仅用于模拟成交和新增样本验证。',
                            %s::jsonb,
                            false,
                            false,
                            %s,
                            %s::jsonb
                        )
                        ON CONFLICT (code) DO NOTHING
                        RETURNING strategy_id
                        """,
                        (
                            json.dumps(source[1], ensure_ascii=False),
                            source[0],
                            json.dumps(
                                {
                                    "paper_campaign": {
                                        "source": (
                                            "banxia-first-board-second-board"
                                        ),
                                        "purpose": (
                                            "30-new-sample-forward-validation"
                                        ),
                                    }
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    strategy = cursor.fetchone()
                    if strategy is None:
                        cursor.execute(
                            """
                            SELECT strategy_id
                            FROM banxia.strategy_definition
                            WHERE code = 'paper-first-board-positive-v1'
                            """
                        )
                        strategy = cursor.fetchone()
                strategy_id = str(strategy[0])
                cursor.execute(
                    """
                    SELECT current_config
                    FROM banxia.strategy_definition
                    WHERE strategy_id = %s
                    """,
                    (strategy_id,),
                )
                config = cursor.fetchone()[0]
                cursor.execute(
                    """
                    INSERT INTO banxia.paper_campaign (
                        campaign_id, code, strategy_id, started_on,
                        target_sample_count, config_snapshot
                    ) VALUES (
                        'b170b44a-4d72-532b-a28f-6654b18d7d00',
                        %s,
                        %s,
                        CURRENT_DATE,
                        %s,
                        %s::jsonb
                    )
                    ON CONFLICT (code) DO NOTHING
                    """,
                    (
                        code,
                        strategy_id,
                        target_sample_count,
                        json.dumps(config, ensure_ascii=False),
                    ),
                )
        campaign = self.get_paper_campaign(code)
        if campaign is None:
            raise RuntimeError("paper campaign initialization failed")
        return campaign

    def get_paper_campaign(
        self,
        code: str = "first-board-positive-v1",
    ) -> Optional[Mapping[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        campaign_id, code, strategy_id, started_on,
                        target_sample_count, status, config_snapshot,
                        completed_at, created_at, updated_at
                    FROM banxia.paper_campaign
                    WHERE code = %s
                    """,
                    (code,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return {
            "campaign_id": str(row[0]),
            "code": str(row[1]),
            "strategy_id": str(row[2]),
            "started_on": row[3].isoformat(),
            "target_sample_count": int(row[4]),
            "status": str(row[5]),
            "config_snapshot": dict(row[6]),
            "completed_at": row[7].isoformat() if row[7] else None,
            "created_at": row[8].isoformat(),
            "updated_at": row[9].isoformat(),
        }

    def list_paper_entry_candidates(
        self,
        campaign_id: str,
        through_date: date,
    ) -> list[Mapping[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        day.trade_date,
                        day.execution_plan,
                        campaign.started_on
                    FROM banxia.paper_campaign AS campaign
                    JOIN banxia.strategy_day AS day
                      ON day.strategy_id = campaign.strategy_id
                    WHERE campaign.campaign_id = %s
                      AND day.trade_date <= %s
                      AND day.execution_plan IS NOT NULL
                      AND (day.execution_plan->>'as_of')::date
                          >= campaign.started_on
                    ORDER BY day.trade_date
                    """,
                    (campaign_id, through_date),
                )
                days = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT plan_id, symbol, status
                    FROM banxia.paper_trade
                    WHERE campaign_id = %s
                    """,
                    (campaign_id,),
                )
                existing = {
                    (str(row[0]), str(row[1])): str(row[2])
                    for row in cursor.fetchall()
                }
        result = []
        for entry_date, execution, _started_on in days:
            plan_id = str(execution["plan_id"])
            for candidate in execution.get("candidates", []):
                symbol = str(candidate["code"])
                status = existing.get((plan_id, symbol))
                if status not in {None, "entry_data_missing"}:
                    continue
                result.append(
                    {
                        "plan_id": plan_id,
                        "reference_date": str(execution["as_of"]),
                        "entry_date": entry_date.isoformat(),
                        "candidate": dict(candidate),
                    }
                )
        return result

    def save_paper_entry(
        self,
        campaign_id: str,
        record: Mapping[str, Any],
    ) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.paper_trade (
                        campaign_id, plan_id, symbol, name, industry,
                        reference_date, entry_date, exit_date, status,
                        rejection_reason, entry_time, entry_price,
                        entry_sample_price, shares, target_price,
                        entry_evidence, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s::jsonb, now()
                    )
                    ON CONFLICT (campaign_id, plan_id, symbol) DO UPDATE SET
                        exit_date = EXCLUDED.exit_date,
                        status = EXCLUDED.status,
                        rejection_reason = EXCLUDED.rejection_reason,
                        entry_time = EXCLUDED.entry_time,
                        entry_price = EXCLUDED.entry_price,
                        entry_sample_price = EXCLUDED.entry_sample_price,
                        shares = EXCLUDED.shares,
                        target_price = EXCLUDED.target_price,
                        entry_evidence = EXCLUDED.entry_evidence,
                        updated_at = now()
                    WHERE banxia.paper_trade.status = 'entry_data_missing'
                    """,
                    (
                        campaign_id,
                        record["plan_id"],
                        record["symbol"],
                        record["name"],
                        record.get("industry"),
                        record["reference_date"],
                        record["entry_date"],
                        record.get("exit_date"),
                        record["status"],
                        record.get("rejection_reason"),
                        record.get("entry_time"),
                        record.get("entry_price"),
                        record.get("entry_sample_price"),
                        record.get("shares"),
                        record.get("target_price"),
                        json.dumps(
                            record.get("entry_evidence", {}),
                            ensure_ascii=False,
                        ),
                    ),
                )

    def list_paper_pending_exits(
        self,
        campaign_id: str,
        through_date: date,
    ) -> list[Mapping[str, Any]]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        paper_trade_id, symbol, name, industry,
                        reference_date, entry_date, exit_date, entry_time,
                        entry_price, entry_sample_price, shares, target_price,
                        entry_evidence
                    FROM banxia.paper_trade
                    WHERE campaign_id = %s
                      AND status IN ('open', 'exit_data_missing')
                      AND exit_date <= %s
                    ORDER BY exit_date, symbol
                    """,
                    (campaign_id, through_date),
                )
                rows = cursor.fetchall()
        return [
            {
                "paper_trade_id": str(row[0]),
                "symbol": str(row[1]),
                "name": str(row[2]),
                "industry": row[3],
                "reference_date": row[4].isoformat(),
                "entry_date": row[5].isoformat(),
                "exit_date": row[6].isoformat(),
                "entry_time": row[7].isoformat(),
                "entry_price": float(row[8]),
                "entry_sample_price": float(row[9]),
                "shares": int(row[10]),
                "target_price": float(row[11]),
                "entry_evidence": dict(row[12]),
            }
            for row in rows
        ]

    def save_paper_exit(
        self,
        paper_trade_id: str,
        record: Mapping[str, Any],
    ) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.paper_trade
                    SET
                        status = %s,
                        exit_time = %s,
                        exit_price = %s,
                        exit_reason = %s,
                        net_return_pct = %s,
                        positive = %s,
                        exit_evidence = %s::jsonb,
                        updated_at = now()
                    WHERE paper_trade_id = %s
                      AND status IN ('open', 'exit_data_missing')
                    """,
                    (
                        record["status"],
                        record.get("exit_time"),
                        record.get("exit_price"),
                        record.get("exit_reason"),
                        record.get("net_return_pct"),
                        record.get("positive"),
                        json.dumps(
                            record.get("exit_evidence", {}),
                            ensure_ascii=False,
                        ),
                        paper_trade_id,
                    ),
                )

    def refresh_paper_campaign(self, campaign_id: str) -> Mapping[str, Any]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        target_sample_count,
                        count(*) FILTER (
                            WHERE trade.status IN ('closed', 'exit_unfilled')
                        )
                    FROM banxia.paper_campaign AS campaign
                    LEFT JOIN banxia.paper_trade AS trade
                      ON trade.campaign_id = campaign.campaign_id
                    WHERE campaign.campaign_id = %s
                    GROUP BY campaign.target_sample_count
                    """,
                    (campaign_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("paper campaign not found")
                completed = int(row[1])
                cursor.execute(
                    """
                    UPDATE banxia.paper_campaign
                    SET
                        status = CASE
                            WHEN status = 'running'
                             AND %s >= target_sample_count
                            THEN 'completed'
                            ELSE status
                        END,
                        completed_at = CASE
                            WHEN status = 'running'
                             AND %s >= target_sample_count
                            THEN COALESCE(completed_at, now())
                            ELSE completed_at
                        END,
                        updated_at = now()
                    WHERE campaign_id = %s
                    """,
                    (completed, completed, campaign_id),
                )
        return self.paper_campaign_summary(campaign_id)

    def paper_campaign_summary(
        self,
        campaign_id: str,
    ) -> Mapping[str, Any]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        campaign.code,
                        campaign.strategy_id,
                        campaign.started_on,
                        campaign.target_sample_count,
                        campaign.status,
                        campaign.completed_at,
                        count(*) FILTER (
                            WHERE trade.status IN ('closed', 'exit_unfilled')
                        ) AS completed_samples,
                        count(*) FILTER (WHERE trade.positive) AS positives,
                        count(*) FILTER (WHERE trade.status = 'open') AS open,
                        count(*) FILTER (
                            WHERE trade.status IN (
                                'entry_data_missing',
                                'exit_data_missing'
                            )
                        ) AS missing,
                        count(*) FILTER (WHERE trade.status = 'rejected')
                            AS rejected,
                        avg(trade.net_return_pct) FILTER (
                            WHERE trade.net_return_pct IS NOT NULL
                        ) AS average_return,
                        min(trade.net_return_pct) FILTER (
                            WHERE trade.net_return_pct IS NOT NULL
                        ) AS worst_return
                    FROM banxia.paper_campaign AS campaign
                    LEFT JOIN banxia.paper_trade AS trade
                      ON trade.campaign_id = campaign.campaign_id
                    WHERE campaign.campaign_id = %s
                    GROUP BY
                        campaign.code,
                        campaign.strategy_id,
                        campaign.started_on,
                        campaign.target_sample_count,
                        campaign.status,
                        campaign.completed_at
                    """,
                    (campaign_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise ValueError("paper campaign not found")
        completed, positives = int(row[6]), int(row[7])
        target = int(row[3])
        lower, upper = _wilson(positives, completed)
        return {
            "campaign_id": campaign_id,
            "code": str(row[0]),
            "strategy_id": str(row[1]),
            "started_on": row[2].isoformat(),
            "target_sample_count": target,
            "status": str(row[4]),
            "completed_at": row[5].isoformat() if row[5] else None,
            "completed_sample_count": completed,
            "remaining_sample_count": max(0, target - completed),
            "positive_count": positives,
            "positive_rate_pct": (
                round(100 * positives / completed, 2)
                if completed
                else None
            ),
            "wilson_95_lower_pct": (
                round(100 * lower, 2) if completed else None
            ),
            "wilson_95_upper_pct": (
                round(100 * upper, 2) if completed else None
            ),
            "open_trade_count": int(row[8]),
            "missing_data_count": int(row[9]),
            "rejected_count": int(row[10]),
            "average_net_return_pct": _number(row[11]),
            "worst_net_return_pct": _number(row[12]),
        }

    def list_paper_trades(
        self,
        campaign_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid paper trade pagination")
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        paper_trade_id, plan_id, symbol, name, industry,
                        reference_date, entry_date, exit_date, status,
                        rejection_reason, entry_time, entry_price,
                        entry_sample_price, shares, target_price, exit_time,
                        exit_price, exit_reason, net_return_pct, positive,
                        entry_evidence, exit_evidence, created_at, updated_at
                    FROM banxia.paper_trade
                    WHERE campaign_id = %s
                    ORDER BY entry_date DESC, symbol
                    LIMIT %s OFFSET %s
                    """,
                    (campaign_id, limit, offset),
                )
                rows = cursor.fetchall()
        keys = (
            "paper_trade_id",
            "plan_id",
            "symbol",
            "name",
            "industry",
            "reference_date",
            "entry_date",
            "exit_date",
            "status",
            "rejection_reason",
            "entry_time",
            "entry_price",
            "entry_sample_price",
            "shares",
            "target_price",
            "exit_time",
            "exit_price",
            "exit_reason",
            "net_return_pct",
            "positive",
            "entry_evidence",
            "exit_evidence",
            "created_at",
            "updated_at",
        )
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            for key in ("paper_trade_id", "plan_id"):
                item[key] = str(item[key])
            for key in ("reference_date", "entry_date", "exit_date"):
                if item[key] is not None:
                    item[key] = item[key].isoformat()
            for key in ("entry_time", "exit_time"):
                if item[key] is not None:
                    item[key] = item[key].isoformat()
            for key in (
                "entry_price",
                "entry_sample_price",
                "target_price",
                "exit_price",
                "net_return_pct",
            ):
                item[key] = _number(item[key])
            for key in ("created_at", "updated_at"):
                item[key] = item[key].isoformat()
            item["entry_evidence"] = dict(item["entry_evidence"])
            item["exit_evidence"] = dict(item["exit_evidence"])
            result.append(item)
        return result
