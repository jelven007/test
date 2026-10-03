"""Collect concept-board mapping and daily OHLC history from akshare (东方财富).

Outputs (per concept board):
  - research/sentiment-features/concept_boards/mapping.json
      {board_name: [symbol, ...], ...}  snapshot of today's mapping
  - research/sentiment-features/concept_boards/daily/<board_name>.csv.gz
      per-board daily OHLC, incrementally appended

akshare endpoints used:
  - stock_board_concept_name_em(): list of all concept boards
  - stock_board_concept_cons_em(symbol=board_name): constituents of a board
  - stock_board_concept_hist_em(symbol=board_name,
        period='daily', start_date, end_date): daily OHLC per board

Notes:
  * Mapping is current-day only (akshare doesn't give历史成分股). Each run
    overwrites mapping.json after a successful full refresh. For historical
    analysis we'll use TODAY's membership as proxy — a known limitation.
  * Board history can go back years; we default to START=2025-01-01.
  * Any 'Remote end closed connection' is retried up to RETRY times.
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36"
)
_ORIG_REQ = requests.sessions.Session.request


def _patched_request(self, method, url, **kw):
    headers = kw.get("headers") or {}
    if "User-Agent" not in headers:
        headers["User-Agent"] = _BROWSER_UA
    if "eastmoney.com" in str(url) and "Referer" not in headers:
        headers["Referer"] = "https://data.eastmoney.com/"
    kw["headers"] = headers
    return _ORIG_REQ(self, method, url, **kw)


requests.sessions.Session.request = _patched_request


import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "research" / "sentiment-features" / "concept_boards"
MAPPING_FP = OUT / "mapping.json"
DAILY_DIR = OUT / "daily"
METADATA_FP = OUT / "metadata.json"

START = "20250101"
END = date.today().strftime("%Y%m%d")

RETRY = 8
BACKOFF = 1.6
BOARD_SLEEP = 0.6  # throttle between boards to avoid getting throttled


def with_retry(fn, label: str, *args, **kwargs):
    last = None
    for attempt in range(1, RETRY + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            if attempt == RETRY:
                print(f"  {label}: ERR after {RETRY} tries ({exc})")
                return None
            delay = min(BACKOFF ** attempt, 30.0)
            time.sleep(delay)
    return None


def refresh_board_list() -> pd.DataFrame | None:
    print(">>> fetching concept board list ...")
    df = with_retry(ak.stock_board_concept_name_em, "stock_board_concept_name_em")
    if df is None or len(df) == 0:
        return None
    print(f"  got {len(df)} boards; cols={list(df.columns)}")
    return df


def normalize_symbol(code: str) -> str:
    c = str(code).strip()
    if "." in c:
        c = c.split(".")[0]
    return c.zfill(6)


def refresh_mapping(board_names: list[str]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    print(f"\n>>> fetching constituents for {len(board_names)} boards ...")
    for i, name in enumerate(board_names, 1):
        df = with_retry(ak.stock_board_concept_cons_em, f"cons[{name}]", symbol=name)
        if df is None or len(df) == 0:
            continue
        syms = sorted({normalize_symbol(s) for s in df["代码"].tolist()}) if "代码" in df.columns else []
        if syms:
            mapping[name] = syms
        if i % 50 == 0:
            print(f"  mapping progress: {i}/{len(board_names)} boards, cumulative={len(mapping)}")
        time.sleep(BOARD_SLEEP)
    return mapping


def refresh_daily(board_names: list[str]) -> dict[str, int]:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"written": 0, "errors": 0, "cached": 0}
    for i, name in enumerate(board_names, 1):
        safe = name.replace("/", "_").replace(" ", "_")
        fp = DAILY_DIR / f"{safe}.csv.gz"

        # Determine incremental start: if we have a file, re-pull from last cached
        # date onward; else go back to START. Overwriting is fine since we re-save all rows.
        start_date = START
        if fp.exists():
            try:
                with gzip.open(fp, "rt") as f:
                    cached = pd.read_csv(f)
                if "日期" in cached.columns and len(cached):
                    last_cached = pd.to_datetime(cached["日期"]).max()
                    # pull from (last - 5 days) to be safe
                    start_date = (last_cached - pd.Timedelta(days=5)).strftime("%Y%m%d")
            except Exception:
                pass

        df = with_retry(
            ak.stock_board_concept_hist_em,
            f"hist[{name}]",
            symbol=name, period="daily", start_date=start_date, end_date=END, adjust="",
        )
        if df is None or len(df) == 0:
            stats["errors"] += 1
            continue

        if fp.exists():
            try:
                with gzip.open(fp, "rt") as f:
                    old = pd.read_csv(f)
                combined = pd.concat([old, df], ignore_index=True)
                combined = combined.drop_duplicates(subset=["日期"], keep="last").sort_values("日期")
            except Exception:
                combined = df
        else:
            combined = df

        with gzip.open(fp, "wt", encoding="utf-8") as f:
            combined.to_csv(f, index=False)
        stats["written"] += 1
        if i % 20 == 0 or i == len(board_names):
            print(f"  daily progress: {i}/{len(board_names)} written={stats['written']} err={stats['errors']}")
        time.sleep(BOARD_SLEEP)
    return stats


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    boards_df = refresh_board_list()
    if boards_df is None:
        print("FATAL: could not fetch board list; aborting")
        sys.exit(1)

    board_names = boards_df["板块名称"].tolist() if "板块名称" in boards_df.columns else []
    if not board_names:
        print("FATAL: board list has no 板块名称 column; cols=", list(boards_df.columns))
        sys.exit(1)

    # 1. Mapping (constituents snapshot — current only, akshare limitation)
    mapping = refresh_mapping(board_names)
    MAPPING_FP.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_FP.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
    print(f"\nmapping snapshot: {len(mapping)} boards -> {MAPPING_FP}")

    # 2. Daily history per board
    stats = refresh_daily(board_names)
    print(f"daily refresh stats: {stats}")

    # 3. Metadata
    metadata = {
        "refreshed_at": pd.Timestamp.now().isoformat(),
        "board_total": len(board_names),
        "mapping_boards_with_symbols": len(mapping),
        "daily_written": stats["written"],
        "daily_errors": stats["errors"],
        "mapping_is_snapshot": True,
        "note": "Mapping reflects today only — akshare doesn't expose historical constituents.",
    }
    METADATA_FP.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
