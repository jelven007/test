"""Fusion backtest with Layer 1 sentiment proxy applied.

Problem: real Layer 1 sentiment scorer only has 12 trading days of data
(2026-09-09 ~ 2026-09-24, from zt/zb pool backfill). Not enough to validate
Layer 1 impact on a 21-month backtest.

Workaround: build a lookahead-free "sentiment proxy" from historical data
we already have — daily count of D1≥3% stocks (from candidates.json.gz),
then rolling 20-day percentile → ice/start/ferment/climax same bins as the
real scorer. Known by D1 close; D2 open uses D1-date state → no lookahead.

Then (1) validate proxy vs real sentiment in the 12-day overlap, (2) apply
proxy as Layer 1 to the fusion pipeline, (3) report monthly hit rate change.

Two L1 modes compared:
  Mode A (hard): skip all signals when D1-date sentiment_state == 'climax'
  Mode B (soft): scale single_name_weight by position_multiplier
                 ice=1.5, start=1.0, ferment=0.75, climax=0.0
                 (cap total portfolio exposure at 1.0)
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_d1_last30min import extract_d1_features  # type: ignore
from mvp_fusion_backtest import (  # type: ignore
    load_lhb, build_sample, pass_layer4_rule_a, pass_layer5_lhb, pass_layer6_gap,
    wilson_lower, FEE_PCT, IN_SAMPLE_END, HOLDOUT_START,
    CAND_FP, OUT_DIR as BASE_OUT_DIR,
)

OUT_DIR = ROOT / "research" / "fusion-mvp"
OUT_REPORT = OUT_DIR / "report_with_l1.json"
REAL_SENTIMENT_FP = ROOT / "research" / "sentiment-features" / "sentiment_daily.csv.gz"

PROXY_WINDOW = 20
POSITION_MULT = {
    "ice": 1.5,
    "start": 1.0,
    "ferment": 0.75,
    "climax": 0.0,
}


def classify_state(score: float) -> str:
    if score < 25:
        return "ice"
    if score < 50:
        return "start"
    if score < 75:
        return "ferment"
    return "climax"


def build_sentiment_proxy(cands: list[dict], window: int = PROXY_WINDOW) -> dict[str, dict]:
    """Lookahead-free: for each D1 date, count D1 >= 3% stocks that day,
    then rolling 'window'-day percentile.
    """
    daily_count: dict[str, int] = defaultdict(int)
    for c in cands:
        if (c.get("d1_change_pct") or 0) >= 3.0:
            daily_count[c["d1_date"]] += 1
    days = sorted(daily_count.keys())
    out: dict[str, dict] = {}
    for i, d in enumerate(days):
        lo = max(0, i - window + 1)
        window_slice = days[lo:i + 1]
        values = [daily_count[dd] for dd in window_slice]
        current = daily_count[d]
        if len(values) <= 1:
            score = 50.0
        else:
            below = sum(1 for v in values if v < current)
            tied = sum(1 for v in values if v == current)
            # standard competition rank in percent
            score = (below + 0.5 * (tied - 1)) / (len(values) - 1) * 100 if len(values) > 1 else 50.0
            score = max(0.0, min(100.0, score))
        state = classify_state(score)
        out[d] = {
            "count": current,
            "score": round(score, 2),
            "state": state,
            "mult": POSITION_MULT[state],
        }
    return out


def validate_proxy(proxy: dict[str, dict]) -> dict | None:
    """Compare proxy state vs real Layer 1 scorer in the overlap window."""
    if not REAL_SENTIMENT_FP.exists():
        return None
    with gzip.open(REAL_SENTIMENT_FP, "rt") as f:
        real = pd.read_csv(f)
    overlap = []
    for _, row in real.iterrows():
        d = str(row["date"])
        if d not in proxy:
            continue
        overlap.append({
            "date": d,
            "proxy_state": proxy[d]["state"],
            "proxy_score": proxy[d]["score"],
            "proxy_count": proxy[d]["count"],
            "real_state": str(row["sentiment_state"]),
            "real_score": float(row["sentiment_score"]),
        })
    if not overlap:
        return None
    matched = sum(1 for o in overlap if o["proxy_state"] == o["real_state"])
    coarse_matched = 0
    coarse_order = {"ice": 0, "start": 1, "ferment": 2, "climax": 3}
    for o in overlap:
        if abs(coarse_order[o["proxy_state"]] - coarse_order[o["real_state"]]) <= 1:
            coarse_matched += 1
    print("\n=== Proxy vs real sentiment (overlap window) ===")
    print(f"{'date':<12} {'proxy':<10}{'real':<10}{'proxy_sc':>8}  {'real_sc':>7}  {'count':>5}")
    for o in overlap:
        tag = "✓" if o["proxy_state"] == o["real_state"] else " "
        print(f"{o['date']:<12} {o['proxy_state']:<10}{o['real_state']:<10}"
              f"{o['proxy_score']:>8.2f}  {o['real_score']:>7.2f}  {o['proxy_count']:>5}  {tag}")
    print(f"  exact_match: {matched}/{len(overlap)} = {100*matched/len(overlap):.1f}%")
    print(f"  within±1 bin: {coarse_matched}/{len(overlap)} = {100*coarse_matched/len(overlap):.1f}%")
    return {
        "overlap_days": len(overlap),
        "exact_match": matched,
        "exact_match_pct": round(100 * matched / len(overlap), 2),
        "within_1bin_match": coarse_matched,
        "within_1bin_match_pct": round(100 * coarse_matched / len(overlap), 2),
        "details": overlap,
    }


def summarize_filter(samples: list[dict], passed: list[dict], name: str) -> dict:
    n = len(passed)
    wins = sum(1 for s in passed if s["win"])
    mean_net = (sum(s["net_pct"] for s in passed) / n) if n else 0.0
    win_pct = (100.0 * wins / n) if n else 0.0
    share = (100.0 * n / len(samples)) if samples else 0.0
    return {
        "filter": name,
        "n": n,
        "wins": wins,
        "win_pct": round(win_pct, 2),
        "wilson95_lower_pct": round(wilson_lower(wins, n), 2),
        "mean_net_pct": round(mean_net, 4),
        "share_of_window_pct": round(share, 2),
    }


def monthly_portfolio(samples: list[dict], proxy: dict,
                       label: str,
                       mode: str = "none",
                       single_weight: float = 0.20,
                       max_concurrent: int = 5,
                       target_pct: float = 5.0) -> dict:
    """
    mode: 'none' (no L1), 'hard' (skip climax), 'soft' (scale weight by L1 mult).
    """
    by_d2: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_d2[s["d2_date"]].append(s)
    for d2, lst in by_d2.items():
        lst.sort(key=lambda x: x.get("d1_change_pct") or 0, reverse=True)

    monthly_ret: dict[str, float] = defaultdict(float)
    monthly_trades: dict[str, int] = defaultdict(int)
    monthly_wins: dict[str, int] = defaultdict(int)
    monthly_skipped_climax: dict[str, int] = defaultdict(int)

    for d2 in sorted(by_d2):
        picks = by_d2[d2][:max_concurrent]
        for s in picks:
            d1 = s["d1_date"]
            sent = proxy.get(d1)
            if mode == "hard" and sent and sent["state"] == "climax":
                monthly_skipped_climax[s["d2_date"][:7]] += 1
                continue
            if mode == "soft":
                mult = sent["mult"] if sent else 1.0
                if mult <= 0:
                    monthly_skipped_climax[s["d2_date"][:7]] += 1
                    continue
                weight = min(single_weight * mult, 1.0 / max_concurrent * 1.5)
            else:
                weight = single_weight
            month = s["d2_date"][:7]
            monthly_ret[month] += s["net_pct"] * weight
            monthly_trades[month] += 1
            if s["win"]:
                monthly_wins[month] += 1

    rows = []
    hits = 0
    for m in sorted(monthly_ret):
        ret = monthly_ret[m]
        trades = monthly_trades[m]
        wins = monthly_wins[m]
        hit = ret >= target_pct
        rows.append({
            "month": m,
            "n_trades": trades,
            "wins": wins,
            "win_pct": round(100.0 * wins / trades, 2) if trades else 0.0,
            "month_return_pct": round(ret, 4),
            "ge_target": hit,
            "skipped_climax": monthly_skipped_climax.get(m, 0),
        })
        if hit:
            hits += 1
    n = len(rows)
    hit_pct = 100.0 * hits / n if n else 0.0
    print(f"\n=== Monthly portfolio [{label}] mode={mode} target={target_pct}% ===")
    print(f"{'month':<10} {'n':>4}  {'skip':>4}  {'wins':>4}  win%   month_ret%   ≥target?")
    for r in rows:
        print(f"{r['month']:<10} {r['n_trades']:>4}  {r['skipped_climax']:>4}  {r['wins']:>4}  "
              f"{r['win_pct']:5.2f}  {r['month_return_pct']:+8.3f}    "
              f"{'YES' if r['ge_target'] else 'no'}")
    cum = 1.0
    for r in rows:
        cum *= (1.0 + r["month_return_pct"] / 100.0)
    cum_pct = (cum - 1.0) * 100.0
    print(f"  months={n}  hits={hits}  hit_rate={hit_pct:.2f}%  cum={cum_pct:+.3f}%")
    return {
        "mode": mode,
        "months": rows,
        "n_months": n,
        "hits_ge_target": hits,
        "hit_rate_pct": round(hit_pct, 2),
        "cum_return_pct": round(cum_pct, 4),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("loading candidates ...")
    with gzip.open(CAND_FP, "rt") as f:
        cands = json.load(f)
    print(f"  n={len(cands)}")

    print("building sentiment proxy (D1≥3% daily count, rolling pct) ...")
    proxy = build_sentiment_proxy(cands)
    state_dist = defaultdict(int)
    for d, v in proxy.items():
        state_dist[v["state"]] += 1
    print(f"  trading days with proxy: {len(proxy)}")
    print(f"  state distribution: {dict(state_dist)}")

    valid = validate_proxy(proxy)

    print("\nloading LHB index ...")
    lhb_idx = load_lhb()
    print(f"  LHB pairs: {len(lhb_idx)}")

    print("replaying samples ...")
    samples: list[dict] = []
    for c in cands:
        if c.get("d2_action") or c.get("d3_action"):
            continue
        s = build_sample(c, lhb_idx)
        if s is None:
            continue
        samples.append(s)
    print(f"  replayed={len(samples)}")

    final_samples = [s for s in samples
                     if pass_layer4_rule_a(s) and pass_layer5_lhb(s) and pass_layer6_gap(s)]
    final_holdout = [s for s in final_samples if s["d1_date"] >= HOLDOUT_START]
    final_in = [s for s in final_samples if s["d1_date"] <= IN_SAMPLE_END]

    def tag_sentiment(sample_list):
        """Return a copy of samples that pass L1 hard-filter (not climax)."""
        return [s for s in sample_list if (proxy.get(s["d1_date"]) or {}).get("state") != "climax"]

    # Single-trade stats under each stacking
    print("\n=== Single-trade stats (ALL / IN / HOLDOUT) × (no-L1 / +L1 hard) ===")
    print(f"{'window':<14}{'config':<22}{'n':>6} {'win%':>7} {'wilL':>7} {'mean_net%':>10}")
    rows = []
    for label, base in [("ALL", final_samples), ("IN", final_in), ("HOLDOUT", final_holdout)]:
        for cfg, sel in [("L4+L5+L6", base),
                         ("L4+L5+L6+L1hard", tag_sentiment(base))]:
            r = summarize_filter(base, sel, f"{label} / {cfg}")
            rows.append(r)
            print(f"{label:<14}{cfg:<22}{r['n']:>6} {r['win_pct']:>7.2f} "
                  f"{r['wilson95_lower_pct']:>7.2f} {r['mean_net_pct']:>+10.3f}")

    # Monthly simulation: 4 scenarios on each of the 3 windows (ALL/IN/HOLDOUT)
    scenarios = []
    for label, pool in [("ALL", final_samples), ("IN", final_in), ("HOLDOUT", final_holdout)]:
        scenarios.append((f"{label} / no-L1", pool, "none"))
        scenarios.append((f"{label} / L1 hard", pool, "hard"))
        scenarios.append((f"{label} / L1 soft", pool, "soft"))
    monthly_results = []
    for label, pool, mode in scenarios:
        res = monthly_portfolio(pool, proxy, label, mode=mode)
        monthly_results.append({"label": label, **res})

    report = {
        "fee_pct": FEE_PCT,
        "proxy_config": {
            "feature": "daily count of D1>=3% stocks (lookahead-free)",
            "rolling_window_days": PROXY_WINDOW,
            "state_bins": "ice(<25) start(25-50) ferment(50-75) climax(>=75)",
            "position_mult": POSITION_MULT,
        },
        "proxy_state_distribution": dict(state_dist),
        "proxy_validation_vs_real": valid,
        "single_trade_stats": rows,
        "monthly_simulation": monthly_results,
    }
    OUT_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_REPORT}")


if __name__ == "__main__":
    main()
