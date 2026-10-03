"""Analyze D1 last-30-minute volume/price structure for the profitable trade set.

Reads the profitable-trade list from research/profitable-trade-counts/trades_open.csv
and the matching unprofitable set from replay.json.gz. For each (symbol, D1_date),
pull the D1 minute bars via mootdx if not cached and compute:

- Late-session returns:   r_last30, r_last15, r_last5 (%)
- Late-session momentum:  slope of close over last 30 min, VWAP vs close
- Late-session volume share: vol(last30)/vol(day), vol(last15)/vol(day), vol(last5)/vol(day)
- Volume ratios:          vr_last30_vs_prev210 (avg per-min ratio), vr_last5_vs_prev235
- Range compression:      high-low of last 30 vs full day
- Close location on D1:   (close - low) / (high - low)
- Pre-close bid pressure proxy: positive-minute count & up-tick ratio in last 30
- Price agitation:        std(minute return) in last 30 vs full day
- Final 3-min burst:      max(last3 minute return) and whether final minute volume > 2x avg

The analysis compares profitable vs unprofitable samples under identical D1 gates
(d1_change_pct > 5, 2025-01-01 onward, 1-min open-proxy fill feasible on D2 and D3).
Reports go to research/profitable-trade-counts/d1_minute_*.{csv,json}.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import statistics as st
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from banxia_strategy.monthly_target_research import _request_with_retry, load, save
from banxia_strategy.mootdx_provider import MootdxProvider

ROOT = Path(__file__).resolve().parent.parent
REPLAY = ROOT / "research" / "monthly-target-v1" / "replay.json.gz"
MINUTE_DIR = ROOT / "research" / "monthly-target-v1" / "minutes"
TRADES_CSV = ROOT / "research" / "profitable-trade-counts" / "trades_open.csv"
OUT_DIR = ROOT / "research" / "profitable-trade-counts"


def load_profitable_keys() -> set[tuple[str, str]]:
    with TRADES_CSV.open() as f:
        return {(r["symbol"], r["d1_date"]) for r in csv.DictReader(f)}


def enumerate_candidates() -> list[dict]:
    with gzip.open(REPLAY, "rt") as f:
        rows = json.load(f)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for r in rows:
        if r["d1_date"] < "2025-01-01":
            continue
        if (r.get("d1_change_pct") or 0.0) <= 5.0:
            continue
        key = (r["symbol"], r["d1_date"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def fetch_missing_d1_minutes(targets: list[tuple[str, str]]) -> dict[str, int]:
    """Pull D1 minute series via mootdx for keys not already cached."""
    missing = [(sym, day) for sym, day in targets if not (MINUTE_DIR / f"{day}_{sym}.json.gz").exists()]
    if not missing:
        return {"cached": len(targets), "downloaded": 0, "errors": 0}
    print(f"need to download D1 minutes: {len(missing)} / {len(targets)}")

    provider = MootdxProvider()
    workers = min(len(provider.servers), max(1, len(missing)))

    def worker(number: int, partition: list[tuple[str, str]]):
        counts = {"downloaded": 0, "errors": 0}
        state: dict = {"client": None, "server": None}
        try:
            for i, (symbol, day) in enumerate(partition, 1):
                path = MINUTE_DIR / f"{day}_{symbol}.json.gz"
                if path.exists():
                    continue
                try:
                    def fetch(client, symbol=symbol, day=day):
                        frame = client.minutes(symbol=symbol, date=day.replace("-", ""))
                        if frame is None or len(frame) != 240:
                            raise ValueError(
                                f"expected 240 minute samples, got {None if frame is None else len(frame)}"
                            )
                        prices = [float(v) for v in frame["price"]]
                        volumes = [float(v) for v in frame["vol"]]
                        if not all(math.isfinite(v) for v in prices + volumes):
                            raise ValueError("non-finite minute sample")
                        return {
                            "date": day,
                            "symbol": symbol,
                            "prices": prices,
                            "volumes": volumes,
                            "source": "mootdx",
                        }

                    save(path, _request_with_retry(provider, number, state, fetch))
                    counts["downloaded"] += 1
                except Exception as exc:
                    counts["errors"] += 1
                    print(f"  err {symbol} {day}: {exc}")
                if i % 50 == 0:
                    print(f"  worker {number}: {i}/{len(partition)} dl={counts['downloaded']} err={counts['errors']}")
        finally:
            if state.get("client") is not None:
                provider._close(state["client"])
        return counts

    total = {"downloaded": 0, "errors": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, idx, missing[idx::workers]) for idx in range(workers)]
        for fut in as_completed(futures):
            for k, v in fut.result().items():
                total[k] = total.get(k, 0) + v
    total["cached"] = len(targets) - total["downloaded"] - total["errors"]
    return total


def load_minutes(symbol: str, day: str) -> dict | None:
    fp = MINUTE_DIR / f"{day}_{symbol}.json.gz"
    if not fp.exists():
        return None
    with gzip.open(fp, "rt") as f:
        return json.load(f)


def _ret_pct(a: float, b: float) -> float | None:
    if b is None or b <= 0 or a is None:
        return None
    return (a / b - 1.0) * 100.0


def _vwap(prices: list[float], volumes: list[float]) -> float | None:
    num = den = 0.0
    for p, v in zip(prices, volumes):
        if p is None or v is None or v <= 0 or p <= 0:
            continue
        num += p * v
        den += v
    return (num / den) if den > 0 else None


def _std(values: list[float]) -> float | None:
    pts = [v for v in values if v is not None and math.isfinite(v)]
    if len(pts) < 2:
        return None
    return st.pstdev(pts)


def extract_d1_features(minutes: dict) -> dict | None:
    prices = minutes.get("prices") or []
    volumes = minutes.get("volumes") or []
    if len(prices) != 240 or len(volumes) != 240:
        return None
    day_open = prices[0]
    day_high = max(prices)
    day_low = min(prices)
    day_close = prices[-1]
    day_vol = sum(volumes)
    if day_vol <= 0 or day_close <= 0 or day_high <= day_low:
        return None

    last30_p = prices[-30:]
    last30_v = volumes[-30:]
    last15_p = prices[-15:]
    last15_v = volumes[-15:]
    last5_p = prices[-5:]
    last5_v = volumes[-5:]
    prev210_v = volumes[:-30]
    prev210_avg_min = (sum(prev210_v) / len(prev210_v)) if prev210_v else 0.0
    last30_avg_min = (sum(last30_v) / 30.0)
    last5_avg_min = (sum(last5_v) / 5.0)
    last3_avg_min = (sum(volumes[-3:]) / 3.0)

    minute_returns = [_ret_pct(prices[i], prices[i - 1]) for i in range(1, 240)]
    last30_returns = minute_returns[-30:]
    up_min = sum(1 for r in last30_returns if r is not None and r > 0)
    down_min = sum(1 for r in last30_returns if r is not None and r < 0)

    # linear slope of minute price over last 30 (centered x = -14.5..14.5)
    xs = list(range(30))
    xbar = sum(xs) / 30.0
    ybar = sum(last30_p) / 30.0
    num = sum((xs[i] - xbar) * (last30_p[i] - ybar) for i in range(30))
    den = sum((xs[i] - xbar) ** 2 for i in range(30))
    slope_pct_per_min = (num / den) / day_close * 100.0 if den > 0 else None

    vwap_last30 = _vwap(last30_p, last30_v)
    vwap_day = _vwap(prices, volumes)

    f = {
        "r_last30_pct": _ret_pct(day_close, prices[-31]),
        "r_last15_pct": _ret_pct(day_close, prices[-16]),
        "r_last5_pct": _ret_pct(day_close, prices[-6]),
        "r_final_1min_pct": _ret_pct(day_close, prices[-2]),
        "r_last3_max_pct": max((r for r in minute_returns[-3:] if r is not None), default=None),
        "slope_last30_bps_per_min": slope_pct_per_min * 100.0 if slope_pct_per_min is not None else None,
        "close_vs_last30_vwap_pct": _ret_pct(day_close, vwap_last30) if vwap_last30 else None,
        "close_vs_day_vwap_pct": _ret_pct(day_close, vwap_day) if vwap_day else None,
        "vol_share_last30": sum(last30_v) / day_vol,
        "vol_share_last15": sum(last15_v) / day_vol,
        "vol_share_last5": sum(last5_v) / day_vol,
        "vol_share_final_1min": volumes[-1] / day_vol if day_vol > 0 else None,
        "vr_last30_vs_prev210": (last30_avg_min / prev210_avg_min) if prev210_avg_min > 0 else None,
        "vr_last5_vs_prev235": (last5_avg_min / ((day_vol - sum(last5_v)) / 235.0)) if (day_vol - sum(last5_v)) > 0 else None,
        "vr_final_3min_vs_day_avg": (last3_avg_min / (day_vol / 240.0)) if day_vol > 0 else None,
        "range_last30_vs_day": (max(last30_p) - min(last30_p)) / (day_high - day_low),
        "close_location_day": (day_close - day_low) / (day_high - day_low),
        "close_location_last30": (
            (last30_p[-1] - min(last30_p)) / (max(last30_p) - min(last30_p))
            if max(last30_p) > min(last30_p) else 0.5
        ),
        "up_min_last30": up_min,
        "down_min_last30": down_min,
        "up_min_ratio_last30": up_min / 30.0,
        "vol_std_last30_vs_day": (_std(last30_v) or 0) / (_std(volumes) or 1) if _std(volumes) else None,
        "ret_std_last30_vs_day": (_std(last30_returns) or 0) / (_std(minute_returns) or 1) if _std(minute_returns) else None,
        "final_minute_vol_x_avg": volumes[-1] / (day_vol / 240.0) if day_vol > 0 else None,
        "closed_at_day_high": day_close >= day_high - 1e-9,
    }
    return f


def summarize(vals: list) -> dict:
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool) and v is not None and math.isfinite(v)]
    bools = [v for v in vals if isinstance(v, bool)]
    out: dict = {"n": len(vals)}
    if nums:
        s = sorted(nums)
        out["mean"] = round(sum(nums) / len(nums), 4)
        out["median"] = round(s[len(s) // 2], 4)
        out["p25"] = round(s[max(0, len(s) // 4)], 4)
        out["p75"] = round(s[min(len(s) - 1, (3 * len(s)) // 4)], 4)
    if bools:
        out["true_pct"] = round(100.0 * sum(1 for v in bools if v) / len(bools), 2)
    return out


def main() -> None:
    profitable = load_profitable_keys()
    candidates = enumerate_candidates()
    targets = [(r["symbol"], r["d1_date"]) for r in candidates]
    fetch_stats = fetch_missing_d1_minutes(targets)
    print("fetch summary:", fetch_stats)

    pos_feats: list[dict] = []
    neg_feats: list[dict] = []
    missing = 0
    for r in candidates:
        key = (r["symbol"], r["d1_date"])
        m = load_minutes(r["symbol"], r["d1_date"])
        if m is None:
            missing += 1
            continue
        feats = extract_d1_features(m)
        if feats is None:
            missing += 1
            continue
        feats["_symbol"] = r["symbol"]
        feats["_d1"] = r["d1_date"]
        (pos_feats if key in profitable else neg_feats).append(feats)
    print(f"positive samples: {len(pos_feats)}  negative samples: {len(neg_feats)}  missing minutes: {missing}")

    # Split by whether D1 was a sealed limit-up (last 30 min all zero return & zero vol share tiny).
    def is_sealed(f: dict) -> bool:
        return (
            abs(f.get("r_last30_pct") or 0.0) < 1e-9
            and abs(f.get("slope_last30_bps_per_min") or 0.0) < 1e-9
            and (f.get("close_location_day") or 0) >= 0.9999
        )

    buckets = {
        "all": (pos_feats, neg_feats),
        "sealed_limit_up": ([f for f in pos_feats if is_sealed(f)], [f for f in neg_feats if is_sealed(f)]),
        "not_sealed": ([f for f in pos_feats if not is_sealed(f)], [f for f in neg_feats if not is_sealed(f)]),
    }
    print("bucket sizes:")
    for name, (ps, ns) in buckets.items():
        print(f"  {name}: pos={len(ps)}  neg={len(ns)}  win_rate={100*len(ps)/(len(ps)+len(ns)) if (ps or ns) else 0:.2f}%")

    keys = sorted({k for f in pos_feats + neg_feats for k in f if not k.startswith("_")})
    report: dict[str, dict] = {}
    for name, (ps, ns) in buckets.items():
        for k in keys:
            report.setdefault(k, {})[name] = {
                "profitable": summarize([f[k] for f in ps if k in f and f[k] is not None]),
                "unprofitable": summarize([f[k] for f in ns if k in f and f[k] is not None]),
            }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "d1_minute_comparison.json").write_text(json.dumps(
        {
            "positive_n": len(pos_feats),
            "negative_n": len(neg_feats),
            "bucket_sizes": {k: {"pos": len(v[0]), "neg": len(v[1])} for k, v in buckets.items()},
            "features": report,
            "fetch": fetch_stats,
        },
        indent=2, ensure_ascii=False,
    ))
    with (OUT_DIR / "d1_minute_comparison.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["feature", "bucket",
                    "pos_n", "pos_mean", "pos_median", "pos_p25", "pos_p75", "pos_true_pct",
                    "neg_n", "neg_mean", "neg_median", "neg_p25", "neg_p75", "neg_true_pct",
                    "mean_delta"])
        for k in keys:
            for name in ("all", "sealed_limit_up", "not_sealed"):
                p = report[k][name]["profitable"]
                n = report[k][name]["unprofitable"]
                delta = None
                if isinstance(p.get("mean"), (int, float)) and isinstance(n.get("mean"), (int, float)):
                    delta = round(p["mean"] - n["mean"], 4)
                w.writerow([
                    k, name,
                    p.get("n"), p.get("mean"), p.get("median"), p.get("p25"), p.get("p75"), p.get("true_pct"),
                    n.get("n"), n.get("mean"), n.get("median"), n.get("p25"), n.get("p75"), n.get("true_pct"),
                    delta,
                ])
    print("wrote d1_minute_comparison.{csv,json}")

    # Decile analysis restricted to not_sealed samples (avoid the all-zero bucket).
    not_sealed_pos, not_sealed_neg = buckets["not_sealed"]
    def deciles(name: str) -> list[dict]:
        combined = [(f[name], True) for f in not_sealed_pos if f.get(name) is not None]
        combined += [(f[name], False) for f in not_sealed_neg if f.get(name) is not None]
        combined.sort(key=lambda x: x[0])
        if not combined:
            return []
        bucket_size = max(1, len(combined) // 10)
        rows = []
        for i in range(10):
            seg = combined[i * bucket_size: (i + 1) * bucket_size if i < 9 else len(combined)]
            if not seg:
                continue
            win = sum(1 for _, w in seg if w)
            rows.append({
                "decile": i + 1,
                "range": [round(seg[0][0], 4), round(seg[-1][0], 4)],
                "n": len(seg),
                "profitable": win,
                "win_rate_pct": round(100.0 * win / len(seg), 2),
            })
        return rows

    decile_report = {
        k: deciles(k) for k in [
            "vol_share_last30", "vol_share_last5", "r_last30_pct", "r_last5_pct",
            "vr_last30_vs_prev210", "slope_last30_bps_per_min", "final_minute_vol_x_avg",
            "close_location_day", "ret_std_last30_vs_day",
        ]
    }
    (OUT_DIR / "d1_minute_deciles.json").write_text(json.dumps(decile_report, indent=2, ensure_ascii=False))
    print("wrote d1_minute_deciles.json")


if __name__ == "__main__":
    main()
