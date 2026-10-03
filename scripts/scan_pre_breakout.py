#!/usr/bin/env python3
"""Scan A-share universe for the "cold pre-breakout" pattern anchored at 2026-09-30."""
from __future__ import annotations

import csv
import logging
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from banxia_strategy.limit_up_collector import daily_bars  # noqa: E402
from banxia_strategy.mootdx_provider import MootdxProvider, _bar_date  # noqa: E402


LOG = logging.getLogger("pre-breakout")

ANCHOR = date(2026, 9, 30)
START_DATE = date(2026, 8, 15)
WINDOW = 20
OUTPUT = Path("/tmp/pre_breakout_candidates.csv")

NORTH_PREFIXES = ("4", "8", "9")  # 北交所
GEM_STAR_PREFIXES = ("300", "301", "688", "689")


def board_limit_threshold(symbol: str) -> float:
    """Return the daily-return percent that counts as a limit-up for this symbol."""
    if symbol.startswith(GEM_STAR_PREFIXES):
        return 19.5
    return 9.5


def eligible(security: dict) -> bool:
    name = security.get("name") or ""
    if "ST" in name.upper():
        return False
    symbol = security["symbol"]
    if symbol.startswith(NORTH_PREFIXES):
        return False
    return True


def analyze_bars(symbol: str, name: str, bars: list[dict]) -> dict | None:
    """Return a candidate record if symbol matches the pre-breakout template."""
    anchor_bars = [bar for bar in bars if _bar_date(bar) <= ANCHOR]
    if len(anchor_bars) < WINDOW:
        return None
    window = anchor_bars[-WINDOW:]

    closes = [float(bar.get("close") or 0) for bar in window]
    if any(c <= 0 for c in closes):
        return None
    highs = [float(bar.get("high") or 0) for bar in window]
    opens = [float(bar.get("open") or 0) for bar in window]
    amounts = [float(bar.get("amount") or 0) for bar in window]
    volumes = [float(bar.get("vol") or bar.get("volume") or 0) for bar in window]
    sessions = [_bar_date(bar) for bar in window]

    # Ensure the window actually ends on or around anchor (no large gaps allowed).
    if sessions[-1] != ANCHOR:
        return None

    mean_close = sum(closes) / WINDOW
    if mean_close > 20.0:
        return None

    # Count limit-ups: use previous close as denominator, pct_change >= threshold.
    threshold = board_limit_threshold(symbol)
    limit_count = 0
    for idx in range(1, WINDOW):
        prev_close = closes[idx - 1]
        if prev_close <= 0:
            continue
        day_ret_pct = (closes[idx] / prev_close - 1) * 100
        if day_ret_pct >= threshold:
            limit_count += 1
    if limit_count > 0:
        return None

    # Cumulative return over the 20-session window: anchor close vs. 20-sessions-ago close.
    # closes[0] is "20 trading days ago", closes[-1] is the anchor.
    cum_ret_pct = (closes[-1] / closes[0] - 1) * 100
    if cum_ret_pct > 15.0:
        return None
    # Per spec rule 5: 20-day cumulative return <= 0% (strict simplified max-drawdown).
    if cum_ret_pct > 0.0:
        return None

    # Max drawdown within window (closes-based, anchored at window start).
    peak = closes[0]
    mdd = 0.0
    for close in closes:
        if close > peak:
            peak = close
        drawdown = close / peak - 1
        if drawdown < mdd:
            mdd = drawdown

    # Average daily turnover over the 20 sessions.
    mean_amount = sum(amounts) / WINDOW
    if mean_amount > 5e8:
        return None

    # Volume ratio: last 3 sessions vs. previous 17 sessions.
    last3_vol = volumes[-3:]
    prev17_vol = volumes[:-3]
    if not prev17_vol or min(prev17_vol) < 0:
        return None
    mean_last3 = sum(last3_vol) / 3
    mean_prev17 = sum(prev17_vol) / 17
    if mean_prev17 <= 0:
        return None
    vol_ratio = mean_last3 / mean_prev17
    if vol_ratio < 1.2:
        return None

    return {
        "symbol": symbol,
        "name": name,
        "mean_close_20d": round(mean_close, 4),
        "cum_ret_20d_pct": round(cum_ret_pct, 4),
        "mdd_20d_pct": round(mdd * 100, 4),
        "mean_amount_20d_cny_1e8": round(mean_amount / 1e8, 4),
        "vol_ratio_3_17": round(vol_ratio, 4),
        "latest_close": round(closes[-1], 4),
        "latest_amount_cny_1e8": round(amounts[-1] / 1e8, 4),
    }


