"""Count stocks that satisfy a conservative profitable-trade pattern.

Pattern (read-only statistics; does NOT alter any production strategy):
- Universe: 沪深 A 股剔除 ST/*ST (already pre-filtered in replay.json.gz)
- D1: previous session change > 5%
- D2: before 10:00 could buy — i.e. first minute (09:31) has volume
- D3: before 10:00 could sell — i.e. first minute (09:31) has volume
- Profit: net return > 0 after a round-trip fee of 0.30%

Two execution proxies are reported for sensitivity:
- open: buy = D2 09:31 close, sell = D3 09:31 close
- vwap: buy = D2 09:31-09:59 VWAP, sell = D3 09:31-09:59 VWAP

Both are deterministic and lookahead-free.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLAY = ROOT / "research" / "monthly-target-v1" / "replay.json.gz"
MINUTE_DIR = ROOT / "research" / "monthly-target-v1" / "minutes"
OUT_DIR = ROOT / "research" / "profitable-trade-counts"

START_DATE = "2025-01-01"
ROUND_TRIP_FEE_PCT = 0.30  # 0.03% commission *2 + 0.05% stamp + 0.001% transfer + ~0.1% slippage *2
PRE10_SLICE = slice(0, 29)  # minutes 09:31..09:59 inclusive -> indices 0..28


def load_minutes(date: str, symbol: str) -> dict | None:
    fp = MINUTE_DIR / f"{date}_{symbol}.json.gz"
    if not fp.exists():
        return None
    with gzip.open(fp, "rt") as f:
        return json.load(f)


def open_price(minutes: dict) -> tuple[float, float] | None:
    prices = minutes.get("prices") or []
    volumes = minutes.get("volumes") or []
    if not prices or not volumes:
        return None
    if volumes[0] <= 0 or prices[0] <= 0:
        return None
    return float(prices[0]), float(volumes[0])


def vwap_pre10(minutes: dict) -> float | None:
    prices = (minutes.get("prices") or [])[PRE10_SLICE]
    volumes = (minutes.get("volumes") or [])[PRE10_SLICE]
    if not prices or not volumes:
        return None
    num = 0.0
    den = 0.0
    for p, v in zip(prices, volumes):
        if p is None or v is None or v <= 0 or p <= 0:
            continue
        num += p * v
        den += v
    if den <= 0:
        return None
    return num / den


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(REPLAY, "rt") as f:
        rows = json.load(f)

    stats_open: dict[str, list[dict]] = defaultdict(list)
    stats_vwap: dict[str, list[dict]] = defaultdict(list)
    name_of: dict[str, str] = {}

    scanned = 0
    missing_minutes = 0
    rejected_buy = 0
    rejected_sell = 0
    seen: set[tuple[str, str]] = set()

    for r in rows:
        if r.get("d1_date", "") < START_DATE:
            continue
        if (r.get("d1_change_pct") or 0.0) <= 5.0:
            continue
        key = (r["symbol"], r["d1_date"])
        if key in seen:
            continue
        seen.add(key)
        scanned += 1

        symbol = r["symbol"]
        name_of[symbol] = r.get("name") or symbol
        d2 = r["d2_date"]
        d3 = r["d3_date"]

        d2_min = load_minutes(d2, symbol)
        d3_min = load_minutes(d3, symbol)
        if d2_min is None or d3_min is None:
            missing_minutes += 1
            continue

        buy_open = open_price(d2_min)
        sell_open = open_price(d3_min)
        if buy_open is None:
            rejected_buy += 1
        if sell_open is None:
            rejected_sell += 1

        buy_vwap = vwap_pre10(d2_min)
        sell_vwap = vwap_pre10(d3_min)

        for label, bucket, buy, sell in (
            ("open", stats_open, buy_open[0] if buy_open else None, sell_open[0] if sell_open else None),
            ("vwap", stats_vwap, buy_vwap, sell_vwap),
        ):
            if buy is None or sell is None or buy <= 0:
                continue
            gross = (sell / buy - 1.0) * 100.0
            net = gross - ROUND_TRIP_FEE_PCT
            if net > 0:
                bucket[symbol].append({
                    "d1_date": r["d1_date"],
                    "d2_date": d2,
                    "d3_date": d3,
                    "d1_change_pct": round(r.get("d1_change_pct") or 0.0, 4),
                    "buy_price": round(buy, 4),
                    "sell_price": round(sell, 4),
                    "gross_pct": round(gross, 4),
                    "net_pct": round(net, 4),
                })

    def write_report(label: str, bucket: dict[str, list[dict]]) -> dict:
        trades_total = sum(len(v) for v in bucket.values())
        rows_sorted = sorted(bucket.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        summary_path = OUT_DIR / f"summary_{label}.csv"
        trades_path = OUT_DIR / f"trades_{label}.csv"
        with summary_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "name", "profitable_trade_count",
                        "avg_net_pct", "median_net_pct", "min_net_pct", "max_net_pct"])
            for sym, trades in rows_sorted:
                nets = sorted(t["net_pct"] for t in trades)
                mid = nets[len(nets) // 2] if len(nets) % 2 else (nets[len(nets)//2 - 1] + nets[len(nets)//2]) / 2
                w.writerow([sym, name_of.get(sym, sym), len(trades),
                            round(sum(nets)/len(nets), 4),
                            round(mid, 4),
                            min(nets),
                            max(nets)])
        with trades_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "name", "d1_date", "d2_date", "d3_date",
                        "d1_change_pct", "buy_price", "sell_price", "gross_pct", "net_pct"])
            for sym, trades in rows_sorted:
                for t in trades:
                    w.writerow([sym, name_of.get(sym, sym), t["d1_date"], t["d2_date"], t["d3_date"],
                                t["d1_change_pct"], t["buy_price"], t["sell_price"],
                                t["gross_pct"], t["net_pct"]])
        return {
            "label": label,
            "unique_symbols": len(bucket),
            "profitable_trade_count": trades_total,
            "summary_csv": str(summary_path.relative_to(ROOT)),
            "trades_csv": str(trades_path.relative_to(ROOT)),
        }

    report_open = write_report("open", stats_open)
    report_vwap = write_report("vwap", stats_vwap)

    summary = {
        "pattern": {
            "d1_change_pct_gt": 5.0,
            "exclude_prefix": ["ST", "*ST"],
            "buy_window": "D2 09:31",
            "sell_window": "D3 09:31",
            "round_trip_fee_pct": ROUND_TRIP_FEE_PCT,
            "profit_rule": "net_pct > 0",
            "date_from": START_DATE,
        },
        "candidates_scanned": scanned,
        "candidates_missing_minutes": missing_minutes,
        "rejected_buy_proxy": rejected_buy,
        "rejected_sell_proxy": rejected_sell,
        "reports": {"open": report_open, "vwap": report_vwap},
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
