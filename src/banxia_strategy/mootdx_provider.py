from __future__ import annotations

import math
import tempfile
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


Server = Tuple[str, int]
ClientFactory = Callable[[Server], Any]


DEFAULT_SERVERS: Sequence[Server] = tuple(
    (f"117.34.114.{suffix}", 7709)
    for suffix in (14, 15, 16, 17, 18, 20, 27)
)

MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
GENERIC_CONCEPTS = {
    "ST板块",
    "次新股",
    "含B股",
    "含GDR",
    "含H股",
    "含可转债",
    "通达信88",
}
KLINE_FREQUENCIES = {
    "day": 9,
    "week": 5,
    "month": 6,
    "year": 11,
}


def _limit_price(previous_close: float, name: str = "") -> float:
    ratio = Decimal("1.05") if "ST" in name.upper() or "退" in name else Decimal("1.10")
    value = Decimal(str(previous_close)) * ratio
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _at_price_limit(price: Any, limit_price: float) -> bool:
    try:
        return float(price) >= limit_price - 0.001
    except (TypeError, ValueError):
        return False


def _bar_date(bar: Dict[str, Any]) -> date:
    value = bar.get("datetime")
    if isinstance(value, datetime):
        return value.date()
    if value:
        return datetime.fromisoformat(str(value)[:19]).date()
    return date(int(bar["year"]), int(bar["month"]), int(bar["day"]))


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _minute_label(index: int) -> str:
    if index < 120:
        minutes = 9 * 60 + 31 + index
    else:
        minutes = 13 * 60 + 1 + index - 120
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _seal_statistics(
    prices: Iterable[Any],
    limit_price: float,
) -> Tuple[Optional[str], Optional[str], int]:
    flags = [_at_price_limit(value, limit_price) for value in prices]
    starts = [
        index
        for index, flag in enumerate(flags)
        if flag and (index == 0 or not flags[index - 1])
    ]
    if not starts:
        return None, None, 0
    break_count = sum(
        1
        for index in range(1, len(flags))
        if flags[index - 1] and not flags[index]
    )
    return _minute_label(starts[0]), _minute_label(starts[-1]), break_count


def _extract_pools(
    histories: Dict[str, List[Dict[str, Any]]],
    names: Dict[str, str],
) -> Tuple[Dict[date, List[Dict[str, Any]]], Dict[date, List[Dict[str, Any]]]]:
    limit_pools: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
    broken_pools: Dict[date, List[Dict[str, Any]]] = defaultdict(list)

    for code, raw_bars in histories.items():
        bars = sorted(raw_bars, key=_bar_date)
        name = names.get(code, "")
        limit_flags: List[bool] = []
        for index, bar in enumerate(bars):
            if index == 0:
                limit_flags.append(False)
                continue
            previous_close = float(bars[index - 1].get("close") or 0)
            if previous_close <= 0:
                limit_flags.append(False)
                continue
            limit_price = _limit_price(previous_close, name)
            closed_at_limit = _at_price_limit(bar.get("close"), limit_price)
            touched_limit = _at_price_limit(bar.get("high"), limit_price)
            limit_flags.append(closed_at_limit)

            session = _bar_date(bar)
            common = {
                "代码": code,
                "名称": name,
                "最新价": float(bar.get("close") or 0),
                "成交额": float(bar.get("amount") or 0),
                "流通市值": 0.0,
                "换手率": 0.0,
                "封板资金": 0.0,
                "_seal_amount_available": False,
                "首次封板时间": None,
                "最后封板时间": None,
                "炸板次数": 0,
                "所属行业": "其他",
                "_limit_price": limit_price,
                "_volume_hands": float(bar.get("vol") or bar.get("volume") or 0),
            }
            if closed_at_limit:
                board_count = 1
                cursor = index - 1
                while cursor >= 0 and limit_flags[cursor]:
                    board_count += 1
                    cursor -= 1
                limit_pools[session].append({**common, "连板数": board_count})
            elif touched_limit:
                broken_pools[session].append(common)

    return dict(limit_pools), dict(broken_pools)


