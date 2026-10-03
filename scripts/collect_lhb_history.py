"""Download 2025-01-01 ~ 2026-09-28 龙虎榜 history via akshare and cache by month.

Writes research/auction-features/lhb_cache/YYYYMM.csv.gz. If a month file
already exists, skip. The last partial month is always re-downloaded.
"""
from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "research" / "auction-features" / "lhb_cache"

START = date(2025, 1, 1)
END = date(2026, 9, 28)


def months_in_range(start: date, end: date) -> list[tuple[date, date]]:
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        first = date(y, m, 1)
        if m == 12:
            next_first = date(y + 1, 1, 1)
        else:
            next_first = date(y, m + 1, 1)
        last = min(next_first.replace(day=1).toordinal() - 1, end.toordinal())
        out.append((first, date.fromordinal(last)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    total = 0
    for lo, hi in months_in_range(START, END):
        tag = f"{lo.year:04d}{lo.month:02d}"
        fp = OUT / f"{tag}.csv.gz"
        last_month = (hi == END) or (hi == date(END.year, END.month, END.day))
        if fp.exists() and not last_month:
            with gzip.open(fp, "rt") as f:
                n = sum(1 for _ in f) - 1
            print(f"  {tag}: cached ({n} rows)")
            total += n
            continue
        print(f"  {tag}: fetching {lo}~{hi} ...", end="", flush=True)
        try:
            df = ak.stock_lhb_detail_em(
                start_date=lo.strftime("%Y%m%d"),
                end_date=hi.strftime("%Y%m%d"),
            )
        except Exception as exc:
            print(f" ERR {exc}")
            continue
        if df is None or len(df) == 0:
            print(" empty")
            continue
        with gzip.open(fp, "wt", encoding="utf-8") as f:
            df.to_csv(f, index=False)
        print(f" {len(df)} rows -> {fp.name}")
        total += len(df)
    print(f"\ntotal cached rows across all months: {total}")


if __name__ == "__main__":
    main()
