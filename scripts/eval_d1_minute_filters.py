"""Simple combined filter study on D1-last-30-minute features (not-sealed only)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CMP_JSON = ROOT / "research" / "profitable-trade-counts" / "d1_minute_comparison.json"
TRADES_CSV = ROOT / "research" / "profitable-trade-counts" / "trades_open.csv"


def main() -> None:
    data = json.loads(CMP_JSON.read_text())
    print("baseline (not_sealed):", data["bucket_sizes"]["not_sealed"])

    import gzip
    with gzip.open(ROOT / "research" / "monthly-target-v1" / "replay.json.gz", "rt") as f:
        replay = json.load(f)
    profitable = set()
    with TRADES_CSV.open() as f:
        for r in csv.DictReader(f):
            profitable.add((r["symbol"], r["d1_date"]))

    sys_path = ROOT / "scripts"
    import sys
    sys.path.insert(0, str(sys_path))
    from analyze_d1_last30min import load_minutes, extract_d1_features  # type: ignore

    rows = []
    seen = set()
    for r in replay:
        if r["d1_date"] < "2025-01-01":
            continue
        if (r.get("d1_change_pct") or 0.0) <= 5.0:
            continue
        key = (r["symbol"], r["d1_date"])
        if key in seen:
            continue
        seen.add(key)
        m = load_minutes(r["symbol"], r["d1_date"])
        if m is None:
            continue
        f = extract_d1_features(m)
        if f is None:
            continue
        if (
            abs(f.get("r_last30_pct") or 0.0) < 1e-9
            and abs(f.get("slope_last30_bps_per_min") or 0.0) < 1e-9
            and (f.get("close_location_day") or 0) >= 0.9999
        ):
            continue  # drop sealed limit-up
        f["win"] = key in profitable
        rows.append(f)

    print(f"not_sealed total: {len(rows)}")

    def eval_filter(name: str, fn) -> dict:
        passed = [r for r in rows if fn(r)]
        if not passed:
            return {"filter": name, "n": 0, "win_pct": None}
        return {
            "filter": name,
            "n": len(passed),
            "win_pct": round(100 * sum(1 for r in passed if r["win"]) / len(passed), 2),
            "share_pct": round(100 * len(passed) / len(rows), 2),
        }

    def within(x, lo, hi):
        return x is not None and lo <= x <= hi

    tests = [
        ("baseline", lambda r: True),
        ("final_1min_vol < 2x avg", lambda r: within(r.get("final_minute_vol_x_avg"), 0, 2.0)),
        ("final_1min_vol < 1.5x avg", lambda r: within(r.get("final_minute_vol_x_avg"), 0, 1.5)),
        ("slope_last30 < 0", lambda r: (r.get("slope_last30_bps_per_min") or 0) < 0),
        ("slope_last30 < -1", lambda r: (r.get("slope_last30_bps_per_min") or 0) < -1),
        ("r_last30 < 0", lambda r: (r.get("r_last30_pct") or 0) < 0),
        ("r_last30 < -0.5%", lambda r: (r.get("r_last30_pct") or 0) < -0.5),
        ("close_loc < 0.6", lambda r: within(r.get("close_location_day"), 0, 0.6)),
        ("close_loc 0.6-0.75", lambda r: within(r.get("close_location_day"), 0.6, 0.75)),
        ("close_loc > 0.9", lambda r: (r.get("close_location_day") or 0) > 0.9),
        ("slope<0 & final_vol<2x", lambda r: (r.get("slope_last30_bps_per_min") or 0) < 0 and within(r.get("final_minute_vol_x_avg"), 0, 2.0)),
        ("r_last30<0 & final_vol<2x", lambda r: (r.get("r_last30_pct") or 0) < 0 and within(r.get("final_minute_vol_x_avg"), 0, 2.0)),
        ("r_last30<-0.5 & final_vol<2x", lambda r: (r.get("r_last30_pct") or 0) < -0.5 and within(r.get("final_minute_vol_x_avg"), 0, 2.0)),
        ("r_last30<-0.5 & close_loc<0.75", lambda r: (r.get("r_last30_pct") or 0) < -0.5 and within(r.get("close_location_day"), 0, 0.75)),
        ("slope<-1 & final_vol<1.5x", lambda r: (r.get("slope_last30_bps_per_min") or 0) < -1 and within(r.get("final_minute_vol_x_avg"), 0, 1.5)),
    ]

    results = [eval_filter(name, fn) for name, fn in tests]
    for row in results:
        print(json.dumps(row, ensure_ascii=False))

    out = ROOT / "research" / "profitable-trade-counts" / "d1_minute_filters.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print("wrote", out)


if __name__ == "__main__":
    main()
