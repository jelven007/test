"""Replay the 3% pipeline using open/open proxy, then run the same analyses
and holdout validation as the 5% version."""
from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "research" / "monthly-target-v1-3pct"
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_d1_last30min import extract_d1_features  # type: ignore

FEE_PCT = 0.30
HOLDOUT_START = "2026-07-01"
IN_SAMPLE_END = "2026-06-30"


def load_minutes(symbol: str, day: str) -> dict | None:
    fp = OUTPUT / "minutes" / f"{day}_{symbol}.json.gz"
    if not fp.exists():
        return None
    with gzip.open(fp, "rt") as f:
        return json.load(f)


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
    ("close_loc 0.6-0.75", lambda r: within(r.get("close_location_day"), 0.6, 0.75)),
]


def main() -> None:
    cands = json.load(gzip.open(OUTPUT / "candidates.json.gz", "rt"))
    print(f"candidates: {len(cands)}")

    rows = []
    missing_min = 0
    sealed_skipped = 0
    bad_corp = 0
    for c in cands:
        if c["d2_action"] or c["d3_action"]:
            bad_corp += 1
            continue
        d1m = load_minutes(c["symbol"], c["d1_date"])
        d2m = load_minutes(c["symbol"], c["d2_date"])
        d3m = load_minutes(c["symbol"], c["d3_date"])
        if d1m is None or d2m is None or d3m is None:
            missing_min += 1
            continue
        feats = extract_d1_features(d1m)
        if feats is None:
            missing_min += 1
            continue
        if is_sealed(feats):
            sealed_skipped += 1
            continue
        buy = d2m["prices"][0]
        sell = d3m["prices"][0]
        if buy <= 0 or d2m["volumes"][0] <= 0 or d3m["volumes"][0] <= 0:
            missing_min += 1
            continue
        net = (sell / buy - 1.0) * 100.0 - FEE_PCT
        feats["win"] = net > 0
        feats["net_pct"] = net
        feats["symbol"] = c["symbol"]
        feats["d1_date"] = c["d1_date"]
        feats["d1_change_pct"] = c["d1_change_pct"]
        rows.append(feats)
    print(f"replayed samples (not_sealed): {len(rows)}  missing_minutes={missing_min}  sealed_skipped={sealed_skipped}  bad_corp={bad_corp}")

    def summarize(samples: list[dict], label: str) -> list[dict]:
        print(f"\n=== {label}: {len(samples)} samples ===")
        results = []
        for name, fn in FILTERS:
            passed = [s for s in samples if fn(s)]
            n = len(passed)
            wins = sum(1 for s in passed if s["win"])
            mean_net = (sum(s["net_pct"] for s in passed) / n) if n else 0.0
            win_pct = (100 * wins / n) if n else 0.0
            r = {
                "filter": name,
                "n": n,
                "wins": wins,
                "win_pct": round(win_pct, 2),
                "wilson95_lower_pct": round(wilson_lower(wins, n), 2),
                "mean_net_pct": round(mean_net, 4),
                "share_of_window_pct": round(100 * n / len(samples), 2) if samples else 0.0,
            }
            results.append(r)
            print(f"  {r['filter']:<42s} n={r['n']:5d}  win%={r['win_pct']:5.2f}  "
                  f"wilsonL={r['wilson95_lower_pct']:5.2f}  mean_net%={r['mean_net_pct']:+6.3f}  "
                  f"share={r['share_of_window_pct']:5.2f}%")
        return results

    all_bucket = summarize(rows, "ALL 3%+ (2025-01-01~2026-09-28)")
    in_sample = [r for r in rows if r["d1_date"] <= IN_SAMPLE_END]
    holdout = [r for r in rows if r["d1_date"] >= HOLDOUT_START]
    in_results = summarize(in_sample, f"IN-SAMPLE (<= {IN_SAMPLE_END})")
    out_results = summarize(holdout, f"HOLDOUT (>= {HOLDOUT_START})")

    # Also stratify by D1 change magnitude on all samples
    tiers = [("3-4%", 3.0, 4.0), ("4-5%", 4.0, 5.0), ("5-7%", 5.0, 7.0), ("7-10%", 7.0, 10.0), ("10%+", 10.0, 22.0)]
    print("\n=== D1 magnitude strata (baseline win% on not_sealed) ===")
    strata_report = []
    for label, lo, hi in tiers:
        seg = [r for r in rows if lo <= r["d1_change_pct"] < hi]
        n = len(seg)
        wins = sum(1 for s in seg if s["win"])
        mean_net = (sum(s["net_pct"] for s in seg) / n) if n else 0.0
        win_pct = (100 * wins / n) if n else 0.0
        s = {"stratum": label, "n": n, "wins": wins, "win_pct": round(win_pct, 2),
             "wilson95_lower_pct": round(wilson_lower(wins, n), 2),
             "mean_net_pct": round(mean_net, 4)}
        strata_report.append(s)
        print(f"  {label:>6s}  n={n:5d}  win%={s['win_pct']:5.2f}  wilsonL={s['wilson95_lower_pct']:5.2f}  mean_net%={s['mean_net_pct']:+6.3f}")

    out = OUTPUT / "holdout_3pct.json"
    out.write_text(json.dumps({
        "fee_pct": FEE_PCT,
        "min_d1_change_pct": 3.0,
        "all": all_bucket,
        "in_sample": in_results,
        "holdout": out_results,
        "strata_baseline": strata_report,
        "sealed_skipped": sealed_skipped,
        "bad_corp": bad_corp,
        "total_rows": len(rows),
    }, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
