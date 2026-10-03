"""MVP fusion backtest: Layer 2 + 4 + 5 + 6.

Chain (strict lookahead-free, each layer uses only pre-D2-open info):
  Layer 2 — D1 initial screen: d1_change_pct > 3 (already baked into
            candidates.json.gz universe) + drop sealed limit-up.
  Layer 4 — Rule A: D1 last-30-min r_last30<-0.5% & close_loc<0.75.
  Layer 5 — LHB反向过滤: 排除 D1 上榜且上榜原因含"振幅" 或 "跌幅",
            也排除 D1 机构卖出 (lhb_inst_sell_any).
  Layer 6 — D2 开盘跳空闸门: gap_pct_d2 ∈ [-1.0, +4.0]. 过度低开(破位)和
            过度高开(透支)都剔除. 边界取自 analyze_quasi_auction 的分布.

Trade logic (unchanged):
  Buy  D2 09:31  (= minutes[0] on D2)
  Sell D3 09:31
  Fee  0.30% round-trip

Outputs:
  research/fusion-mvp/report.json
  research/fusion-mvp/trades.csv.gz      (per-trade, holdout only)
  Per-month净收益 (holdout) for 月度≥5% 命中率估算.

Not touched:
  - src/banxia_strategy/monthly_target_research.py
  - docs/research/monthly-5pct-protocol.json
  - research/monthly-target-v1/, research/monthly-target-v1-3pct/ 的输入资料
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_d1_last30min import extract_d1_features  # type: ignore

CAND_FP = ROOT / "research" / "monthly-target-v1-3pct" / "candidates.json.gz"
MIN_DIR = ROOT / "research" / "monthly-target-v1-3pct" / "minutes"
DAILY_DIR = ROOT / "research" / "monthly-target-v1-3pct" / "daily"
LHB_DIR = ROOT / "research" / "auction-features" / "lhb_cache"
OUT_DIR = ROOT / "research" / "fusion-mvp"
OUT_REPORT = OUT_DIR / "report.json"
OUT_TRADES = OUT_DIR / "trades.csv.gz"

FEE_PCT = 0.30
IN_SAMPLE_END = "2026-06-30"
HOLDOUT_START = "2026-07-01"

# Layer 6 D2 gap gate — 低于 -1% 视为破位, 高于 +4% 视为透支
GAP_MIN_PCT = -1.0
GAP_MAX_PCT = 4.0

# Layer 4 Rule A thresholds
RULE_A_R_LAST30 = -0.5
RULE_A_CLOSE_LOC = 0.75


_daily_cache: dict[str, dict[str, float]] = {}


def load_minutes(symbol: str, day: str) -> dict | None:
    fp = MIN_DIR / f"{day}_{symbol}.json.gz"
    if not fp.exists():
        return None
    with gzip.open(fp, "rt") as f:
        return json.load(f)


def get_d1_close(symbol: str, d1_date: str) -> float | None:
    if symbol not in _daily_cache:
        fp = DAILY_DIR / f"{symbol}.json.gz"
        if not fp.exists():
            _daily_cache[symbol] = {}
            return None
        d = json.load(gzip.open(fp, "rt"))
        _daily_cache[symbol] = {b["datetime"][:10]: float(b["close"]) for b in d.get("bars", [])}
    return _daily_cache[symbol].get(d1_date)


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


def load_lhb() -> dict[tuple[str, str], dict]:
    rows: list[pd.DataFrame] = []
    for fp in sorted(LHB_DIR.glob("*.csv.gz")):
        df = pd.read_csv(gzip.open(fp, "rt"))
        rows.append(df)
    if not rows:
        return {}
    df = pd.concat(rows, ignore_index=True)
    df["symbol"] = df["代码"].astype(int).map(lambda x: f"{x:06d}")
    df["d1_date"] = df["上榜日"].astype(str).str[:10]
    reason_amp = df["上榜原因"].fillna("").str.contains("振幅")
    reason_down = df["上榜原因"].fillna("").str.contains("跌幅")
    inst_sell = df["解读"].fillna("").str.contains("卖出") & df["解读"].fillna("").str.contains("机构")
    df["bad_reason"] = reason_amp | reason_down
    df["bad_inst_sell"] = inst_sell
    agg = df.groupby(["symbol", "d1_date"]).agg(
        lhb_bad_reason=("bad_reason", "any"),
        lhb_bad_inst_sell=("bad_inst_sell", "any"),
        lhb_hit=("代码", "size"),
    ).reset_index()
    return {
        (r.symbol, r.d1_date): {
            "lhb_hit": int(r.lhb_hit),
            "lhb_bad_reason": bool(r.lhb_bad_reason),
            "lhb_bad_inst_sell": bool(r.lhb_bad_inst_sell),
        }
        for r in agg.itertuples()
    }


def build_sample(c: dict, lhb_idx: dict) -> dict | None:
    d1m = load_minutes(c["symbol"], c["d1_date"])
    d2m = load_minutes(c["symbol"], c["d2_date"])
    d3m = load_minutes(c["symbol"], c["d3_date"])
    if d1m is None or d2m is None or d3m is None:
        return None
    if (d2m.get("volumes") or [0])[0] <= 0 or (d3m.get("volumes") or [0])[0] <= 0:
        return None
    feats = extract_d1_features(d1m)
    if feats is None:
        return None
    if is_sealed(feats):
        return None
    d1_close = get_d1_close(c["symbol"], c["d1_date"])
    if d1_close is None or d1_close <= 0:
        return None
    buy = d2m["prices"][0]
    sell = d3m["prices"][0]
    if buy <= 0 or sell <= 0:
        return None
    gap_pct_d2 = (buy / d1_close - 1.0) * 100.0
    net_pct = (sell / buy - 1.0) * 100.0 - FEE_PCT
    lhb = lhb_idx.get((c["symbol"], c["d1_date"]))
    return {
        "symbol": c["symbol"],
        "name": c.get("name", ""),
        "d1_date": c["d1_date"],
        "d2_date": c["d2_date"],
        "d3_date": c["d3_date"],
        "d1_change_pct": c["d1_change_pct"],
        "r_last30_pct": feats.get("r_last30_pct"),
        "close_location_day": feats.get("close_location_day"),
        "gap_pct_d2": gap_pct_d2,
        "net_pct": net_pct,
        "win": net_pct > 0,
        "lhb_bad_reason": bool(lhb and lhb["lhb_bad_reason"]),
        "lhb_bad_inst_sell": bool(lhb and lhb["lhb_bad_inst_sell"]),
    }


def pass_layer2(s: dict) -> bool:
    # D1 ≥ 3% already enforced by candidates universe, drop sealed is in build_sample.
    return (s.get("d1_change_pct") or 0) >= 3.0


def pass_layer4_rule_a(s: dict) -> bool:
    r = s.get("r_last30_pct")
    cl = s.get("close_location_day")
    return r is not None and cl is not None and r < RULE_A_R_LAST30 and cl < RULE_A_CLOSE_LOC


def pass_layer5_lhb(s: dict) -> bool:
    return not s["lhb_bad_reason"] and not s["lhb_bad_inst_sell"]


def pass_layer6_gap(s: dict) -> bool:
    g = s.get("gap_pct_d2")
    return g is not None and GAP_MIN_PCT <= g <= GAP_MAX_PCT


LAYER_FILTERS = [
    ("L0 baseline (not_sealed, 3%+)", lambda s: True),
    ("L4 Rule A", pass_layer4_rule_a),
    ("L4+L5 (LHB clean)", lambda s: pass_layer4_rule_a(s) and pass_layer5_lhb(s)),
    ("L4+L5+L6 (gap gate)",
     lambda s: pass_layer4_rule_a(s) and pass_layer5_lhb(s) and pass_layer6_gap(s)),
    ("L4+L6 only", lambda s: pass_layer4_rule_a(s) and pass_layer6_gap(s)),
    ("L5 only (LHB clean)", pass_layer5_lhb),
    ("L6 only (gap gate)", pass_layer6_gap),
]


def summarize(samples: list[dict], label: str) -> list[dict]:
    print(f"\n=== {label}: {len(samples)} samples ===")
    print(f"  {'filter':<42s}  {'n':>5s}  win%   wilsonL  mean_net%   share%")
    out = []
    for name, fn in LAYER_FILTERS:
        passed = [s for s in samples if fn(s)]
        n = len(passed)
        wins = sum(1 for s in passed if s["win"])
        mean_net = (sum(s["net_pct"] for s in passed) / n) if n else 0.0
        win_pct = (100.0 * wins / n) if n else 0.0
        share = (100.0 * n / len(samples)) if samples else 0.0
        r = {
            "filter": name,
            "n": n,
            "wins": wins,
            "win_pct": round(win_pct, 2),
            "wilson95_lower_pct": round(wilson_lower(wins, n), 2),
            "mean_net_pct": round(mean_net, 4),
            "share_of_window_pct": round(share, 2),
        }
        out.append(r)
        print(f"  {name:<42s}  {n:>5d}  {win_pct:5.2f}  {r['wilson95_lower_pct']:5.2f}  "
              f"{mean_net:+7.3f}   {share:5.2f}")
    return out


def monthly_portfolio_simulation(samples: list[dict], label: str,
                                  single_name_weight: float = 0.20,
                                  max_concurrent: int = 5,
                                  monthly_target_pct: float = 5.0) -> dict:
    """Simulate equal-weight month-level portfolio returns.

    Each D1 signal → one buy on D2 at open, one sell on D3 at open.
    Portfolio is capital-weighted: each new position consumes 20% of total
    capital, capped at 5 concurrent positions (总仓位最多 100%).
    If more than 5 signals land on the same D2, we take the first-N arrival
    (sorted by d1_change_pct desc) — approximates "挑最强 N 只".

    Monthly return = sum of individual trade net% × position weight for that trade.
    """
    # Bucket signals by d2 date and sort within each date by d1_change_pct desc
    by_d2: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_d2[s["d2_date"]].append(s)
    for d2, lst in by_d2.items():
        lst.sort(key=lambda x: x.get("d1_change_pct") or 0, reverse=True)

    # Daily position bucket: picks up to max_concurrent
    monthly_returns: dict[str, float] = defaultdict(float)
    monthly_trade_count: dict[str, int] = defaultdict(int)
    monthly_wins: dict[str, int] = defaultdict(int)
    for d2 in sorted(by_d2):
        picks = by_d2[d2][:max_concurrent]
        for s in picks:
            month = s["d2_date"][:7]
            monthly_returns[month] += s["net_pct"] * single_name_weight
            monthly_trade_count[month] += 1
            if s["win"]:
                monthly_wins[month] += 1

    rows = []
    hits = 0
    for m in sorted(monthly_returns):
        ret = monthly_returns[m]
        trades = monthly_trade_count[m]
        wins = monthly_wins[m]
        hit = ret >= monthly_target_pct
        rows.append({
            "month": m,
            "n_trades": trades,
            "wins": wins,
            "win_pct": round(100.0 * wins / trades, 2) if trades else 0.0,
            "month_return_pct": round(ret, 4),
            "ge_target": hit,
        })
        if hit:
            hits += 1
    n = len(rows)
    hit_pct = 100.0 * hits / n if n else 0.0
    print(f"\n=== Monthly portfolio ({label}) — single_weight={single_name_weight} "
          f"max_concurrent={max_concurrent} target={monthly_target_pct}% ===")
    print(f"{'month':<10} {'n':>4}  {'wins':>4}  win%   month_ret%   ≥target?")
    for r in rows:
        print(f"{r['month']:<10} {r['n_trades']:>4}  {r['wins']:>4}  "
              f"{r['win_pct']:5.2f}  {r['month_return_pct']:+8.3f}    "
              f"{'YES' if r['ge_target'] else 'no'}")
    cum = 1.0
    for r in rows:
        cum *= (1.0 + r["month_return_pct"] / 100.0)
    cum_pct = (cum - 1.0) * 100.0
    print(f"\n  months_total={n}  hits={hits}  hit_rate={hit_pct:.2f}%  "
          f"cum_return={cum_pct:+.3f}%")
    return {
        "months": rows,
        "n_months": n,
        "hits_ge_target": hits,
        "hit_rate_pct": round(hit_pct, 2),
        "cum_return_pct": round(cum_pct, 4),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("loading LHB index ...")
    lhb_idx = load_lhb()
    print(f"  LHB (symbol, d1_date) pairs: {len(lhb_idx)}")

    print("loading 3% candidates ...")
    with gzip.open(CAND_FP, "rt") as f:
        cands = json.load(f)
    print(f"  candidates: {len(cands)}")

    print("replaying samples ...")
    samples: list[dict] = []
    skipped_corp = 0
    skipped_missing = 0
    skipped_sealed = 0
    for c in cands:
        if c.get("d2_action") or c.get("d3_action"):
            skipped_corp += 1
            continue
        s = build_sample(c, lhb_idx)
        if s is None:
            skipped_missing += 1  # includes sealed-skip inside build_sample
            continue
        samples.append(s)
    print(f"  replayed={len(samples)}  skipped_corp={skipped_corp}  skipped_missing_or_sealed={skipped_missing}")

    all_bucket = summarize(samples, "ALL (2025-01-01 ~ latest)")
    in_sample = [s for s in samples if s["d1_date"] <= IN_SAMPLE_END]
    holdout = [s for s in samples if s["d1_date"] >= HOLDOUT_START]
    in_res = summarize(in_sample, f"IN-SAMPLE (<= {IN_SAMPLE_END})")
    hold_res = summarize(holdout, f"HOLDOUT (>= {HOLDOUT_START})")

    # Final layer filtered set for monthly simulation
    final_samples = [s for s in samples
                     if pass_layer4_rule_a(s) and pass_layer5_lhb(s) and pass_layer6_gap(s)]
    final_in_sample = [s for s in final_samples if s["d1_date"] <= IN_SAMPLE_END]
    final_holdout = [s for s in final_samples if s["d1_date"] >= HOLDOUT_START]

    monthly_all = monthly_portfolio_simulation(final_samples, "L4+L5+L6 ALL")
    monthly_in = monthly_portfolio_simulation(final_in_sample, "L4+L5+L6 IN-SAMPLE")
    monthly_out = monthly_portfolio_simulation(final_holdout, "L4+L5+L6 HOLDOUT")

    # Also simulate baseline (L0) for comparison
    monthly_baseline = monthly_portfolio_simulation(
        [s for s in samples if s["d1_date"] >= HOLDOUT_START],
        "BASELINE HOLDOUT (no filter)")

    report = {
        "fee_pct": FEE_PCT,
        "config": {
            "layer2_d1_change_pct_min": 3.0,
            "layer4_rule_a_r_last30": RULE_A_R_LAST30,
            "layer4_rule_a_close_loc_max": RULE_A_CLOSE_LOC,
            "layer5_lhb_exclude": ["振幅上榜", "跌幅上榜", "机构卖出"],
            "layer6_gap_pct_range": [GAP_MIN_PCT, GAP_MAX_PCT],
            "portfolio_single_weight": 0.20,
            "portfolio_max_concurrent": 5,
            "monthly_target_pct": 5.0,
        },
        "windows": {
            "in_sample": ["2025-01-01", IN_SAMPLE_END],
            "holdout": [HOLDOUT_START, max((s["d1_date"] for s in holdout), default=None)],
        },
        "filter_stats": {
            "all": all_bucket,
            "in_sample": in_res,
            "holdout": hold_res,
        },
        "monthly_simulation": {
            "all_fusion": monthly_all,
            "in_sample_fusion": monthly_in,
            "holdout_fusion": monthly_out,
            "holdout_baseline_l0": monthly_baseline,
        },
        "total_samples": len(samples),
        "final_fusion_samples": len(final_samples),
    }
    OUT_REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_REPORT}")

    # Dump holdout trades CSV
    with gzip.open(OUT_TRADES, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "name", "d1_date", "d2_date", "d3_date",
                    "d1_change_pct", "r_last30_pct", "close_location_day",
                    "gap_pct_d2", "net_pct", "win"])
        for s in final_holdout:
            w.writerow([
                s["symbol"], s["name"], s["d1_date"], s["d2_date"], s["d3_date"],
                round(s["d1_change_pct"] or 0, 4),
                round(s["r_last30_pct"] or 0, 4),
                round(s["close_location_day"] or 0, 4),
                round(s["gap_pct_d2"] or 0, 4),
                round(s["net_pct"], 4),
                int(s["win"]),
            ])
    print(f"wrote {OUT_TRADES}  ({len(final_holdout)} holdout trades)")


if __name__ == "__main__":
    main()
