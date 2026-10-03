"""Quasi-auction features — strict lookahead-free.

Two decision points evaluated separately:
  (A) decide-at-D2-09:30 (fill D2 price[0]):
      - gap_pct_d2  (D2 price[0] / D1 close - 1) * 100
      That's the ONLY cost-free, pre-fill feature.
  (B) decide-at-D2-09:40 (fill D2 price[9], the close of 09:40):
      - gap_pct_d2
      - early_up_ratio_10min, early_price_strength_10min, early_pullback_10min,
        early_vol_ratio_3min, early_vol_share_10min
      This shifts entry 9 minutes but gives us 10 min of post-open evidence.

Both are tested below; (B) retains Rule A (last-30-min signal from D1).

Sell is always D3 price[0] (09:31 fill).
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_d1_last30min import extract_d1_features  # type: ignore

CAND_FP = ROOT / "research" / "monthly-target-v1-3pct" / "candidates.json.gz"
MIN_DIR = ROOT / "research" / "monthly-target-v1-3pct" / "minutes"
DAILY_DIR = ROOT / "research" / "monthly-target-v1-3pct" / "daily"
OUT_FP = ROOT / "research" / "auction-features" / "quasi_auction_report.json"

FEE_PCT = 0.30
HOLDOUT_START = "2026-07-01"
IN_SAMPLE_END = "2026-06-30"

_daily_cache: dict[str, dict[str, float]] = {}


def load_minutes(path: Path) -> dict | None:
    if not path.exists():
        return None
    with gzip.open(path, "rt") as f:
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


def get_d1_close(symbol: str, d1_date: str) -> float | None:
    key = symbol
    if key not in _daily_cache:
        fp = DAILY_DIR / f"{symbol}.json.gz"
        if not fp.exists():
            _daily_cache[key] = {}
            return None
        d = json.load(gzip.open(fp, "rt"))
        _daily_cache[key] = {bar["datetime"][:10]: float(bar["close"]) for bar in d.get("bars", [])}
    return _daily_cache[key].get(d1_date)


def early_minute_features(d2m: dict, d1_close: float) -> dict | None:
    prices = d2m.get("prices") or []
    volumes = d2m.get("volumes") or []
    if len(prices) < 240 or len(volumes) < 240 or d1_close <= 0:
        return None
    d2_open_price = prices[0]  # first-minute close (approx open)
    gap_pct = (d2_open_price / d1_close - 1.0) * 100.0

    first10_p = prices[:10]
    first10_v = volumes[:10]
    first3_p = prices[:3]
    first3_v = volumes[:3]
    avg_per_min = sum(volumes) / 240.0 if sum(volumes) > 0 else 0.0

    ups = 0
    for i in range(1, 10):
        if prices[i] > prices[i - 1]:
            ups += 1
    minute_returns_f10 = [(prices[i] / prices[i - 1] - 1.0) * 100.0 for i in range(1, 10) if prices[i - 1] > 0]

    f = {
        "gap_pct_d2": gap_pct,
        "early_amount_3min": sum(first3_v[i] * first3_p[i] for i in range(3)),
        "early_vol_ratio_3min": (sum(first3_v) / 3.0) / avg_per_min if avg_per_min > 0 else None,
        "early_up_ratio_10min": ups / 9.0,
        "early_price_strength_10min_pct": (max(first10_p) / d2_open_price - 1.0) * 100.0,
        "early_pullback_10min_pct": (min(first10_p) / d2_open_price - 1.0) * 100.0,
        "early_vol_share_10min": sum(first10_v) / sum(volumes) if sum(volumes) > 0 else None,
        "d2_minute_vol_std_10": (statistics_pstdev(minute_returns_f10) if len(minute_returns_f10) >= 2 else None),
    }
    return f


def statistics_pstdev(xs):
    if len(xs) < 2:
        return None
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))


def within(x, lo, hi):
    return x is not None and lo <= x <= hi


def summarize(samples: list[dict], filters, label: str) -> list[dict]:
    print(f"\n=== {label}: {len(samples)} samples ===")
    results = []
    for name, fn in filters:
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
        print(f"  {r['filter']:<52s} n={r['n']:5d}  win%={r['win_pct']:5.2f}  "
              f"wilsonL={r['wilson95_lower_pct']:5.2f}  mean_net%={r['mean_net_pct']:+6.3f}  "
              f"share={r['share_of_window_pct']:5.2f}%")
    return results


def main() -> None:
    cands = json.load(gzip.open(CAND_FP, "rt"))
    print(f"candidates: {len(cands)}")

    rows_a = []  # buy at D2 09:31 (prices[0])
    rows_b = []  # buy at D2 09:40 (prices[9])
    missing_min = 0
    missing_daily = 0
    sealed_skipped = 0
    bad_corp = 0
    for c in cands:
        if c["d2_action"] or c["d3_action"]:
            bad_corp += 1
            continue
        d1m = load_minutes(MIN_DIR / f"{c['d1_date']}_{c['symbol']}.json.gz")
        d2m = load_minutes(MIN_DIR / f"{c['d2_date']}_{c['symbol']}.json.gz")
        d3m = load_minutes(MIN_DIR / f"{c['d3_date']}_{c['symbol']}.json.gz")
        if d1m is None or d2m is None or d3m is None:
            missing_min += 1
            continue
        d1_close_price = get_d1_close(c["symbol"], c["d1_date"])
        if d1_close_price is None:
            missing_daily += 1
            continue
        feats = extract_d1_features(d1m)
        if feats is None:
            missing_min += 1
            continue
        if is_sealed(feats):
            sealed_skipped += 1
            continue
        d2_feats = early_minute_features(d2m, d1_close_price)
        if d2_feats is None:
            missing_min += 1
            continue
        sell = d3m["prices"][0]
        if sell <= 0 or d3m["volumes"][0] <= 0:
            missing_min += 1
            continue

        # Variant A: buy D2 09:31
        buy_a = d2m["prices"][0]
        if buy_a > 0 and d2m["volumes"][0] > 0:
            feats_a = dict(feats)
            feats_a.update(d2_feats)
            net_a = (sell / buy_a - 1.0) * 100.0 - FEE_PCT
            feats_a["win"] = net_a > 0
            feats_a["net_pct"] = net_a
            feats_a["symbol"] = c["symbol"]
            feats_a["d1_date"] = c["d1_date"]
            feats_a["d1_change_pct"] = c["d1_change_pct"]
            rows_a.append(feats_a)

        # Variant B: buy D2 09:40 (requires any volume in first 10 mins and valid price)
        buy_b = d2m["prices"][9]
        if buy_b > 0 and sum(d2m["volumes"][:10]) > 0:
            feats_b = dict(feats)
            feats_b.update(d2_feats)
            net_b = (sell / buy_b - 1.0) * 100.0 - FEE_PCT
            feats_b["win"] = net_b > 0
            feats_b["net_pct"] = net_b
            feats_b["symbol"] = c["symbol"]
            feats_b["d1_date"] = c["d1_date"]
            feats_b["d1_change_pct"] = c["d1_change_pct"]
            rows_b.append(feats_b)

    print(f"Variant A (buy 09:31): {len(rows_a)} samples")
    print(f"Variant B (buy 09:40): {len(rows_b)} samples")
    print(f"missing_minutes={missing_min}  missing_daily={missing_daily}  "
          f"sealed_skipped={sealed_skipped}  bad_corp={bad_corp}")

    rule_a = lambda r: (r.get("r_last30_pct") or 0) < -0.5 and within(r.get("close_location_day"), 0, 0.75)

    # Variant A filters: only features available before 09:31 (gap + D1-last30)
    filters_a = [
        ("baseline (not_sealed, buy 09:31)", lambda r: True),
        ("Rule A: r_last30<-0.5 & close_loc<0.75", rule_a),
        ("gap_pct_d2 > 0", lambda r: r["gap_pct_d2"] > 0),
        ("gap_pct_d2 >= 1", lambda r: r["gap_pct_d2"] >= 1),
        ("gap_pct_d2 in [0, 2]", lambda r: within(r["gap_pct_d2"], 0, 2)),
        ("gap_pct_d2 in [-1, 1]", lambda r: within(r["gap_pct_d2"], -1, 1)),
        ("gap_pct_d2 < 0", lambda r: r["gap_pct_d2"] < 0),
        ("Rule A + gap_pct_d2 in [0, 2]",
         lambda r: rule_a(r) and within(r["gap_pct_d2"], 0, 2)),
        ("Rule A + gap_pct_d2 > 0", lambda r: rule_a(r) and r["gap_pct_d2"] > 0),
    ]

    # Variant B filters: 10-min open window features legal (buy at 09:40 close)
    filters_b = [
        ("baseline (not_sealed, buy 09:40)", lambda r: True),
        ("Rule A: r_last30<-0.5 & close_loc<0.75", rule_a),
        ("gap_pct_d2 in [0, 2]", lambda r: within(r["gap_pct_d2"], 0, 2)),
        ("early_price_strength > 1%", lambda r: r["early_price_strength_10min_pct"] > 1),
        ("early_pullback > -1% (hold up)", lambda r: r["early_pullback_10min_pct"] > -1),
        ("early_up_ratio >= 0.6", lambda r: r["early_up_ratio_10min"] >= 0.6),
        ("early_up_ratio >= 0.7", lambda r: r["early_up_ratio_10min"] >= 0.7),
        ("Rule A + early_up_ratio >= 0.6",
         lambda r: rule_a(r) and r["early_up_ratio_10min"] >= 0.6),
        ("Rule A + early_pullback > -1",
         lambda r: rule_a(r) and r["early_pullback_10min_pct"] > -1),
        ("Rule A + early_price_strength > 1",
         lambda r: rule_a(r) and r["early_price_strength_10min_pct"] > 1),
        ("Rule A + gap in [0,2] + up_ratio>=0.6",
         lambda r: rule_a(r) and within(r["gap_pct_d2"], 0, 2) and r["early_up_ratio_10min"] >= 0.6),
        ("gap in [0,2] + up_ratio>=0.6",
         lambda r: within(r["gap_pct_d2"], 0, 2) and r["early_up_ratio_10min"] >= 0.6),
    ]

    def eval_variant(rows, filters, tag):
        print(f"\n########## Variant {tag} ##########")
        all_bucket = summarize(rows, filters, f"ALL ({tag})")
        in_sample = [r for r in rows if r["d1_date"] <= IN_SAMPLE_END]
        holdout = [r for r in rows if r["d1_date"] >= HOLDOUT_START]
        in_results = summarize(in_sample, filters, f"IN-SAMPLE {tag} (<= {IN_SAMPLE_END})")
        out_results = summarize(holdout, filters, f"HOLDOUT  {tag} (>= {HOLDOUT_START})")
        return {"all": all_bucket, "in_sample": in_results, "holdout": out_results,
                "n_total": len(rows), "n_in": len(in_sample), "n_hold": len(holdout)}

    result_a = eval_variant(rows_a, filters_a, "A buy@09:31")
    result_b = eval_variant(rows_b, filters_b, "B buy@09:40")

    OUT_FP.parent.mkdir(parents=True, exist_ok=True)
    OUT_FP.write_text(json.dumps({
        "fee_pct": FEE_PCT,
        "min_d1_change_pct": 3.0,
        "variant_a_buy_0931": result_a,
        "variant_b_buy_0940": result_b,
    }, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_FP}")


if __name__ == "__main__":
    main()
