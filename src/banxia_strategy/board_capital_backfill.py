"""Build point-in-time board market-cap history from stored daily bars."""
from __future__ import annotations

import argparse
import logging
from bisect import bisect_right
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from .adapters.clickhouse import ClickHouseMarketHistoryStore
from .mootdx_provider import MootdxProvider, _bar_date, _finite_number, _json_value
from .storage_config import StorageSettings


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_START_DATE = date(2016, 1, 1)
BOARD_DEFINITIONS = {
    ("sh", "main"): ("sh_main", "沪市主板"),
    ("sz", "main"): ("sz_main", "深市主板"),
    ("sh", "star"): ("sh_star", "科创板（沪市）"),
    ("sz", "gem"): ("sz_gem", "创业板（深市）"),
}


@dataclass(frozen=True)
class CapitalBackfillResult:
    universe_count: int
    completed_count: int
    excluded_symbols: tuple[str, ...]
    failed_symbols: tuple[str, ...]
    daily_row_count: int
    start_date: date
    end_date: date


class CapitalResolver:
    def __init__(
        self,
        finance: Mapping[str, Any],
        actions: Sequence[Mapping[str, Any]],
    ) -> None:
        self.current = tuple(
            value if value is not None and value > 0 else None
            for value in (
                _finite_number(finance.get("zongguben")),
                _finite_number(finance.get("liutongguben")),
            )
        )
        events = []
        for action in actions:
            try:
                stamp = _bar_date(dict(action))
            except (KeyError, TypeError, ValueError):
                continue
            before = tuple(
                _finite_number(action.get(key))
                for key in ("qianzongguben", "panqianliutong")
            )
            after = tuple(
                _finite_number(action.get(key))
                for key in ("houzongguben", "panhouliutong")
            )
            if all(value is not None and value > 0 for value in (*before, *after)):
                events.append(
                    (
                        stamp,
                        (before[0] * 10_000, before[1] * 10_000),
                        (after[0] * 10_000, after[1] * 10_000),
                    )
                )
        self.events = sorted(events, key=lambda item: item[0])
        self.dates = [item[0] for item in self.events]

    def resolve(
        self,
        session: date,
    ) -> tuple[Optional[float], Optional[float], bool]:
        index = bisect_right(self.dates, session)
        if index < len(self.events):
            total, floating = self.events[index][1]
            return total, floating, False
        if self.events:
            total, floating = self.events[-1][2]
            return total, floating, False
        return self.current[0], self.current[1], True


