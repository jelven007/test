from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .limit_up_history import build_limit_up_history_rows, industry_from_f10
from .mootdx_provider import _bar_date


logger = logging.getLogger(__name__)


def daily_bars(client: Any, symbol: str, start_date: date, *, index: bool = False) -> list[dict]:
    """Page backwards until the range plus five previous trading bars is covered."""
    records = {}
    position = 0
    while True:
        method = client.index if index else client.bars
        frame = method(symbol=symbol, frequency=9, start=position, offset=800)
        if frame is None:
            raise RuntimeError(f"missing daily-bar response: {symbol}")
        batch = frame.to_dict(orient="records")
        if not batch:
            break
        old_count = len(records)
        records.update({_bar_date(b): b for b in batch})
        if len(records) == old_count:
            raise RuntimeError(f"daily-bar pagination made no progress: {symbol}")
        if sum(stamp < start_date for stamp in records) >= 5 or len(batch) < 800:
            break
        position += len(batch)
    return [records[stamp] for stamp in sorted(records)]


def collect_limit_up_history(provider: Any, start_date: date, end_date: date) -> dict:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if start_date > end_date or end_date > now.date():
        raise ValueError("invalid history date range")
    # Never persist an intraday bar as a daily closing observation.
    cutoff = min(end_date, now.date() - timedelta(days=1) if now.hour < 15 else now.date())
    index_bars = provider._first_result(
        lambda client: daily_bars(client, "000001", start_date, index=True)
    )
    sessions = sorted({_bar_date(b) for b in index_bars if start_date <= _bar_date(b) <= cutoff})
    securities = provider.securities()
    if not sessions:
        return {
            "source": "mootdx", "start_date": start_date, "end_date": cutoff,
            "collected_at": now, "universe_count": len(securities), "history_count": 0,
            "missing_symbols": [], "successful_symbols": [], "coverage": [], "rows": [],
        }
    end = sessions[-1]
    worker_count = min(provider.workers, len(provider.servers), len(securities))

    def worker(number: int, partition: list[dict]) -> dict:
        client, active_server = None, None
        rows, successful, missing = [], [], []
        coverage = Counter()
        for position, security in enumerate(partition):
            symbol = security["symbol"]
            for attempt in range(3):
                server = provider.servers[(number + attempt) % len(provider.servers)]
                try:
                    if client is None or active_server != server:
                        if client is not None:
                            provider._close(client)
                        client, active_server = provider._client(server), server
                    bars = daily_bars(client, symbol, start_date)
                    finance_frame = client.finance(symbol=symbol)
                    if finance_frame is None or finance_frame.empty:
                        raise RuntimeError("finance response unavailable")
                    finance = finance_frame.to_dict(orient="records")[0]
                    actions = client.client.get_xdxr_info(1 if security["market"] == "sh" else 0, symbol)
                    if actions is None:
                        raise RuntimeError("corporate action response unavailable")
                    if not bars:
                        raise RuntimeError("daily history is empty")
                    derived = build_limit_up_history_rows(
                        histories={symbol: bars}, securities={symbol: security},
                        finances={symbol: finance}, actions={symbol: actions},
                        start_date=start_date, end_date=end, collected_at=now,
                    )
                    if derived:
                        text = client.F10(symbol=symbol, name="公司概况")
                        if isinstance(text, dict):
                            text = text.get("公司概况", "")
                        industry = industry_from_f10(text or "")
                        for row in derived:
                            row["industry"] = industry.split("-")[0].strip() if industry else "未分类"
                            row["raw"]["industry_full"] = industry
                    rows.extend(derived)
                    successful.append(symbol)
                    coverage.update({_bar_date(b) for b in bars if start_date <= _bar_date(b) <= end})
                    break
                except Exception as exc:
                    if client is not None:
                        provider._close(client)
                    client = None
                    if attempt == 2:
                        missing.append(symbol)
                        logger.warning("limit-up collection failed symbol=%s error=%s", symbol, exc)
            if (position + 1) % 50 == 0:
                logger.info("limit-up collection worker=%s progress=%s/%s rows=%s missing=%s",
                            number, position + 1, len(partition), len(rows), len(missing))
        if client is not None:
            provider._close(client)
        return {"rows": rows, "successful": successful, "missing": missing, "coverage": coverage}

    all_rows, successful, missing = [], [], []
    observed = Counter()
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(worker, i, securities[i::worker_count]) for i in range(worker_count)]
        for future in as_completed(futures):
            result = future.result()
            all_rows.extend(result["rows"])
            successful.extend(result["successful"])
            missing.extend(result["missing"])
            observed.update(result["coverage"])
    if not successful:
        raise RuntimeError("mootdx returned no usable stock histories")
    row_counts = Counter(row["trade_date"] for row in all_rows)
    return {
        "source": "mootdx", "start_date": start_date, "end_date": end,
        "collected_at": now, "universe_count": len(securities),
        "history_count": len(successful), "missing_symbols": sorted(missing),
        "successful_symbols": sorted(successful),
        "coverage": [{"trade_date": session, "bar_count": observed[session],
                      "row_count": row_counts[session]} for session in sessions],
        "rows": all_rows,
    }