def worker(number: int, partition: list[dict], provider: MootdxProvider) -> tuple[list[dict], Counter]:
    client = None
    active_server = None
    hits: list[dict] = []
    stats: Counter = Counter()

    for position, security in enumerate(partition):
        symbol = security["symbol"]
        name = security["name"]
        bars = None
        for attempt in range(3):
            server = provider.servers[(number + attempt) % len(provider.servers)]
            try:
                if client is None or active_server != server:
                    if client is not None:
                        provider._close(client)
                    client = provider._client(server)
                    active_server = server
                bars = daily_bars(client, symbol, START_DATE)
                break
            except Exception as exc:
                if client is not None:
                    provider._close(client)
                client = None
                if attempt == 2:
                    stats["fetch_error"] += 1
                    LOG.warning("fetch error symbol=%s err=%s", symbol, exc)
        if bars is None:
            continue
        try:
            record = analyze_bars(symbol, name, bars)
        except Exception as exc:
            stats["analyze_error"] += 1
            LOG.warning("analyze error symbol=%s err=%s", symbol, exc)
            continue
        if record is None:
            stats["filtered"] += 1
        else:
            stats["hit"] += 1
            hits.append(record)
        if (position + 1) % 100 == 0:
            LOG.info("worker=%s progress=%s/%s hits=%s stats=%s",
                     number, position + 1, len(partition), len(hits), dict(stats))
    if client is not None:
        provider._close(client)
    return hits, stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    provider = MootdxProvider()
    all_securities = provider.securities()
    universe = [sec for sec in all_securities if eligible(sec)]
    LOG.info("universe size=%s (filtered from %s)", len(universe), len(all_securities))

    worker_count = max(1, min(provider.workers, len(provider.servers), len(universe)))
    partitions = [universe[i::worker_count] for i in range(worker_count)]

    hits: list[dict] = []
    stats: Counter = Counter()
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(worker, i, part, provider) for i, part in enumerate(partitions)]
        for fut in as_completed(futures):
            res_hits, res_stats = fut.result()
            hits.extend(res_hits)
            stats.update(res_stats)

    # Sort hits by vol_ratio_3_17 desc for output consistency.
    hits.sort(key=lambda row: row["vol_ratio_3_17"], reverse=True)

    fieldnames = [
        "symbol", "name", "mean_close_20d", "cum_ret_20d_pct", "mdd_20d_pct",
        "mean_amount_20d_cny_1e8", "vol_ratio_3_17", "latest_close",
        "latest_amount_cny_1e8",
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(hits)

    LOG.info("stats=%s total_hits=%s", dict(stats), len(hits))
    print(f"\n>>> 符合条件的记录总数: {len(hits)}")
    print(f">>> 输出文件: {OUTPUT}")

    headers = [
        "代码", "名称", "20日均价(元)", "20日累涨(%)", "20日最大回撤(%)",
        "20日日均成交额(亿元)", "前3日/前17日量比", "最新收盘(元)", "最新成交额(亿元)",
    ]
    print("\n>>> 按量比降序前 30 行:")
    sep = " | "
    print(sep.join(headers))
    print(sep.join(["-" * len(h) for h in headers]))
    for row in hits[:30]:
        values = [
            row["symbol"], row["name"],
            f"{row['mean_close_20d']:.2f}",
            f"{row['cum_ret_20d_pct']:+.2f}",
            f"{row['mdd_20d_pct']:+.2f}",
            f"{row['mean_amount_20d_cny_1e8']:.2f}",
            f"{row['vol_ratio_3_17']:.2f}",
            f"{row['latest_close']:.2f}",
            f"{row['latest_amount_cny_1e8']:.2f}",
        ]
        print(sep.join(str(v) for v in values))


if __name__ == "__main__":
    main()
