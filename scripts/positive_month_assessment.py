"""Target reassessment #3: 月度正收益率 ≥ 60% 可达吗?

New target: P(month_return > 0) >= 60%, instead of P(month_return >= 5%) >= 80%.
Also report P(month > 0) >= 50% and P(beat CSI300) as auxiliary metrics.

Config universe (reuse all prior work):
  C1 L0 baseline (no filter)
  C2 L4+L5+L6 fusion (D3 open exit)
  C3 L4+L5+L6 + L1 soft (D1>=3% count proxy)
  C4 L4+L5+L6 + L7 (TP=5%/SL=3%)
  C5 L4+L5+L6 + L7 (TP=7%/SL=3%)

Each config evaluated on ALL / IN / HOLDOUT windows.

Output: research/fusion-mvp/positive_month_report.json
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mvp_fusion_backtest import (  # type: ignore
    load_lhb, pass_layer4_rule_a, pass_layer5_lhb, pass_layer6_gap,
    FEE_PCT, IN_SAMPLE_END, HOLDOUT_START, CAND_FP, MIN_DIR,
    extract_d1_features, is_sealed, get_d1_close,
)
from mvp_fusion_backtest_with_l1 import build_sentiment_proxy, POSITION_MULT  # type: ignore
from layer7_dynamic_exit import simulate_intraday_exit  # type: ignore

OUT_FP = ROOT / "research" / "fusion-mvp" / "positive_month_report.json"

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


def build_all_samples(cands: list[dict], lhb_idx: dict) -> list[dict]:
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
        d3_open = d3m["prices"][0]
        if buy <= 0 or d3_open <= 0:
            continue
        gap_pct_d2 = (buy / d1_close - 1.0) * 100.0
        lhb = lhb_idx.get((c["symbol"], c["d1_date"]))
        d3_net = net_pct(buy, d3_open)
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
            "d2_prices": d2m["prices"],
            "d3_open": d3_open,
            "d3_net_pct": d3_net,
            "lhb_bad_reason": bool(lhb and lhb["lhb_bad_reason"]),
            "lhb_bad_inst_sell": bool(lhb and lhb["lhb_bad_inst_sell"]),
        }
        out.append(sample)
    return out


def compute_exits(samples: list[dict], tp: float | None = None,
                   sl: float | None = None) -> list[dict]:
    """Attach net_pct based on either D3 open (if tp/sl both None) or L7 TP/SL."""
    out = []
    for s in samples:
        if tp is None or sl is None:
            np_pct = s["d3_net_pct"]
        else:
            exit_price, _ = simulate_intraday_exit(s["d2_prices"], s["d3_open"], tp, sl)
            np_pct = net_pct(s["buy"], exit_price)
        out.append({**s, "net_pct": np_pct, "win": np_pct > 0})
    return out


def monthly_returns(samples: list[dict], proxy: dict | None,
                     mode: str = "none") -> dict[str, float]:
    """mode in {'none', 'soft'}; soft uses L1 proxy multiplier."""
    by_d2: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_d2[s["d2_date"]].append(s)
    for lst in by_d2.values():
        lst.sort(key=lambda x: x.get("d1_change_pct") or 0, reverse=True)
    ret: dict[str, float] = defaultdict(float)
    for d2 in sorted(by_d2):
        for s in by_d2[d2][:MAX_CONCURRENT]:
            if mode == "soft":
                mult = (proxy.get(s["d1_date"]) or {}).get("mult", 1.0) if proxy else 1.0
                if mult <= 0:
                    continue
                weight = min(SINGLE_WEIGHT * mult, 1.0 / MAX_CONCURRENT * 1.5)
            else:
                weight = SINGLE_WEIGHT
            ret[s["d2_date"][:7]] += s["net_pct"] * weight
    return ret


def hit_stats(monthly_ret: dict[str, float], window_pred,
              target: float = 0.0) -> dict:
    rows = [(m, r) for m, r in sorted(monthly_ret.items()) if window_pred(m)]
    n = len(rows)
    hits = sum(1 for _, r in rows if r > target)
    hit_pct = 100.0 * hits / n if n else 0.0
    wl = wilson_lower(hits, n)
    cum = 1.0
    for _, r in rows:
        cum *= (1.0 + r / 100.0)
    cum_pct = (cum - 1.0) * 100.0
    mean = sum(r for _, r in rows) / n if n else 0.0
    return {
        "n_months": n,
        "hits_positive": hits,
        "hit_rate_pct": round(hit_pct, 2),
        "wilson95_lower_pct": round(wl, 2),
        "mean_month_return_pct": round(mean, 4),
        "cum_return_pct": round(cum_pct, 4),
        "months": [{"m": m, "ret_pct": round(r, 4), "positive": r > 0} for m, r in rows],
    }


def main() -> None:
    OUT_FP.parent.mkdir(parents=True, exist_ok=True)

    print("loading candidates ...")
    with gzip.open(CAND_FP, "rt") as f:
        cands = json.load(f)
    print(f"  n={len(cands)}")

    proxy = build_sentiment_proxy(cands)
    print(f"proxy days={len(proxy)}")

    print("loading LHB ...")
    lhb_idx = load_lhb()

    print("building sample universe ...")
    all_samples = build_all_samples(cands, lhb_idx)
    print(f"  n={len(all_samples)}")

    # Pre-compute net_pct for D3-exit and L7 variants
    all_d3 = compute_exits(all_samples)
    fus_mask = [pass_layer4_rule_a(s) and pass_layer5_lhb(s) and pass_layer6_gap(s)
                for s in all_samples]
    fus_samples = [s for s, keep in zip(all_samples, fus_mask) if keep]

    print(f"  fusion samples (L4+L5+L6): {len(fus_samples)}")

    fus_d3 = compute_exits(fus_samples)
    fus_l7_5_3 = compute_exits(fus_samples, tp=0.05, sl=0.03)
    fus_l7_7_3 = compute_exits(fus_samples, tp=0.07, sl=0.03)

    configs = [
        ("C1 L0 baseline",        all_d3,       "none"),
        ("C2 L4+L5+L6 (D3 exit)", fus_d3,       "none"),
        ("C3 L4+L5+L6 + L1 soft", fus_d3,       "soft"),
        ("C4 L4+L5+L6 + L7(5/3)", fus_l7_5_3,   "none"),
        ("C5 L4+L5+L6 + L7(7/3)", fus_l7_7_3,   "none"),
    ]

    windows = [
        ("ALL",     lambda m: True),
        ("IN",      lambda m: m <= IN_SAMPLE_END[:7]),
        ("HOLDOUT", lambda m: m >= HOLDOUT_START[:7]),
    ]

    print(f"\n=== Target: P(month_return > 0) >= 60% ===")
    print(f"{'config':<28}{'window':<10}{'nM':>4} {'wins':>5} {'hit%':>7} "
          f"{'wilL%':>7} {'mean%':>8} {'cum%':>9}")
    report_rows = []
    for name, samples, mode in configs:
        monthly = monthly_returns(samples, proxy, mode)
        for wname, pred in windows:
            r = hit_stats(monthly, pred)
            report_rows.append({
                "config": name,
                "window": wname,
                "mode": mode,
                **r,
            })
            print(f"{name:<28}{wname:<10}{r['n_months']:>4} {r['hits_positive']:>5} "
                  f"{r['hit_rate_pct']:>6.2f}% {r['wilson95_lower_pct']:>6.2f}% "
                  f"{r['mean_month_return_pct']:>+7.3f}% {r['cum_return_pct']:>+8.3f}%")

    # Rank best by hit_rate (ALL window) & mean month return
    all_scope = [r for r in report_rows if r["window"] == "ALL"]
    all_scope.sort(key=lambda x: (x["hit_rate_pct"], x["mean_month_return_pct"]), reverse=True)
    print("\n=== Ranking by hit_rate (ALL) ===")
    for i, r in enumerate(all_scope, 1):
        reach_60 = "✓" if r["hit_rate_pct"] >= 60 else "✗"
        reach_50 = "✓" if r["hit_rate_pct"] >= 50 else "✗"
        print(f"  #{i} {r['config']:<28}  hit={r['hit_rate_pct']:>5.2f}%  "
              f"wilsonL={r['wilson95_lower_pct']:>5.2f}%  mean={r['mean_month_return_pct']:>+7.3f}%  "
              f"≥60%:{reach_60} ≥50%:{reach_50}")

    report = {
        "target_rule": "P(month_return > 0) >= 60%",
        "aux_targets": ["P(month > 0) >= 50%", "any config reaching ≥60%?"],
        "portfolio": {"single_weight": SINGLE_WEIGHT, "max_concurrent": MAX_CONCURRENT},
        "notes": [
            "Baseline uses the full not-sealed 3%+ universe (no filter).",
            "L1 soft uses D1>=3% count proxy (does NOT match real sentiment scorer, see doc 21).",
            "L7 uses minute-close proxy for high/low (optimistic vs real tick fills).",
        ],
        "results": report_rows,
    }
    OUT_FP.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_FP}")


if __name__ == "__main__":
    main()
