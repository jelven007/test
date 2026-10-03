"""Collect zt-pool (涨停池) and zb-pool (炸板池) history from akshare.

Writes per-day CSV.gz:
  - research/sentiment-features/zt_pool/YYYYMMDD.csv.gz  (涨停)
  - research/sentiment-features/zb_pool/YYYYMMDD.csv.gz  (炸板)

akshare limits:
  - stock_zt_pool_em: ~ last 20 trading days
  - stock_zt_pool_zbgc_em: last 30 trading days (hard limit)

Design: idempotent. If a day's file already exists we skip. For "today"
and the previous 1-2 days we re-download because akshare may update
intraday snapshots. Expected to be run as a daily cron job once the
market closes (>= 15:30).
"""
from __future__ import annotations

import gzip
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ZT_DIR = ROOT / "research" / "sentiment-features" / "zt_pool"
ZB_DIR = ROOT / "research" / "sentiment-features" / "zb_pool"

LOOKBACK_DAYS = 30  # try to pull everything akshare still exposes
RETRY_PER_DAY = 3
SLEEP_BETWEEN = 0.5


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def trading_days_window(end: date, lookback: int) -> list[date]:
    out = []
    cur = end
    while len(out) < lookback:
        if is_weekday(cur):
            out.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(out))


def _fetch(fn, tag: str, d: date) -> pd.DataFrame | None:
    date_str = d.strftime("%Y%m%d")
    for attempt in range(1, RETRY_PER_DAY + 1):
        try:
            df = fn(date=date_str)
            return df
        except ValueError as exc:
            # explicit "out of range" from akshare → don't retry
            print(f"  {tag} {date_str}: skip ({exc})")
            return None
        except Exception as exc:
            if attempt == RETRY_PER_DAY:
                print(f"  {tag} {date_str}: ERR after {RETRY_PER_DAY} retries ({exc})")
                return None
            time.sleep(1.0 * attempt)
    return None


def save_df(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        df.to_csv(f, index=False)


def collect(days: list[date], out_dir: Path, fn, tag: str, force_last_n: int = 2) -> dict[str, int]:
    stats = {"days_fetched": 0, "days_empty": 0, "days_cached": 0, "days_err": 0}
    force_set = set(days[-force_last_n:]) if force_last_n > 0 else set()
    for d in days:
        fp = out_dir / f"{d.strftime('%Y%m%d')}.csv.gz"
        if fp.exists() and d not in force_set:
            stats["days_cached"] += 1
            continue
        df = _fetch(fn, tag, d)
        if df is None:
            stats["days_err"] += 1
            continue
        if len(df) == 0:
            stats["days_empty"] += 1
            # Keep an empty marker so we don't retry non-trading days forever.
            # Only skip write when file not present; existing cached files stay.
            if not fp.exists():
                save_df(df, fp)
            continue
        save_df(df, fp)
        stats["days_fetched"] += 1
        print(f"  {tag} {d.strftime('%Y%m%d')}: {len(df)} rows -> {fp.name}")
        time.sleep(SLEEP_BETWEEN)
    return stats


def main() -> None:
    today = date.today()
    days = trading_days_window(today, LOOKBACK_DAYS)
    print(f"ZT/ZB collector window: {days[0]} ~ {days[-1]} ({len(days)} weekdays)")

    print("\n>>> fetching 涨停池 (stock_zt_pool_em) ...")
    zt_stats = collect(days, ZT_DIR, ak.stock_zt_pool_em, "zt", force_last_n=2)
    print(f"zt summary: {zt_stats}")

    print("\n>>> fetching 炸板池 (stock_zt_pool_zbgc_em) ...")
    zb_stats = collect(days, ZB_DIR, ak.stock_zt_pool_zbgc_em, "zb", force_last_n=2)
    print(f"zb summary: {zb_stats}")

    # Print quick index of files on disk
    for name, out_dir in [("zt_pool", ZT_DIR), ("zb_pool", ZB_DIR)]:
        files = sorted(out_dir.glob("*.csv.gz"))
        if files:
            print(f"\n{name} on disk: {len(files)} files, {files[0].stem}~{files[-1].stem}")


if __name__ == "__main__":
    main()
