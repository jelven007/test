"""Join LHB (龙虎榜) history to D1>=3% candidates and test signal strength.

Each candidate (symbol, D1_date) is left-joined to any LHB rows where
上榜日 == D1_date. Features are only taken from fields known at D1 close
(not the ex-post "上榜后 N 日" columns, which are lookahead).

Then we replay the open/open trade (buy D2 09:31, sell D3 09:31, 0.30% RT fee),
compute win rate / mean net return per filter, and compare the LHB signal
on in-sample (<=2026-06-30) vs holdout (>=2026-07-01) windows.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_d1_last30min import extract_d1_features  # type: ignore

CAND_FP = ROOT / "research" / "monthly-target-v1-3pct" / "candidates.json.gz"
MIN_DIR = ROOT / "research" / "monthly-target-v1-3pct" / "minutes"
LHB_DIR = ROOT / "research" / "auction-features" / "lhb_cache"
OUT_FP = ROOT / "research" / "auction-features" / "lhb_report.json"

FEE_PCT = 0.30
HOLDOUT_START = "2026-07-01"
IN_SAMPLE_END = "2026-06-30"


def load_minutes(symbol: str, day: str) -> dict | None:
    fp = MIN_DIR / f"{day}_{symbol}.json.gz"
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


_INST_RE = re.compile(r"(\d+)\s*家机构")
_RANK_FIVE = "日换手率达到20%的前5只证券"
_WIDE = "日振幅值达到15%的前5只证券"
_UP = "日涨幅偏离值达到7%的前5只证券"
_DOWN = "日跌幅偏离值达到7%的前5只证券"


def extract_inst(text: str) -> int:
    if not isinstance(text, str):
        return 0
    m = _INST_RE.search(text)
    return int(m.group(1)) if m else 0


def load_lhb() -> pd.DataFrame:
    frames = []
    for fp in sorted(LHB_DIR.glob("*.csv.gz")):
        df = pd.read_csv(gzip.open(fp, "rt"))
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["symbol"] = df["代码"].astype(int).map(lambda x: f"{x:06d}")
    df["d1_date"] = df["上榜日"].astype(str).str[:10]
    df["lhb_inst_sell"] = df["解读"].fillna("").str.contains("卖出") & df["解读"].fillna("").str.contains("机构")
    df["lhb_inst_buy"] = df["解读"].fillna("").str.contains("买入") & df["解读"].fillna("").str.contains("机构")
    df["lhb_inst_count"] = df["解读"].fillna("").map(extract_inst)
    df["lhb_reason_up"] = df["上榜原因"].fillna("").str.contains("涨幅")
    df["lhb_reason_turnover"] = df["上榜原因"].fillna("").str.contains("换手率")
    df["lhb_reason_amp"] = df["上榜原因"].fillna("").str.contains("振幅")
    df["lhb_reason_down"] = df["上榜原因"].fillna("").str.contains("跌幅")
    # Aggregate per (symbol, d1_date): sum of净买额占比, max换手率, 累计上榜次数
    agg = df.groupby(["symbol", "d1_date"]).agg(
        lhb_net_buy_pct=("净买额占总成交比", "sum"),
        lhb_amount_pct=("成交额占总成交比", "max"),
        lhb_turnover=("换手率", "max"),
        lhb_mktcap_floating=("流通市值", "max"),
        lhb_rows=("代码", "size"),
        lhb_inst_sell_any=("lhb_inst_sell", "any"),
        lhb_inst_buy_any=("lhb_inst_buy", "any"),
        lhb_inst_count=("lhb_inst_count", "max"),
        lhb_reason_up=("lhb_reason_up", "any"),
        lhb_reason_turnover=("lhb_reason_turnover", "any"),
        lhb_reason_amp=("lhb_reason_amp", "any"),
        lhb_reason_down=("lhb_reason_down", "any"),
    ).reset_index()
    return agg


def within(x, lo, hi):
    return x is not None and lo <= x <= hi


def summarize(samples: list[dict], filters: list[tuple[str, callable]], label: str) -> list[dict]:
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
    print("loading LHB cache ...")
    lhb = load_lhb()
    print(f"LHB rows aggregated: {len(lhb)}  unique days: {lhb['d1_date'].nunique()}")

    print("\nloading 3% candidates ...")
    cands = json.load(gzip.open(CAND_FP, "rt"))
    print(f"candidates: {len(cands)}")

    # Build index for fast join
    lhb_idx = {(r["symbol"], r["d1_date"]): r.to_dict() for _, r in lhb.iterrows()}

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
        # Attach LHB fields (default 0/False when absent)
        lhb_row = lhb_idx.get((c["symbol"], c["d1_date"]))
        feats["lhb_hit"] = lhb_row is not None
        if lhb_row:
            feats["lhb_net_buy_pct"] = float(lhb_row["lhb_net_buy_pct"] or 0.0)
            feats["lhb_amount_pct"] = float(lhb_row["lhb_amount_pct"] or 0.0)
            feats["lhb_turnover"] = float(lhb_row["lhb_turnover"] or 0.0)
            feats["lhb_mktcap_floating"] = float(lhb_row["lhb_mktcap_floating"] or 0.0)
            feats["lhb_inst_sell"] = bool(lhb_row["lhb_inst_sell_any"])
            feats["lhb_inst_buy"] = bool(lhb_row["lhb_inst_buy_any"])
            feats["lhb_inst_count"] = int(lhb_row["lhb_inst_count"] or 0)
            feats["lhb_reason_up"] = bool(lhb_row["lhb_reason_up"])
            feats["lhb_reason_turnover"] = bool(lhb_row["lhb_reason_turnover"])
            feats["lhb_reason_amp"] = bool(lhb_row["lhb_reason_amp"])
            feats["lhb_reason_down"] = bool(lhb_row["lhb_reason_down"])
        else:
            feats["lhb_net_buy_pct"] = 0.0
            feats["lhb_amount_pct"] = 0.0
            feats["lhb_turnover"] = 0.0
            feats["lhb_mktcap_floating"] = 0.0
            feats["lhb_inst_sell"] = False
            feats["lhb_inst_buy"] = False
            feats["lhb_inst_count"] = 0
            feats["lhb_reason_up"] = False
            feats["lhb_reason_turnover"] = False
            feats["lhb_reason_amp"] = False
            feats["lhb_reason_down"] = False
        rows.append(feats)
    hit_total = sum(1 for r in rows if r["lhb_hit"])
    print(f"replayed samples (not_sealed): {len(rows)}  LHB-hit: {hit_total}  "
          f"missing_minutes={missing_min}  sealed_skipped={sealed_skipped}  bad_corp={bad_corp}")

    rule_a = lambda r: (r.get("r_last30_pct") or 0) < -0.5 and within(r.get("close_location_day"), 0, 0.75)

    filters = [
        ("baseline (not_sealed)", lambda r: True),
        ("Rule A: r_last30<-0.5% & close_loc<0.75", rule_a),
        ("LHB hit D1", lambda r: r["lhb_hit"]),
        ("LHB not hit D1", lambda r: not r["lhb_hit"]),
        ("LHB hit & inst_buy", lambda r: r["lhb_hit"] and r["lhb_inst_buy"]),
        ("LHB hit & inst_sell", lambda r: r["lhb_hit"] and r["lhb_inst_sell"]),
        ("LHB hit & net_buy_pct>5", lambda r: r["lhb_hit"] and r["lhb_net_buy_pct"] > 5),
        ("LHB hit & net_buy_pct<0", lambda r: r["lhb_hit"] and r["lhb_net_buy_pct"] < 0),
        ("LHB hit & net_buy_pct in [-5,-1]", lambda r: r["lhb_hit"] and within(r["lhb_net_buy_pct"], -5, -1)),
        ("LHB hit & reason_up (涨幅)", lambda r: r["lhb_hit"] and r["lhb_reason_up"]),
        ("LHB hit & reason_turnover (换手)", lambda r: r["lhb_hit"] and r["lhb_reason_turnover"]),
        ("LHB hit & reason_amp (振幅)", lambda r: r["lhb_hit"] and r["lhb_reason_amp"]),
        ("Rule A + LHB not hit", lambda r: rule_a(r) and not r["lhb_hit"]),
        ("Rule A + LHB hit", lambda r: rule_a(r) and r["lhb_hit"]),
        ("Rule A + LHB inst_sell", lambda r: rule_a(r) and r["lhb_inst_sell"]),
        ("not_sealed - LHB hit (filter OUT)", lambda r: not r["lhb_hit"]),
        ("not_sealed - LHB inst_sell (filter OUT)", lambda r: not r["lhb_inst_sell"]),
    ]

    all_bucket = summarize(rows, filters, "ALL 3%+ (2025-01-01~2026-09-28)")
    in_sample = [r for r in rows if r["d1_date"] <= IN_SAMPLE_END]
    holdout = [r for r in rows if r["d1_date"] >= HOLDOUT_START]
    in_results = summarize(in_sample, filters, f"IN-SAMPLE (<= {IN_SAMPLE_END})")
    out_results = summarize(holdout, filters, f"HOLDOUT (>= {HOLDOUT_START})")

    OUT_FP.parent.mkdir(parents=True, exist_ok=True)
    OUT_FP.write_text(json.dumps({
        "fee_pct": FEE_PCT,
        "min_d1_change_pct": 3.0,
        "lhb_hit_rate_overall": round(hit_total / len(rows) * 100, 2) if rows else 0.0,
        "all": all_bucket,
        "in_sample": in_results,
        "holdout": out_results,
        "total_rows": len(rows),
    }, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_FP}")


if __name__ == "__main__":
    main()