class MootdxProvider:
    """Build limit-up pools from raw TongdaXin data exposed by mootdx."""

    source_name = "mootdx"

    def __init__(
        self,
        history_sessions: int = 40,
        workers: int = 7,
        servers: Optional[Sequence[Server]] = None,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        if client_factory is None:
            try:
                from mootdx.quotes import Quotes
            except ImportError as exc:
                raise RuntimeError(
                    "mootdx is not installed. Run: python3 -m pip install -e ."
                ) from exc

            def client_factory(server: Server) -> Any:
                return Quotes.factory(
                    market="std",
                    server=server,
                    timeout=5,
                    heartbeat=False,
                    auto_retry=False,
                    raise_exception=True,
                )

        self.history_sessions = max(10, history_sessions)
        self.workers = max(1, workers)
        self.servers = tuple(servers or DEFAULT_SERVERS)
        self._client_factory = client_factory
        self._calendar: Optional[List[date]] = None
        self._names: Dict[str, str] = {}
        self._histories: Dict[str, List[Dict[str, Any]]] = {}
        self._limit_pools: Optional[Dict[date, List[Dict[str, Any]]]] = None
        self._broken_pools: Optional[Dict[date, List[Dict[str, Any]]]] = None
        self._concepts: Dict[str, List[str]] = {}
        self._concept_sizes: Dict[str, int] = {}
        self._industry_codes: Dict[str, str] = {}
        self._analysis_session: Optional[date] = None
        self._enriched_sessions: set[date] = set()

    def _client(self, server: Server) -> Any:
        return self._client_factory(server)

    @staticmethod
    def _close(client: Any) -> None:
        try:
            client.close()
        except Exception:
            pass

    def _first_result(self, action: Callable[[Any], Any]) -> Any:
        last_error: Optional[Exception] = None
        for server in self.servers:
            client = None
            try:
                client = self._client(server)
                result = action(client)
                if result is None or getattr(result, "empty", False):
                    continue
                if isinstance(result, (bytes, list, tuple, dict)) and not result:
                    continue
                return result
            except Exception as exc:
                last_error = exc
            finally:
                if client is not None:
                    self._close(client)
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"No usable mootdx quote server{detail}")

    def trading_dates(self) -> Sequence[date]:
        if self._calendar is not None:
            return self._calendar

        frame = self._first_result(
            lambda client: client.index(symbol="000001", frequency=9, offset=800)
        )
        sessions = sorted({_bar_date(row) for row in frame.to_dict(orient="records")})
        if not sessions:
            raise RuntimeError("mootdx returned an empty trading calendar")

        candidate = sessions[-1] + timedelta(days=1)
        for _ in range(14):
            if candidate.weekday() < 5 and not self._is_holiday(candidate):
                sessions.append(candidate)
                break
            candidate += timedelta(days=1)
        self._calendar = sessions
        return sessions

    def kline_bars(
        self,
        symbol: str,
        period: str,
        end_date: date,
        *,
        limit: int = 120,
    ) -> List[Dict[str, Any]]:
        if period not in KLINE_FREQUENCIES:
            raise ValueError("K线周期必须是 day、week、month 或 year")
        if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("股票代码必须为六位数字")
        if not 1 <= limit <= 240:
            raise ValueError("K线数量必须为 1 至 240")

        frame = self._first_result(
            lambda client: client.bars(
                symbol=symbol,
                frequency=KLINE_FREQUENCIES[period],
                offset=800,
            )
        )
        result = []
        for raw in frame.to_dict(orient="records"):
            try:
                session = _bar_date(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if session > end_date:
                continue
            values = {
                key: _finite_number(raw.get(key))
                for key in ("open", "high", "low", "close")
            }
            if any(value is None or value <= 0 for value in values.values()):
                continue
            result.append(
                {
                    "date": session.isoformat(),
                    **values,
                    "volume": _finite_number(
                        raw.get("vol", raw.get("volume"))
                    ),
                    "amount": _finite_number(raw.get("amount")),
                }
            )
        result.sort(key=lambda item: item["date"])
        return result[-limit:]

    @staticmethod
    def _is_holiday(value: date) -> bool:
        try:
            from mootdx.utils.holiday import _holiday
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                calendar = _holiday()
                if calendar is None or calendar.empty or "中国" not in set(calendar["国家"]):
                    raise ValueError("mootdx 未返回有效休市日历")
                # mootdx's holiday() compares DatetimeIndex with datetime.date;
                # pandas 3 no longer coerces that comparison and misses holidays.
                closed = set(calendar[calendar["国家"] == "中国"].index.date)
                return value.weekday() >= 5 or value in closed
        except Exception as exc:
            raise RuntimeError("无法验证 mootdx 休市日历，停止推测下一交易日") from exc

    def _load_universe(self) -> None:
        if self._names:
            return

        def fetch(client: Any) -> List[Dict[str, Any]]:
            records: List[Dict[str, Any]] = []
            for market in (1, 0):
                count = int(client.stock_count(market))
                for start in range(0, count, 1000):
                    batch = client.client.get_security_list(market=market, start=start)
                    records.extend(batch or [])
            return records

        records = self._first_result(fetch)
        for row in records:
            code = str(row.get("code") or "").strip()
            if not code.startswith(MAIN_BOARD_PREFIXES):
                continue
            name = str(row.get("name") or "").replace("\x00", "").strip()
            self._names[code] = name
        if not self._names:
            raise RuntimeError("mootdx returned no Shanghai/Shenzhen main-board stocks")

    def _history_worker(
        self,
        server: Server,
        codes: Sequence[str],
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
        client = None
        histories: Dict[str, List[Dict[str, Any]]] = {}
        failed: List[str] = []
        try:
            client = self._client(server)
            for code in codes:
                try:
                    frame = client.bars(
                        symbol=code,
                        frequency=9,
                        offset=self.history_sessions,
                    )
                    if frame is not None and not frame.empty:
                        histories[code] = frame.to_dict(orient="records")
                except Exception:
                    failed.append(code)
        except Exception:
            failed.extend(codes)
        finally:
            if client is not None:
                self._close(client)
        return histories, failed

    def _load_histories(self) -> None:
        if self._limit_pools is not None:
            return
        self._load_universe()
        codes = sorted(self._names)
        server_count = min(self.workers, len(self.servers), len(codes))
        servers = self.servers[:server_count]
        partitions = [codes[index::server_count] for index in range(server_count)]

        failed: List[str] = []
        with ThreadPoolExecutor(max_workers=server_count) as executor:
            futures = [
                executor.submit(self._history_worker, server, partition)
                for server, partition in zip(servers, partitions)
            ]
            for future in futures:
                histories, worker_failed = future.result()
                self._histories.update(histories)
                failed.extend(worker_failed)

        if failed:
            retry_histories, _ = self._history_worker(servers[0], sorted(set(failed)))
            self._histories.update(retry_histories)
        if not self._histories:
            raise RuntimeError("mootdx returned no daily bars for the stock universe")

        self._limit_pools, self._broken_pools = _extract_pools(
            self._histories,
            self._names,
        )
        self._load_classifications()

    def _download_server_file(self, filename: str) -> bytes:
        def fetch(client: Any) -> bytes:
            meta = client.client.get_block_info_meta(filename)
            size = int((meta or {}).get("size") or 0)
            if size <= 0:
                return b""
            content = bytearray()
            chunk_size = 0x7530
            for start in range(0, size, chunk_size):
                content.extend(
                    client.client.get_block_info(
                        filename,
                        start,
                        min(chunk_size, size - start),
                    )
                )
            return bytes(content[:size])

        return self._first_result(fetch)

    def _load_classifications(self) -> None:
        try:
            self._parse_concepts(self._download_server_file("block_gn.dat"))
        except Exception:
            self._concepts = {}
            self._concept_sizes = {}
        try:
            text = self._download_server_file("tdxhy.cfg").decode("gbk")
            for line in text.splitlines():
                parts = line.split("|")
                if len(parts) >= 3 and parts[1] in self._names:
                    self._industry_codes[parts[1]] = parts[2]
        except Exception:
            self._industry_codes = {}

    def _parse_concepts(self, content: bytes) -> None:
        from tdxpy.reader.block_reader import BlockReader, BlockReader_TYPE_FLAT

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "block_gn.dat"
            path.write_bytes(content)
            records = BlockReader().get_data(str(path), BlockReader_TYPE_FLAT)

        concepts: Dict[str, List[str]] = defaultdict(list)
        sizes: Counter[str] = Counter()
        for row in records:
            concept = str(row.get("blockname") or "").strip()
            code = str(row.get("code") or "").strip()
            if (
                not concept
                or concept in GENERIC_CONCEPTS
                or code not in self._names
            ):
                continue
            concepts[code].append(concept)
            sizes[concept] += 1
        self._concepts = dict(concepts)
        self._concept_sizes = dict(sizes)

    def _assign_themes(self, target: date) -> None:
        assert self._limit_pools is not None
        sessions = sorted(session for session in self._limit_pools if session <= target)[-5:]
        daily_counts: Dict[date, Counter[str]] = {}
        for session in sessions:
            counts: Counter[str] = Counter()
            for row in self._limit_pools.get(session, []):
                counts.update(self._concepts.get(row["代码"], []))
            daily_counts[session] = counts

        for session in sessions:
            for row in self._limit_pools.get(session, []):
                code = row["代码"]
                concepts = self._concepts.get(code, [])
                if concepts:
                    concept = max(
                        concepts,
                        key=lambda item: (
                            daily_counts.get(target, {}).get(item, 0) * 3,
                            sum(counts.get(item, 0) for counts in daily_counts.values()),
                            -self._concept_sizes.get(item, 10_000),
                            item,
                        ),
                    )
                    row["所属行业"] = concept
                else:
                    row["所属行业"] = self._industry_codes.get(code, "其他")

    def _detail_worker(
        self,
        server: Server,
        codes: Sequence[str],
        session: date,
        current_session: bool,
    ) -> Dict[str, Dict[str, Any]]:
        client = None
        details: Dict[str, Dict[str, Any]] = {}
        try:
            client = self._client(server)
            quotes: Dict[str, Dict[str, Any]] = {}
            if current_session:
                for start in range(0, len(codes), 80):
                    frame = client.quotes(symbol=list(codes[start : start + 80]))
                    if frame is not None and not frame.empty:
                        quotes.update(
                            {
                                str(row["code"]): row
                                for row in frame.to_dict(orient="records")
                            }
                        )

            for code in codes:
                item: Dict[str, Any] = {"quote": quotes.get(code)}
                try:
                    finance = client.finance(symbol=code)
                    if finance is not None and not finance.empty:
                        item["finance"] = finance.iloc[0].to_dict()
                except Exception:
                    pass
                try:
                    minutes = client.minutes(
                        symbol=code,
                        date=session.strftime("%Y%m%d"),
                    )
                    if minutes is not None and not minutes.empty:
                        item["prices"] = minutes["price"].tolist()
                except Exception:
                    pass
                details[code] = item
        finally:
            if client is not None:
                self._close(client)
        return details

    def _enrich_session(self, session: date) -> None:
        if session in self._enriched_sessions:
            return
        assert self._limit_pools is not None
        rows = self._limit_pools.get(session, [])
        if not rows:
            return
        self._assign_themes(session)

        codes = [row["代码"] for row in rows]
        latest_session = max(self._limit_pools)
        server_count = min(self.workers, len(self.servers), len(codes))
        servers = self.servers[:server_count]
        partitions = [codes[index::server_count] for index in range(server_count)]
        details: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=server_count) as executor:
            futures = [
                executor.submit(
                    self._detail_worker,
                    server,
                    partition,
                    session,
                    session == latest_session,
                )
                for server, partition in zip(servers, partitions)
            ]
            for future in futures:
                try:
                    details.update(future.result())
                except Exception:
                    continue
        missing = [code for code in codes if code not in details]
        if missing:
            try:
                details.update(
                    self._detail_worker(
                        servers[0],
                        missing,
                        session,
                        session == latest_session,
                    )
                )
            except Exception:
                pass

        for row in rows:
            detail = details.get(row["代码"], {})
            finance = detail.get("finance") or {}
            float_shares = float(finance.get("liutongguben") or 0)
            if float_shares > 0:
                row["流通市值"] = float_shares * row["最新价"]
                row["换手率"] = row["_volume_hands"] * 100 / float_shares * 100

            first, last, breaks = _seal_statistics(
                detail.get("prices") or [],
                row["_limit_price"],
            )
            row["首次封板时间"] = first
            row["最后封板时间"] = last
            row["炸板次数"] = breaks

            quote = detail.get("quote") or {}
            if "bid1" in quote and "bid_vol1" in quote:
                row["_seal_amount_available"] = True
            if _at_price_limit(quote.get("bid1"), row["_limit_price"]):
                bid_hands = float(quote.get("bid_vol1") or 0)
                row["封板资金"] = bid_hands * 100 * row["_limit_price"]

        self._enriched_sessions.add(session)

    def limit_up_pool(self, session: date) -> List[Dict[str, Any]]:
        self._load_histories()
        assert self._limit_pools is not None
        if self._limit_pools.get(session):
            if self._analysis_session is None:
                self._analysis_session = session
                self._enrich_session(session)
        return [dict(row) for row in self._limit_pools.get(session, [])]

    def broken_board_pool(self, session: date) -> List[Dict[str, Any]]:
        self._load_histories()
        assert self._broken_pools is not None
        return [dict(row) for row in self._broken_pools.get(session, [])]
