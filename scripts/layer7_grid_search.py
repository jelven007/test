"""T+1-compliant Layer 7 full parameter grid search.

Prior work tested only 7 (TP, SL) combos. Here we expand to a 35-point grid
plus 5 extra asymmetric candidates, score each by:
  - single-trade: win%, mean_net%, Wilson lower bound
  - monthly portfolio (ALL / IN / HOLDOUT): P(month > 0), mean month ret,
    cum return
  - quarterly P(q > 0)

Rank by (ALL hit_rate_pct desc, HOLDOUT hit_rate_pct desc, mean month desc).

Output: research/fusion-mvp/layer7_grid_report.json + CSV
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mvp_fusion_backtest import (  # type: ignore
    pass_layer4_rule_a, pass_layer6_gap,
    FEE_PCT, IN_SAMPLE_END, HOLDOUT_START, CAND_FP, MIN_DIR,
    extract_d1_features, is_sealed, get_d1_close,
)
from layer7_dynamic_exit import simulate_intraday_exit  # type: ignore

OUT_JSON = ROOT / "research" / "fusion-mvp" / "layer7_grid_report.json"
OUT_CSV = ROOT / "research" / "fusion-mvp" / "layer7_grid_summary.csv"

SINGLE_WEIGHT = 0.20
MAX_CONCURRENT = 5


def load_minutes(symbol: str, day: str) -> dict | None:
    fp = MIN_DIR / f"{day}_{symbol}.json.gz"
    if not fp.exists():
        return None
    with gzip.open(fp, "rt") as f:
        return json.load(f)


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * (center - margin) / denom


def net_pct(buy: float, exit_price: float) -> float:
    return (exit_price / buy - 1.0) * 100.0 - FEE_PCT


def build_fusion_samples(cands: list[dict]) -> list[dict]:
    out = []
    for c in cands:
        if c.get("d2_action") or c.get("d3_action"):
            continue
        d1m = load_minutes(c["symbol"], c["d1_date"])
        d2m = load_minutes(c["symbol"], c["d2_date"])
        d3m = load_minutes(c["symbol"], c["d3_date"])
        if d1m is None or d2m is None or d3m is None:
            continue
        if (d2m.get("volumes") or [0])[0] <= 0 or (d3m.get("volumes") or [0])[0] <= 0:
            continue
        feats = extract_d1_features(d1m)
        if feats is None or is_sealed(feats):
            continue
        d1_close = get_d1_close(c["symbol"], c["d1_date"])
        if d1_close is None or d1_close <= 0:
            continue
        buy = d2m["prices"][0]
        d3_prices = d3m["prices"]
        if buy <= 0 or not d3_prices or d3_prices[0] <= 0:
            continue
        gap_pct_d2 = (buy / d1_close - 1.0) * 100.0
        sample = {
            "symbol": c["symbol"],
            "d1_date": c["d1_date"],
            "d2_date": c["d2_date"],
            "d3_date": c["d3_date"],
            "d1_change_pct": c["d1_change_pct"],
            "r_last30_pct": feats.get("r_last30_pct"),
            "close_location_day": feats.get("close_location_day"),
            "gap_pct_d2": gap_pct_d2,
            "buy": buy,
            "d3_prices": d3_prices,
            "lhb_bad_reason": False,
            "lhb_bad_inst_sell": False,
        }
        if pass_layer4_rule_a(sample) and pass_layer6_gap(sample):
            out.append(sample)
    return out


def apply_exit(samples: list[dict], tp: float, sl: float) -> list[dict]:
    out = []
    for s in samples:
        exit_price, reason = simulate_intraday_exit(
            s["buy"], s["d3_prices"], tp, sl
        )
        np_pct = net_pct(s["buy"], exit_price)
        out.append({
            "d1_date": s["d1_date"],
            "d2_date": s["d2_date"],
            "symbol": s["symbol"],
            "d1_change_pct": s["d1_change_pct"],
            "net_pct": np_pct,
            "exit_reason": reason,
        })
    return out


def monthly_pool(trades: list[dict]) -> dict[str, float]:
    by_d2: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_d2[t["d2_date"]].append(t)
    for lst in by_d2.values():
        lst.sort(key=lambda x: x.get("d1_change_pct") or 0, reverse=True)
    ret: dict[str, float] = defaultdict(float)
    for d2 in sorted(by_d2):
        for t in by_d2[d2][:MAX_CONCURRENT]:
            ret[t["d2_date"][:7]] += t["net_pct"] * SINGLE_WEIGHT
    return ret


def window_stats(monthly_ret: dict[str, float], window_pred) -> dict:
    rows = [(m, r) for m, r in sorted(monthly_ret.items()) if window_pred(m)]
    n = len(rows)
    hits = sum(1 for _, r in rows if r > 0)
    cum = 1.0
    for _, r in rows:
        cum *= (1.0 + r / 100.0)
    mean = sum(r for _, r in rows) / n if n else 0.0
    return {
        "n": n,
        "hits": hits,
        "hit_pct": round(100 * hits / n, 2) if n else 0.0,
        "wilsonL_pct": round(wilson_lower(hits, n), 2),
        "mean_month_pct": round(mean, 4),
        "cum_pct": round((cum - 1.0) * 100.0, 4),
    }


def quarterly_hit(monthly_ret: dict[str, float], window_pred) -> dict:
    by_q: dict[str, list[float]] = defaultdict(list)
    for m, r in monthly_ret.items():
        if not window_pred(m):
            continue
        y, mo = m.split("-")
        q = f"{y}-Q{(int(mo) - 1) // 3 + 1}"
        by_q[q].append(r)
    rows = []
    for q in sorted(by_q):
        cum = 1.0
        for v in by_q[q]:
            cum *= (1.0 + v / 100.0)
        rows.append((q, (cum - 1.0) * 100.0))
    n = len(rows)
    hits = sum(1 for _, v in rows if v > 0)
    return {
        "n": n,
        "hits": hits,
        "hit_pct": round(100 * hits / n, 2) if n else 0.0,
        "wilsonL_pct": round(wilson_lower(hits, n), 2),
    }


def main() -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    print("loading candidates ...")
    with gzip.open(CAND_FP, "rt") as f:
        cands = json.load(f)
    print(f"  n={len(cands)}")

    print("building live-equivalent fusion (L4+L6; L5 disabled) samples ...")
    samples = build_fusion_samples(cands)
    print(f"  n={len(samples)}")

    # Full grid: TP 3..10, SL 2..5
    tp_vals = [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10]
    sl_vals = [0.02, 0.025, 0.03, 0.04, 0.05]
    # Also asymmetric candidates
    extra = [(0.10, 0.02), (0.08, 0.025), (0.06, 0.02), (0.07, 0.025), (0.05, 0.025)]
    grid = []
    for tp in tp_vals:
        for sl in sl_vals:
            grid.append((round(tp, 4), round(sl, 4)))
    for pair in extra:
        if pair not in grid:
            grid.append(pair)

    all_pred = lambda m: True
    in_pred = lambda m: m <= IN_SAMPLE_END[:7]
    hd_pred = lambda m: m >= HOLDOUT_START[:7]

    print(f"\n=== Grid search: {len(grid)} (TP, SL) combos ===")
    print(f"{'TP%':>5} {'SL%':>5} {'nT':>4} {'win%':>6} {'wiL%':>6} "
          f"{'mean%':>8} "
          f"{'All_m':>6} {'All_wL':>7} {'In_m':>5} {'Hd_m':>5} "
          f"{'All_q':>6} {'cumAll%':>9}")

    results = []
    for tp, sl in grid:
        trades = apply_exit(samples, tp, sl)
        n = len(trades)
        wins = sum(1 for t in trades if t["net_pct"] > 0)
        single_win = 100 * wins / n if n else 0
        single_wL = wilson_lower(wins, n)
        mean = sum(t["net_pct"] for t in trades) / n if n else 0
        monthly = monthly_pool(trades)
        all_s = window_stats(monthly, all_pred)
        in_s = window_stats(monthly, in_pred)
        hd_s = window_stats(monthly, hd_pred)
        q_all = quarterly_hit(monthly, all_pred)
        reasons = defaultdict(int)
        for t in trades:
            reasons[t["exit_reason"]] += 1
        row = {
            "tp_pct": tp,
            "sl_pct": sl,
            "n_trades": n,
            "single_win_pct": round(single_win, 2),
            "single_wilsonL_pct": round(single_wL, 2),
            "single_mean_net_pct": round(mean, 4),
            "exit_distribution": dict(reasons),
            "all": all_s,
            "in": in_s,
            "holdout": hd_s,
            "quarterly_all": q_all,
        }
        results.append(row)
        print(f"{tp*100:>5.1f} {sl*100:>5.1f} {n:>4} "
              f"{single_win:>6.2f} {single_wL:>6.2f} {mean:>+7.3f} "
              f"{all_s['hit_pct']:>5.1f}% {all_s['wilsonL_pct']:>6.1f}% "
              f"{in_s['hit_pct']:>4.1f}% {hd_s['hit_pct']:>4.1f}% "
              f"{q_all['hit_pct']:>5.1f}% {all_s['cum_pct']:>+8.3f}%")

    # Ranking: primarily by ALL hit rate, break ties by Wilson lower bound, then mean month
    results_sorted = sorted(
        results,
        key=lambda r: (
            r["all"]["hit_pct"],
            r["all"]["wilsonL_pct"],
            r["holdout"]["hit_pct"],
            r["all"]["mean_month_pct"],
        ),
        reverse=True,
    )
    print("\n=== TOP-10 by ALL hit_rate ===")
    print(f"  #{'':>1}  {'TP':>5} {'SL':>5}  {'All_m%':>7} {'wiL%':>6}  "
          f"{'Hd_m%':>6}  {'mean%':>7}  {'cum%':>8}  "
          f"{'≥60%':>5} {'≥50%':>5}")
    for i, r in enumerate(results_sorted[:10], 1):
        ge60 = "✓" if r["all"]["hit_pct"] >= 60 else "✗"
        ge50 = "✓" if r["all"]["hit_pct"] >= 50 else "✗"
        print(f"  #{i}  {r['tp_pct']*100:>5.1f} {r['sl_pct']*100:>5.1f}  "
              f"{r['all']['hit_pct']:>6.2f}% {r['all']['wilsonL_pct']:>5.2f}%  "
              f"{r['holdout']['hit_pct']:>5.2f}%  "
              f"{r['single_mean_net_pct']:>+6.3f}%  "
              f"{r['all']['cum_pct']:>+7.3f}%  "
              f"{ge60:>5} {ge50:>5}")

    # CSV
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "tp_pct", "sl_pct", "n_trades", "single_win_pct", "single_wilsonL_pct",
            "single_mean_net_pct", "all_n", "all_hits", "all_hit_pct",
            "all_wilsonL_pct", "all_mean_month_pct", "all_cum_pct",
            "in_hit_pct", "in_cum_pct", "hd_n", "hd_hits", "hd_hit_pct",
            "hd_wilsonL_pct", "hd_cum_pct", "q_all_hit_pct",
        ])
        for r in results:
            w.writerow([
                r["tp_pct"] * 100, r["sl_pct"] * 100, r["n_trades"],
                r["single_win_pct"], r["single_wilsonL_pct"],
                r["single_mean_net_pct"],
                r["all"]["n"], r["all"]["hits"], r["all"]["hit_pct"],
                r["all"]["wilsonL_pct"], r["all"]["mean_month_pct"],
                r["all"]["cum_pct"],
                r["in"]["hit_pct"], r["in"]["cum_pct"],
                r["holdout"]["n"], r["holdout"]["hits"],
                r["holdout"]["hit_pct"], r["holdout"]["wilsonL_pct"],
                r["holdout"]["cum_pct"],
                r["quarterly_all"]["hit_pct"],
            ])

    report = {
        "target": "P(month_return > 0) >= 60% (stretch), >= 50% (baseline)",
        "exit_protocol": (
            "Buy D2 09:31; evaluate TP/SL on D3 only; if neither triggers "
            "through 14:54, send force-exit at 14:55 and proxy with 14:56."
        ),
        "n_grid": len(grid),
        "sample_universe": (
            "fusion-l7-v1 live-equivalent L4+L6 samples; "
            "L5 LHB check disabled with pass-through"
        ),
        "n_samples": len(samples),
        "ranking_key": "ALL hit_rate desc, Wilson L desc, holdout hit_rate desc, mean desc",
        "results": results,
        "top_10": [{"tp": r["tp_pct"], "sl": r["sl_pct"],
                     "all_hit": r["all"]["hit_pct"],
                     "all_wilsonL": r["all"]["wilsonL_pct"],
                     "holdout_hit": r["holdout"]["hit_pct"],
                     "mean_net": r["single_mean_net_pct"],
                     "cum_all": r["all"]["cum_pct"]}
                    for r in results_sorted[:10]],
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_JSON}")
    print(f"wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