class BoardCapitalBackfill:
    def __init__(
        self,
        *,
        provider: MootdxProvider,
        store: ClickHouseMarketHistoryStore,
        start_date: date = DEFAULT_START_DATE,
        end_date: Optional[date] = None,
        batch_size: int = 30,
        workers: Optional[int] = None,
        progress: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.start_date = start_date
        self.end_date = end_date or datetime.now(SHANGHAI).date()
        self.batch_size = max(1, batch_size)
        self.workers = max(
            1,
            min(workers or provider.workers, len(provider.servers)),
        )
        self.progress = progress or (lambda _value: None)
        if self.start_date > self.end_date:
            raise ValueError("start_date cannot be later than end_date")

    def _fetch_profiles(
        self,
        batch_number: int,
        securities: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        profiles: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        client = None
        active_server = None
        try:
            for security in securities:
                symbol = str(security["symbol"])
                for attempt in range(3):
                    server = self.provider.servers[
                        (batch_number + attempt) % len(self.provider.servers)
                    ]
                    try:
                        if client is None or active_server != server:
                            if client is not None:
                                self.provider._close(client)
                            client = self.provider._client(server)
                            active_server = server
                        finance_frame = client.finance(symbol=symbol)
                        if finance_frame is None or finance_frame.empty:
                            raise RuntimeError("finance response unavailable")
                        action_frame = client.xdxr(symbol=symbol)
                        profiles[symbol] = {
                            "finance": {
                                str(key): _json_value(value)
                                for key, value in finance_frame.iloc[0].to_dict().items()
                            },
                            "actions": [
                                {
                                    str(key): _json_value(value)
                                    for key, value in row.items()
                                }
                                for row in (
                                    action_frame.to_dict(orient="records")
                                    if action_frame is not None and not action_frame.empty
                                    else []
                                )
                            ],
                        }
                        break
                    except Exception:
                        if client is not None:
                            self.provider._close(client)
                        client = None
                        active_server = None
                        if attempt == 2:
                            failures.append(symbol)
        finally:
            if client is not None:
                self.provider._close(client)
        return profiles, failures

    @staticmethod
    def _empty_aggregates(
        sessions: Sequence[date],
    ) -> dict[tuple[date, str], dict[str, float]]:
        return {
            (session, board_code): {
                "total": 0.0,
                "floating": 0.0,
                "stocks": 0,
                "total_stocks": 0,
                "float_stocks": 0,
                "estimated": 0,
            }
            for session in sessions
            for board_code, _label in BOARD_DEFINITIONS.values()
        }

    def _accumulate_security(
        self,
        security: Mapping[str, Any],
        profile: Mapping[str, Any],
        bars: Sequence[Mapping[str, Any]],
        sessions: Sequence[date],
        aggregates: dict[tuple[date, str], dict[str, float]],
    ) -> bool:
        board = BOARD_DEFINITIONS.get(
            (str(security["market"]), str(security["board"]))
        )
        if board is None or not bars:
            return False
        resolver = CapitalResolver(
            profile["finance"],
            profile["actions"],
        )
        ordered = sorted(bars, key=lambda item: item["trade_date"])
        cursor = 0
        close = None
        for session in sessions:
            while (
                cursor < len(ordered)
                and ordered[cursor]["trade_date"] <= session
            ):
                close = _finite_number(ordered[cursor].get("close"))
                cursor += 1
            if close is None:
                continue
            total_shares, float_shares, estimated = resolver.resolve(session)
            item = aggregates[(session, board[0])]
            item["stocks"] += 1
            if total_shares:
                item["total"] += close * total_shares
                item["total_stocks"] += 1
            if float_shares:
                item["floating"] += close * float_shares
                item["float_stocks"] += 1
            if estimated and (total_shares or float_shares):
                item["estimated"] += 1
        return True

    def run(self) -> CapitalBackfillResult:
        sessions = self.store.get_history_dates(
            start_date=self.start_date,
            end_date=self.end_date,
        )
        if not sessions:
            raise RuntimeError("stored daily history is empty")
        securities = [
            security
            for security in self.provider.securities()
            if (
                str(security["market"]),
                str(security["board"]),
            ) in BOARD_DEFINITIONS
        ]
        batches = [
            securities[index:index + self.batch_size]
            for index in range(0, len(securities), self.batch_size)
        ]
        aggregates = self._empty_aggregates(sessions)
        completed = 0
        excluded: list[str] = []
        failures: list[str] = []

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            batch_iter = iter(enumerate(batches))
            futures = {}
            for _ in range(self.workers):
                number, batch = next(batch_iter, (None, None))
                if number is not None:
                    futures[
                        executor.submit(self._fetch_profiles, number, batch)
                    ] = batch
            while futures:
                done, _pending = wait(
                    set(futures),
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    batch = futures.pop(future)
                    profiles, failed = future.result()
                    failures.extend(failed)
                    symbols = [str(item["symbol"]) for item in batch]
                    bars_by_symbol = self.store.get_history_bars_for_symbols(
                        symbols,
                        start_date=self.start_date,
                        end_date=self.end_date,
                    )
                    for security in batch:
                        symbol = str(security["symbol"])
                        profile = profiles.get(symbol)
                        if profile is None:
                            continue
                        bars = bars_by_symbol.get(symbol, ())
                        if not bars:
                            excluded.append(symbol)
                            continue
                        if self._accumulate_security(
                            security,
                            profile,
                            bars,
                            sessions,
                            aggregates,
                        ):
                            completed += 1
                    self.progress(
                        {
                            "universe_count": len(securities),
                            "completed_count": completed,
                            "excluded_count": len(excluded),
                            "failed_count": len(failures),
                        }
                    )
                    number, batch = next(batch_iter, (None, None))
                    if number is not None:
                        submitted = executor.submit(
                            self._fetch_profiles,
                            number,
                            batch,
                        )
                        futures[submitted] = batch

        rows = [
            {
                "trade_date": session,
                "board_code": board_code,
                "total_market_cap_cny": values["total"],
                "float_market_cap_cny": values["floating"],
                "stock_count": values["stocks"],
                "total_cap_stock_count": values["total_stocks"],
                "float_cap_stock_count": values["float_stocks"],
                "estimated_stock_count": values["estimated"],
                "source": "mootdx",
            }
            for (session, board_code), values in sorted(aggregates.items())
        ]
        self.store.upsert_board_capital_history(rows)
        return CapitalBackfillResult(
            universe_count=len(securities),
            completed_count=completed,
            excluded_symbols=tuple(sorted(set(excluded))),
            failed_symbols=tuple(sorted(set(failures))),
            daily_row_count=len(rows),
            start_date=self.start_date,
            end_date=self.end_date,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build four-board market-cap history",
    )
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--workers", type=int, default=7)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = StorageSettings.from_env()
    store = ClickHouseMarketHistoryStore(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        database=settings.clickhouse_database,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        send_receive_timeout=120,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("board-capital-backfill")

    def report(value: Mapping[str, Any]) -> None:
        logger.info(
            "progress completed=%s excluded=%s failed=%s universe=%s",
            value["completed_count"],
            value["excluded_count"],
            value["failed_count"],
            value["universe_count"],
        )

    try:
        result = BoardCapitalBackfill(
            provider=MootdxProvider(workers=args.workers),
            store=store,
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
            workers=args.workers,
            progress=report,
        ).run()
        logger.info("completed result=%s", result)
        return 1 if result.failed_symbols else 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
