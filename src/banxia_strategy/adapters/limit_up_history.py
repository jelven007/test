from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterable, Mapping, Optional, Sequence


LIMIT_UP_SORT_COLUMNS = {
    "trade_date": "history.trade_date",
    "symbol": "history.symbol",
    "name": "history.name",
    "board": "history.board",
    "industry": "history.industry",
    "close": "history.close",
    "amount_cny": "history.amount_cny",
    "total_market_cap_cny": "history.total_market_cap_cny",
    "float_market_cap_cny": "history.float_market_cap_cny",
    "turnover_pct": "history.turnover_pct",
    "amplitude_pct": "history.amplitude_pct",
    "open_change_pct": "history.open_change_pct",
    "change_pct": "history.change_pct",
    "return_5d_pct": "history.return_5d_pct",
    "consecutive_limit_days": "history.consecutive_limit_days",
}


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class LimitUpHistoryMixin:
    """PostgreSQL persistence and filtered reads for daily limit-up facts."""

    @contextmanager
    def limit_up_history_lock(self):
        # A dedicated connection holds the session lock across collection and commit.
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(20250101, 1635)")
                if not cursor.fetchone()[0]:
                    raise RuntimeError("涨停历史同步正在运行")
                try:
                    yield
                finally:
                    cursor.execute("SELECT pg_advisory_unlock(20250101, 1635)")

    def begin_limit_up_history_sync(
        self,
        start_date: date,
        end_date: date,
    ) -> str:
        run_id = str(uuid.uuid4())
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO banxia.limit_up_history_sync (
                        run_id, requested_start, requested_end, status
                    ) VALUES (%s, %s, %s, 'running')
                    """,
                    (run_id, start_date, end_date),
                )
        return run_id

    def complete_limit_up_history_sync(
        self,
        run_id: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        universe_count: int,
        history_count: int,
        missing_symbols: Sequence[str],
        successful_symbols: Sequence[str],
        coverage: Sequence[Mapping[str, Any]],
        effective_end: date,
    ) -> int:
        prepared = tuple(self._limit_up_row(item) for item in rows)
        status = "partial" if missing_symbols else "succeeded"
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT requested_start FROM banxia.limit_up_history_sync "
                    "WHERE run_id = %s AND status = 'running' FOR UPDATE", (run_id,),
                )
                run = cursor.fetchone()
                if run is None:
                    raise ValueError("同步任务不存在或已结束")
                cursor.execute(
                    "DELETE FROM banxia.limit_up_history WHERE trade_date BETWEEN %s AND %s "
                    "AND symbol = ANY(%s)", (run[0], effective_end, list(successful_symbols)),
                )
                if prepared:
                    cursor.executemany(
                        """
                        INSERT INTO banxia.limit_up_history (
                            trade_date, instrument_id, symbol, name, exchange,
                            board, industry, open, high, low, close,
                            previous_close, limit_price, volume_hands,
                            amount_cny, total_shares, float_shares,
                            total_market_cap_cny, float_market_cap_cny,
                            turnover_pct, amplitude_pct, open_change_pct,
                            change_pct, return_5d_pct,
                            consecutive_limit_days, capital_as_of_date,
                            classification_as_of_date, source, collected_at,
                            raw
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
                        )
                        ON CONFLICT (trade_date, instrument_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            exchange = EXCLUDED.exchange,
                            board = EXCLUDED.board,
                            industry = EXCLUDED.industry,
                            open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            previous_close = EXCLUDED.previous_close,
                            limit_price = EXCLUDED.limit_price,
                            volume_hands = EXCLUDED.volume_hands,
                            amount_cny = EXCLUDED.amount_cny,
                            total_shares = EXCLUDED.total_shares,
                            float_shares = EXCLUDED.float_shares,
                            total_market_cap_cny =
                                EXCLUDED.total_market_cap_cny,
                            float_market_cap_cny =
                                EXCLUDED.float_market_cap_cny,
                            turnover_pct = EXCLUDED.turnover_pct,
                            amplitude_pct = EXCLUDED.amplitude_pct,
                            open_change_pct = EXCLUDED.open_change_pct,
                            change_pct = EXCLUDED.change_pct,
                            return_5d_pct = EXCLUDED.return_5d_pct,
                            consecutive_limit_days =
                                EXCLUDED.consecutive_limit_days,
                            capital_as_of_date = EXCLUDED.capital_as_of_date,
                            classification_as_of_date =
                                EXCLUDED.classification_as_of_date,
                            source = EXCLUDED.source,
                            collected_at = EXCLUDED.collected_at,
                            raw = EXCLUDED.raw
                        """,
                        prepared,
                    )
                cursor.execute(
                    """
                    UPDATE banxia.limit_up_history_sync
                    SET status = %s,
                        requested_end = GREATEST(requested_start, %s),
                        universe_count = %s,
                        history_count = %s,
                        row_count = %s,
                        missing_symbols = %s::jsonb,
                        error_message = NULL,
                        finished_at = now()
                    WHERE run_id = %s AND status = 'running'
                    """,
                    (
                        status,
                        effective_end,
                        universe_count,
                        history_count,
                        len(prepared),
                        _json(list(missing_symbols)),
                        run_id,
                    ),
                )
                cursor.executemany(
                    """
                    INSERT INTO banxia.limit_up_history_coverage
                        (trade_date, run_id, status, universe_count, history_count, bar_count, row_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trade_date) DO UPDATE SET
                        run_id = EXCLUDED.run_id, status = EXCLUDED.status,
                        universe_count = EXCLUDED.universe_count,
                        history_count = EXCLUDED.history_count, bar_count = EXCLUDED.bar_count,
                        row_count = EXCLUDED.row_count, updated_at = now()
                    """,
                    [(item["trade_date"], run_id, status, universe_count, history_count,
                      item["bar_count"], item["row_count"]) for item in coverage],
                )
        return len(prepared)

    def fail_limit_up_history_sync(self, run_id: str, error: Any) -> None:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE banxia.limit_up_history_sync
                    SET status = 'failed',
                        error_message = %s,
                        finished_at = now()
                    WHERE run_id = %s AND status = 'running'
                    """,
                    (str(error)[:4000], run_id),
                )

    def latest_limit_up_history_date(self) -> Optional[date]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT MAX(requested_end) FROM banxia.limit_up_history_sync
                    WHERE status = 'succeeded' AND history_count > 0
                    AND EXISTS (
                        SELECT 1 FROM banxia.limit_up_history_sync
                        WHERE status = 'succeeded' AND requested_start <= DATE '2025-01-01'
                        AND history_count > 0
                    )"""
                )
                row = cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    def list_limit_up_history(
        self,
        *,
        start_date: date,
        end_date: date,
        query: Optional[str] = None,
        board: Optional[str] = None,
        industry: Optional[str] = None,
        include_unverified: bool = False,
        minimums: Optional[Mapping[str, float]] = None,
        maximums: Optional[Mapping[str, float]] = None,
        sort_by: str = "trade_date",
        direction: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if sort_by not in LIMIT_UP_SORT_COLUMNS:
            raise ValueError("涨停历史排序字段无效")
        if direction not in {"asc", "desc"}:
            raise ValueError("涨停历史排序方向无效")

        conditions = [
            "history.trade_date >= %s",
            "history.trade_date <= %s",
        ]
        parameters: list[Any] = [start_date, end_date]
        if not include_unverified:
            conditions.append(
                "history.raw->>'limit_rule_basis' IS DISTINCT FROM 'unverified_5pct_candidate'"
            )
        if query:
            conditions.append(
                "(history.symbol LIKE %s OR history.name ILIKE %s)"
            )
            pattern = f"%{query}%"
            parameters.extend((pattern, pattern))
        if board:
            conditions.append("history.board = %s")
            parameters.append(board)
        if industry:
            conditions.append("history.industry = %s")
            parameters.append(industry)

        for field, value in (minimums or {}).items():
            column = LIMIT_UP_SORT_COLUMNS.get(field)
            if column is None:
                raise ValueError("涨停历史筛选字段无效")
            conditions.append(f"{column} >= %s")
            parameters.append(value)
        for field, value in (maximums or {}).items():
            column = LIMIT_UP_SORT_COLUMNS.get(field)
            if column is None:
                raise ValueError("涨停历史筛选字段无效")
            conditions.append(f"{column} <= %s")
            parameters.append(value)

        where = " AND ".join(conditions)
        order = LIMIT_UP_SORT_COLUMNS[sort_by]
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT
                        COUNT(*),
                        COUNT(DISTINCT history.trade_date),
                        COUNT(DISTINCT history.instrument_id),
                        MIN(history.trade_date),
                        MAX(history.trade_date)
                    FROM banxia.limit_up_history history
                    WHERE {where}
                    """,
                    tuple(parameters),
                )
                summary = cursor.fetchone()
                cursor.execute(
                    f"""
                    SELECT
                        history.trade_date, history.symbol, history.name,
                        history.exchange, history.board, history.industry,
                        history.open, history.high, history.low, history.close,
                        history.previous_close, history.limit_price,
                        history.volume_hands, history.amount_cny,
                        history.total_market_cap_cny,
                        history.float_market_cap_cny, history.turnover_pct,
                        history.amplitude_pct, history.open_change_pct,
                        history.change_pct, history.return_5d_pct,
                        history.consecutive_limit_days,
                        history.capital_as_of_date,
                        history.classification_as_of_date,
                        history.source, history.collected_at,
                        history.raw->>'capital_basis',
                        history.raw->>'limit_rule_basis'
                    FROM banxia.limit_up_history history
                    WHERE {where}
                    ORDER BY {order} {direction.upper()} NULLS LAST,
                             history.trade_date DESC, history.symbol ASC
                    LIMIT %s OFFSET %s
                    """,
                    (*parameters, limit, offset),
                )
                rows = cursor.fetchall()
        return {
            "total": int(summary[0]),
            "trading_days": int(summary[1]),
            "stock_count": int(summary[2]),
            "first_date": summary[3].isoformat() if summary[3] else None,
            "last_date": summary[4].isoformat() if summary[4] else None,
            "items": [self._limit_up_mapping(row) for row in rows],
        }

    def limit_up_history_options(self) -> dict[str, Any]:
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        MIN(trade_date), MAX(trade_date), COUNT(*),
                        COUNT(DISTINCT trade_date),
                        COUNT(DISTINCT instrument_id)
                    FROM banxia.limit_up_history
                    """
                )
                coverage = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT industry, COUNT(*)
                    FROM banxia.limit_up_history
                    GROUP BY industry
                    ORDER BY industry
                    """
                )
                industries = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT
                        run_id, requested_start, requested_end, status,
                        row_count, universe_count, history_count,
                        missing_symbols, error_message, started_at, finished_at
                    FROM banxia.limit_up_history_sync
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                )
                sync = cursor.fetchone()
                cursor.execute(
                    """SELECT MIN(trade_date), MAX(trade_date), COUNT(*),
                    COUNT(*) FILTER (WHERE status = 'partial')
                    FROM banxia.limit_up_history_coverage"""
                )
                sessions = cursor.fetchone()
        return {
            "coverage_first_date": sessions[0].isoformat() if sessions[0] else None,
            "coverage_last_date": sessions[1].isoformat() if sessions[1] else None,
            "covered_sessions": int(sessions[2]),
            "partial_sessions": int(sessions[3]),
            "first_date": coverage[0].isoformat() if coverage[0] else None,
            "last_date": coverage[1].isoformat() if coverage[1] else None,
            "total": int(coverage[2]),
            "trading_days": int(coverage[3]),
            "stock_count": int(coverage[4]),
            "industries": [
                {"name": str(row[0]), "count": int(row[1])}
                for row in industries
            ],
            "latest_sync": (
                {
                    "run_id": str(sync[0]),
                    "requested_start": sync[1].isoformat(),
                    "requested_end": sync[2].isoformat(),
                    "status": str(sync[3]),
                    "row_count": (
                        int(sync[4]) if sync[4] is not None else None
                    ),
                    "universe_count": (
                        int(sync[5]) if sync[5] is not None else None
                    ),
                    "history_count": (
                        int(sync[6]) if sync[6] is not None else None
                    ),
                    "missing_symbols": list(sync[7] or []),
                    "error_message": sync[8],
                    "started_at": sync[9].isoformat(),
                    "finished_at": (
                        sync[10].isoformat() if sync[10] else None
                    ),
                }
                if sync
                else None
            ),
        }

    @staticmethod
    def _limit_up_row(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item["trade_date"],
            item["instrument_id"],
            item["symbol"],
            item["name"],
            item["exchange"],
            item["board"],
            item["industry"],
            item["open"],
            item["high"],
            item["low"],
            item["close"],
            item["previous_close"],
            item["limit_price"],
            item.get("volume_hands"),
            item.get("amount_cny"),
            item.get("total_shares"),
            item.get("float_shares"),
            item.get("total_market_cap_cny"),
            item.get("float_market_cap_cny"),
            item.get("turnover_pct"),
            item["amplitude_pct"],
            item["open_change_pct"],
            item["change_pct"],
            item.get("return_5d_pct"),
            item.get("consecutive_limit_days", 1),
            item.get("capital_as_of_date"),
            item["classification_as_of_date"],
            item.get("source", "mootdx"),
            item["collected_at"],
            _json(item.get("raw") or {}),
        )

    @staticmethod
    def _limit_up_mapping(row: Sequence[Any]) -> dict[str, Any]:
        return {
            "trade_date": row[0].isoformat(),
            "symbol": str(row[1]),
            "name": str(row[2]),
            "exchange": str(row[3]),
            "board": str(row[4]),
            "industry": str(row[5]),
            "open": float(row[6]),
            "high": float(row[7]),
            "low": float(row[8]),
            "close": float(row[9]),
            "previous_close": float(row[10]),
            "limit_price": float(row[11]),
            "volume_hands": float(row[12]) if row[12] is not None else None,
            "amount_cny": float(row[13]) if row[13] is not None else None,
            "total_market_cap_cny": (
                float(row[14]) if row[14] is not None else None
            ),
            "float_market_cap_cny": (
                float(row[15]) if row[15] is not None else None
            ),
            "turnover_pct": (
                float(row[16]) if row[16] is not None else None
            ),
            "amplitude_pct": float(row[17]),
            "open_change_pct": float(row[18]),
            "change_pct": float(row[19]),
            "return_5d_pct": (
                float(row[20]) if row[20] is not None else None
            ),
            "consecutive_limit_days": int(row[21]),
            "capital_as_of_date": (
                row[22].isoformat() if row[22] else None
            ),
            "classification_as_of_date": row[23].isoformat(),
            "source": str(row[24]),
            "collected_at": row[25].isoformat(),
            "capital_basis": row[26],
            "limit_rule_basis": row[27],
        }
