"""Holdout validation: evaluate the two D1-last-30-minute filter rules on 2026H2.

In-sample window: D1 date in [2025-01-01, 2026-06-30].
Holdout window:   D1 date in [2026-07-01, latest available].

For each (symbol, D1_date) in each window, we:
  1. Replay the open-proxy trade (buy D2 09:31, sell D3 09:31) with 0.30% round-trip fee.
  2. Drop sealed-limit-up D1 (last-30-min zero return and slope and close_loc=1).
  3. Apply each candidate filter and compute win rate (net > 0) + Wilson 95% lower bound.

Prints a comparison table.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_d1_last30min import extract_d1_features, load_minutes  # type: ignore

REPLAY = ROOT / "research" / "monthly-target-v1" / "replay.json.gz"
OUT = ROOT / "research" / "profitable-trade-counts" / "holdout_2026h2.json"
MINUTE_DIR = ROOT / "research" / "monthly-target-v1" / "minutes"

FEE_PCT = 0.30
IN_SAMPLE_END = "2026-06-30"
HOLDOUT_START = "2026-07-01"


def minutes_open(path: Path) -> float | None:
    if not path.exists():
        return None
    with gzip.open(path, "rt") as f:
        m = json.load(f)
    prices = m.get("prices") or []
    volumes = m.get("volumes") or []
    if not prices or not volumes or volumes[0] <= 0 or prices[0] <= 0:
        return None
    return float(prices[0])


def is_sealed(f: dict) -> bool:
    return (
        abs(f.get("r_last30_pct") or 0.0) < 1e-9
        and abs(f.get("slope_last30_bps_per_min") or 0.0) < 1e-9
        and (f.get("close_location_day") or 0) >= 0.9999
    )


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * (center - margin) / denom


def build_samples(rows: list[dict]) -> list[dict]:
    out = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["symbol"], r["d1_date"])
        if key in seen:
            continue
        seen.add(key)
        if (r.get("d1_change_pct") or 0.0) <= 5.0:
            continue
        d1_min = load_minutes(r["symbol"], r["d1_date"])
        if d1_min is None:
            continue
        feats = extract_d1_features(d1_min)
        if feats is None or is_sealed(feats):
            continue
        buy = minutes_open(MINUTE_DIR / f"{r['d2_date']}_{r['symbol']}.json.gz")
        sell = minutes_open(MINUTE_DIR / f"{r['d3_date']}_{r['symbol']}.json.gz")
        if buy is None or sell is None or buy <= 0:
            continue
        net = (sell / buy - 1.0) * 100.0 - FEE_PCT
        feats["win"] = net > 0
        feats["net_pct"] = net
        feats["d1_date"] = r["d1_date"]
        feats["symbol"] = r["symbol"]
        out.append(feats)
    return out


def within(x, lo, hi):
    return x is not None and lo <= x <= hi


FILTERS = [
    ("baseline (not_sealed)", lambda r: True),
    ("Rule A: r_last30<-0.5% & close_loc<0.75",
     lambda r: (r.get("r_last30_pct") or 0) < -0.5 and within(r.get("close_location_day"), 0, 0.75)),
    ("Rule B: r_last30<-0.5% & final_vol<2x",
     lambda r: (r.get("r_last30_pct") or 0) < -0.5 and within(r.get("final_minute_vol_x_avg"), 0, 2.0)),
    ("Rule A+B union",
     lambda r: ((r.get("r_last30_pct") or 0) < -0.5 and within(r.get("close_location_day"), 0, 0.75))
               or ((r.get("r_last30_pct") or 0) < -0.5 and within(r.get("final_minute_vol_x_avg"), 0, 2.0))),
    ("Rule A+B intersect",
     lambda r: (r.get("r_last30_pct") or 0) < -0.5
               and within(r.get("close_location_day"), 0, 0.75)
               and within(r.get("final_minute_vol_x_avg"), 0, 2.0)),
    ("close_loc 0.6-0.75 (single)",
     lambda r: within(r.get("close_location_day"), 0.6, 0.75)),
]


def eval_window(label: str, rows: list[dict]) -> list[dict]:
    samples = build_samples(rows)
    print(f"\n=== {label}: {len(samples)} samples ===")
    results = []
    for name, fn in FILTERS:
        passed = [s for s in samples if fn(s)]
        wins = sum(1 for s in passed if s["win"])
        n = len(passed)
        win_pct = (100.0 * wins / n) if n else 0.0
        mean_net = (sum(s["net_pct"] for s in passed) / n) if n else 0.0
        results.append({
            "filter": name,
            "n": n,
            "wins": wins,
            "win_pct": round(win_pct, 2),
            "wilson95_lower_pct": round(wilson_lower(wins, n), 2),
            "mean_net_pct": round(mean_net, 4),
            "share_of_window_pct": round(100.0 * n / len(samples), 2) if samples else 0.0,
        })
    for r in results:
        print(f"  {r['filter']:<42s} n={r['n']:5d}  win%={r['win_pct']:5.2f}  "
              f"wilsonL={r['wilson95_lower_pct']:5.2f}  mean_net%={r['mean_net_pct']:+6.3f}  "
              f"share={r['share_of_window_pct']:5.2f}%")
    return results


def main() -> None:
    with gzip.open(REPLAY, "rt") as f:
        rows = json.load(f)
    in_sample = [r for r in rows if "2025-01-01" <= r["d1_date"] <= IN_SAMPLE_END]
    holdout = [r for r in rows if r["d1_date"] >= HOLDOUT_START]
    print(f"replay rows: in_sample={len(in_sample)}  holdout_raw={len(holdout)}")

    report = {
        "in_sample_window": ["2025-01-01", IN_SAMPLE_END],
        "holdout_window": [HOLDOUT_START, max(r["d1_date"] for r in holdout) if holdout else None],
        "in_sample": eval_window("IN-SAMPLE (2025-01-01 ~ 2026-06-30)", in_sample),
        "holdout": eval_window(f"HOLDOUT (2026-07-01 ~ {max(r['d1_date'] for r in holdout) if holdout else '-'})", holdout),
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print("\nwrote", OUT)


if __name__ == "__main__":
    main()
