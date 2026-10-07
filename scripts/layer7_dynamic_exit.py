"""Layer 7: T+1-compliant dynamic exit on D3.

Reuses the live-equivalent fusion universe (L2+L4+L6, with L5 disabled) and
replays each trade on D3
minute bars:
  buy at D2 minutes[0]
  for each D3 minute through 14:54:
    if high_i >= buy * (1 + TP):  exit at buy*(1+TP), log 'tp'
    elif low_i  <= buy * (1 - SL): exit at buy*(1-SL), log 'sl'
  if neither triggered: send 14:55 force-exit and proxy at D3 14:56

Because our cached minutes only store 'prices' (close per minute) & volumes,
we approximate per-minute high/low with close vs close transitions. Since
TDX free doesn't give OHLC per minute, we conservatively use `prices[i]` as
both high and low proxy and additionally test whether crossing the TP/SL
threshold happens between prices[i-1] and prices[i].

Grid of (TP, SL) tested on the live-equivalent L4+L6 fusion sample:
  TP in {0.03, 0.05, 0.07}
  SL in {0.02, 0.03, 0.05}

Metrics: single-trade win% / mean_net% + monthly 20%/5-concurrent portfolio
hit rate for month>=5% and quarter>=10%.

Output: research/fusion-mvp/layer7_report.json
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
sys.path.insert(0, str(ROOT / "src"))
from banxia_strategy.layer7_exit import simulate_t1_layer7_exit
from mvp_fusion_backtest import (  # type: ignore
    pass_layer4_rule_a, pass_layer6_gap,
    FEE_PCT, IN_SAMPLE_END, HOLDOUT_START, CAND_FP, MIN_DIR, DAILY_DIR,
    extract_d1_features, is_sealed, get_d1_close,
)

OUT_FP = ROOT / "research" / "fusion-mvp" / "layer7_report.json"
OUT_TRADES = ROOT / "research" / "fusion-mvp" / "layer7_trades.csv.gz"


def load_minutes(symbol: str, day: str) -> dict | None:
    fp = MIN_DIR / f"{day}_{symbol}.json.gz"
    if not fp.exists():
        return None
    with gzip.open(fp, "rt") as f:
        return json.load(f)


def simulate_intraday_exit(
    buy_price: float,
    d3_prices: list[float],
    tp_pct: float,
    sl_pct: float,
) -> tuple[float, str]:
    price, reason, _ = simulate_t1_layer7_exit(
        buy_price,
        d3_prices,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
    )
    return price, reason


def net_pct(buy: float, exit_price: float) -> float:
    return (exit_price / buy - 1.0) * 100.0 - FEE_PCT


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * (center - margin) / denom


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
            "name": c.get("name", ""),
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


def evaluate_grid(samples: list[dict], grid: list[tuple[float, float]]) -> list[dict]:
    results = []
    for tp, sl in grid:
        trades = []
        for s in samples:
            exit_price, reason = simulate_intraday_exit(
                s["buy"], s["d3_prices"], tp, sl
            )
            np_pct = net_pct(s["buy"], exit_price)
            trades.append({
                "d1_date": s["d1_date"],
                "d2_date": s["d2_date"],
                "symbol": s["symbol"],
                "d1_change_pct": s["d1_change_pct"],
                "net_pct": np_pct,
                "win": np_pct > 0,
                "exit_reason": reason,
            })
        n = len(trades)
        wins = sum(1 for t in trades if t["win"])
        win_pct = 100 * wins / n if n else 0.0
        mean_net = sum(t["net_pct"] for t in trades) / n if n else 0.0
        reasons = defaultdict(int)
        for t in trades:
            reasons[t["exit_reason"]] += 1
        results.append({
            "tp_pct": tp,
            "sl_pct": sl,
            "n": n,
            "wins": wins,
            "win_pct": round(win_pct, 2),
            "wilson95_lower_pct": round(wilson_lower(wins, n), 2),
            "mean_net_pct": round(mean_net, 4),
            "exit_distribution": dict(reasons),
            "trades": trades,
        })
    return results


def monthly_hit(trades: list[dict], target: float = 5.0,
                single_weight: float = 0.20, max_concurrent: int = 5) -> dict:
    by_d2: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_d2[t["d2_date"]].append(t)
    for lst in by_d2.values():
        lst.sort(key=lambda x: x.get("d1_change_pct") or 0, reverse=True)
    monthly: dict[str, float] = defaultdict(float)
    for d2 in sorted(by_d2):
        for t in by_d2[d2][:max_concurrent]:
            monthly[t["d2_date"][:7]] += t["net_pct"] * single_weight
    rows = sorted(monthly.items())
    hits = sum(1 for _, r in rows if r >= target)
    cum = 1.0
    for _, r in rows:
        cum *= (1.0 + r / 100.0)
    return {
        "n_months": len(rows),
        "hits": hits,
        "hit_rate_pct": round(100 * hits / len(rows), 2) if rows else 0.0,
        "wilson95_lower_pct": round(wilson_lower(hits, len(rows)), 2),
        "cum_pct": round((cum - 1.0) * 100.0, 4),
        "months": [{"m": m, "ret_pct": round(r, 4), "ge_target": r >= target} for m, r in rows],
    }


def quarterly_hit(monthly_rows: list[dict], target: float = 10.0) -> dict:
    by_q: dict[str, list[float]] = defaultdict(list)
    for r in monthly_rows:
        y, m = r["m"].split("-")
        q = f"{y}-Q{(int(m)-1)//3+1}"
        by_q[q].append(r["ret_pct"])
    rows = []
    for q in sorted(by_q):
        cum = 1.0
        for v in by_q[q]:
            cum *= (1.0 + v / 100.0)
        qret = (cum - 1.0) * 100.0
        rows.append({"q": q, "ret_pct": round(qret, 4), "ge_target": qret >= target})
    hits = sum(1 for r in rows if r["ge_target"])
    cum = 1.0
    for r in rows:
        cum *= (1.0 + r["ret_pct"] / 100.0)
    return {
        "n_quarters": len(rows),
        "hits": hits,
        "hit_rate_pct": round(100 * hits / len(rows), 2) if rows else 0.0,
        "wilson95_lower_pct": round(wilson_lower(hits, len(rows)), 2),
        "cum_pct": round((cum - 1.0) * 100.0, 4),
        "quarters": rows,
    }


def main() -> None:
    OUT_FP.parent.mkdir(parents=True, exist_ok=True)

    print("loading candidates ...")
    with gzip.open(CAND_FP, "rt") as f:
        cands = json.load(f)
    print(f"  n={len(cands)}")

    print("building live-equivalent fusion (L4+L6; L5 disabled) samples ...")
    samples = build_fusion_samples(cands)
    print(f"  n={len(samples)}")

    grid = [
        (0.03, 0.02), (0.03, 0.03),
        (0.05, 0.02), (0.05, 0.03), (0.05, 0.05),
        (0.07, 0.03), (0.07, 0.05),
    ]
    # Also baseline: impossible thresholds, which falls back to D3 14:56.
    print("\nevaluating grid ...")
    results = evaluate_grid(samples, grid)

    # Baseline (no L7): impossible TP/SL, force exit at D3 14:56.
    baseline = evaluate_grid(samples, [(10.0, 10.0)])[0]  # 1000% TP / 1000% SL = never fires
    baseline["label"] = "baseline (D3 14:56 only)"
    print(f"\nBaseline (D3 14:56): n={baseline['n']} win%={baseline['win_pct']}  "
          f"mean_net%={baseline['mean_net_pct']}")

    print(f"\n{'TP%':>5} {'SL%':>5} {'n':>5} {'win%':>7} {'wilL%':>7} "
          f"{'mean_net%':>10}  exit_dist")
    for r in results:
        print(f"{r['tp_pct']*100:>5.1f} {r['sl_pct']*100:>5.1f} {r['n']:>5} "
              f"{r['win_pct']:>7.2f} {r['wilson95_lower_pct']:>7.2f} "
              f"{r['mean_net_pct']:>+10.3f}  {r['exit_distribution']}")

    # Monthly / quarterly for each grid config, split by window
    print("\n=== Monthly/Quarterly hit rates (ALL / IN / HOLDOUT) ===")
    print(f"{'TP':>4} {'SL':>4} {'win%':>6} {'mean%':>7} "
          f"{'All m≥5':>8} {'In m≥5':>8} {'Hld m≥5':>8} "
          f"{'All q≥10':>9} {'In q≥10':>9} {'Hld q≥10':>9}")

    def filter_window(trades, pred):
        return [t for t in trades if pred(t)]

    rows_report = []
    all_pred = lambda t: True
    in_pred = lambda t: t["d1_date"] <= IN_SAMPLE_END
    hd_pred = lambda t: t["d1_date"] >= HOLDOUT_START

    for r in [baseline] + results:
        trades = r["trades"]
        def q_for(pred):
            tr = filter_window(trades, pred)
            m = monthly_hit(tr, target=5.0)
            q = quarterly_hit(m["months"], target=10.0)
            return m, q
        m_all, q_all = q_for(all_pred)
        m_in, q_in = q_for(in_pred)
        m_hd, q_hd = q_for(hd_pred)
        row = {
            "tp_pct": r.get("tp_pct"),
            "sl_pct": r.get("sl_pct"),
            "label": r.get("label", f"TP={r['tp_pct']*100:.1f}% SL={r['sl_pct']*100:.1f}%"),
            "n_trades": r["n"],
            "win_pct": r["win_pct"],
            "mean_net_pct": r["mean_net_pct"],
            "exit_distribution": r["exit_distribution"],
            "all": {"monthly": m_all, "quarterly": q_all},
            "in": {"monthly": m_in, "quarterly": q_in},
            "holdout": {"monthly": m_hd, "quarterly": q_hd},
        }
        rows_report.append(row)
        label = row["label"][:14]
        tp = r.get("tp_pct", 10.0) * 100
        sl = r.get("sl_pct", 10.0) * 100
        print(f"{tp:>4.1f} {sl:>4.1f} {r['win_pct']:>6.2f} {r['mean_net_pct']:>+7.3f} "
              f"{m_all['hit_rate_pct']:>7.2f}% {m_in['hit_rate_pct']:>7.2f}% {m_hd['hit_rate_pct']:>7.2f}% "
              f"{q_all['hit_rate_pct']:>8.2f}% {q_in['hit_rate_pct']:>8.2f}% {q_hd['hit_rate_pct']:>8.2f}%")

    # Trim trades from JSON (too large)
    for r in rows_report:
        pass  # trades already not stored at this level
    # Also strip `trades` from the single-trade `results` before dumping
    slim_grid = []
    for r in results:
        slim_grid.append({k: v for k, v in r.items() if k != "trades"})
    slim_baseline = {k: v for k, v in baseline.items() if k != "trades"}

    report = {
        "fee_pct": FEE_PCT,
        "note": (
            "T+1-compliant dynamic exit on D3 (TP/SL on minute close proxy), "
            "with a 14:55 force-exit instruction proxied at 14:56. "
            "Minute close prices used as both high & low proxies for threshold "
            "crossing. Opening gaps beyond a threshold exit at the observed "
            "first-minute price."
        ),
        "sample_universe": (
            "fusion-l7-v1 live-equivalent L4+L6 samples; "
            "L5 LHB check disabled with pass-through"
        ),
        "n_samples": len(samples),
        "grid_single_trade": slim_grid,
        "baseline_no_l7_single_trade": slim_baseline,
        "monthly_quarterly": rows_report,
    }
    OUT_FP.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_FP}")


if __name__ == "__main__":
    main()
