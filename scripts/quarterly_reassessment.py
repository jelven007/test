"""Quarterly target reassessment: 季度收益 ≥ 10% 的概率是否 ≥ 80%?

Reuses fusion-mvp sample construction & filters. Groups months into
calendar quarters (Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec),
computes quarterly return via compounding monthly 20%/5-concurrent
portfolio, then evaluates hit rate.

Configs compared:
  * L0 baseline (no filter)
  * L4+L5+L6 fusion
  * L4+L5+L6 + L1 soft (D1>=3% count proxy, 20d rolling pct)

Output:
  research/fusion-mvp/quarterly_report.json
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
    load_lhb, build_sample, pass_layer4_rule_a, pass_layer5_lhb, pass_layer6_gap,
    IN_SAMPLE_END, HOLDOUT_START, CAND_FP,
)
from mvp_fusion_backtest_with_l1 import build_sentiment_proxy, POSITION_MULT  # type: ignore

OUT_FP = ROOT / "research" / "fusion-mvp" / "quarterly_report.json"
QUARTERLY_TARGET_PCT = 10.0
SINGLE_WEIGHT = 0.20
MAX_CONCURRENT = 5


def month_to_quarter(month: str) -> str:
    y, m = month.split("-")
    q = (int(m) - 1) // 3 + 1
    return f"{y}-Q{q}"


def monthly_pool(samples: list[dict], mode: str, proxy: dict) -> dict[str, float]:
    """Return {month: month_return_pct}. mode in {'none','soft'}."""
    by_d2: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_d2[s["d2_date"]].append(s)
    for lst in by_d2.values():
        lst.sort(key=lambda x: x.get("d1_change_pct") or 0, reverse=True)
    ret: dict[str, float] = defaultdict(float)
    for d2 in sorted(by_d2):
        for s in by_d2[d2][:MAX_CONCURRENT]:
            if mode == "soft":
                mult = (proxy.get(s["d1_date"]) or {}).get("mult", 1.0)
                if mult <= 0:
                    continue
                weight = min(SINGLE_WEIGHT * mult, 1.0 / MAX_CONCURRENT * 1.5)
            else:
                weight = SINGLE_WEIGHT
            ret[s["d2_date"][:7]] += s["net_pct"] * weight
    return ret


def monthly_to_quarterly(monthly_ret: dict[str, float]) -> list[dict]:
    """Compound within each quarter."""
    by_q: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for m in sorted(monthly_ret):
        q = month_to_quarter(m)
        by_q[q].append((m, monthly_ret[m]))
    rows = []
    for q in sorted(by_q):
        months = by_q[q]
        cum = 1.0
        for _, r in months:
            cum *= (1.0 + r / 100.0)
        qret = (cum - 1.0) * 100.0
        rows.append({
            "quarter": q,
            "months": [{"m": m, "ret_pct": round(r, 4)} for m, r in months],
            "n_months": len(months),
            "quarter_return_pct": round(qret, 4),
            "ge_target": qret >= QUARTERLY_TARGET_PCT,
        })
    return rows


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return 100.0 * (center - margin) / denom


def report_scenario(name: str, samples: list[dict], mode: str,
                     proxy: dict, window_label: str) -> dict:
    monthly = monthly_pool(samples, mode, proxy)
    rows = monthly_to_quarterly(monthly)
    n = len(rows)
    hits = sum(1 for r in rows if r["ge_target"])
    hit_pct = 100.0 * hits / n if n else 0.0
    wl = wilson_lower(hits, n)
    cum = 1.0
    for r in rows:
        cum *= (1.0 + r["quarter_return_pct"] / 100.0)
    cum_pct = (cum - 1.0) * 100.0
    print(f"\n=== {window_label} / {name} ===")
    print(f"{'quarter':<10} {'n_m':>3}  qret%     ≥10%?")
    for r in rows:
        print(f"{r['quarter']:<10} {r['n_months']:>3}  "
              f"{r['quarter_return_pct']:+8.3f}    {'YES' if r['ge_target'] else 'no'}")
    print(f"  quarters={n}  hits={hits}  hit_rate={hit_pct:.2f}%  "
          f"wilsonL={wl:.2f}%  cum={cum_pct:+.3f}%")
    return {
        "window": window_label,
        "config": name,
        "mode": mode,
        "quarters": rows,
        "n_quarters": n,
        "hits_ge_target": hits,
        "hit_rate_pct": round(hit_pct, 2),
        "wilson95_lower_pct": round(wl, 2),
        "cum_return_pct": round(cum_pct, 4),
    }


def main() -> None:
    print("loading candidates ...")
    with gzip.open(CAND_FP, "rt") as f:
        cands = json.load(f)
    print(f"  n={len(cands)}")

    proxy = build_sentiment_proxy(cands)
    print(f"proxy days={len(proxy)}")

    print("loading LHB ...")
    lhb_idx = load_lhb()

    print("replaying samples ...")
    samples = []
    for c in cands:
        if c.get("d2_action") or c.get("d3_action"):
            continue
        s = build_sample(c, lhb_idx)
        if s is None:
            continue
        samples.append(s)
    print(f"  n={len(samples)}")

    baseline = samples  # L0
    fusion = [s for s in samples
              if pass_layer4_rule_a(s) and pass_layer5_lhb(s) and pass_layer6_gap(s)]

    windows = {
        "ALL": lambda s: True,
        "IN": lambda s: s["d1_date"] <= IN_SAMPLE_END,
        "HOLDOUT": lambda s: s["d1_date"] >= HOLDOUT_START,
    }
    scenarios = []
    for wname, pred in windows.items():
        base_pool = [s for s in baseline if pred(s)]
        fus_pool = [s for s in fusion if pred(s)]
        scenarios.append(report_scenario("L0 baseline", base_pool, "none", proxy, wname))
        scenarios.append(report_scenario("L4+L5+L6", fus_pool, "none", proxy, wname))
        scenarios.append(report_scenario("L4+L5+L6 + L1 soft", fus_pool, "soft", proxy, wname))

    report = {
        "target_quarterly_return_pct": QUARTERLY_TARGET_PCT,
        "target_hit_rate_pct": 80.0,
        "portfolio": {
            "single_weight": SINGLE_WEIGHT,
            "max_concurrent": MAX_CONCURRENT,
            "quarter_compound": True,
        },
        "l1_proxy_warning": (
            "D1>=3% count proxy does not match the real sentiment scorer "
            "(0/11 exact match in overlap). Treat L1-soft results as "
            "lower-bound exploratory, not evidence for the real L1."
        ),
        "scenarios": scenarios,
    }
    OUT_FP.parent.mkdir(parents=True, exist_ok=True)
    OUT_FP.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_FP}")

    print("\n=== SUMMARY ===")
    print(f"{'window':<10}{'config':<25}{'nQ':>4} {'hits':>5} {'hit%':>8} "
          f"{'wilsonL%':>10} {'cumRet%':>10}")
    for sc in scenarios:
        print(f"{sc['window']:<10}{sc['config']:<25}{sc['n_quarters']:>4} "
              f"{sc['hits_ge_target']:>5} {sc['hit_rate_pct']:>7.2f}% "
              f"{sc['wilson95_lower_pct']:>9.2f}% {sc['cum_return_pct']:>+9.3f}%")


if __name__ == "__main__":
    main()
