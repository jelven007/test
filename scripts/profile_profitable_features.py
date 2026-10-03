"""Feature-mining on the profitable trade set from count_profitable_trades.py.

For each (symbol, D1_date) in the profitable (open-proxy) set, compute common
pre-trade signals over the 5-trading-day window ending at D1 (D1-4 .. D1):
- MACD (12,26,9): dif, dea, hist at D1; golden/dead cross within window
- RSI(6/12/24) at D1 and 5-day high
- Price position vs MA5/MA10/MA20/MA60; MA5 vs MA10 slope
- 5-day gain / 10-day gain / 20-day gain
- Volume ratio vs 5-day avg (vr5) at D1
- Chan (缠论) proxy: fractal structure of the last 5 bars (top/bottom), count
  of top/bottom fractals, and simple pen (笔) direction
- D1 raw features: close location (close vs day high-low), gap pct, open gap

Each metric is also computed on a matched negative control set (same gate: D1
change > 5%, same two execution windows exist, same date range) that did NOT
yield a profitable trade. Reports go to research/profitable-trade-counts/.
"""
from __future__ import annotations

import csv
import gzip
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLAY = ROOT / "research" / "monthly-target-v1" / "replay.json.gz"
DAILY_DIR = ROOT / "research" / "monthly-target-v1" / "daily"
TRADES_CSV = ROOT / "research" / "profitable-trade-counts" / "trades_open.csv"
OUT_DIR = ROOT / "research" / "profitable-trade-counts"

WINDOW = 5           # last N bars including D1
MA_WINDOWS = (5, 10, 20, 60)
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
RSI_PERIODS = (6, 12, 24)


def ema(series: list[float], span: int) -> list[float]:
    k = 2.0 / (span + 1)
    out: list[float] = []
    prev = None
    for x in series:
        prev = x if prev is None else prev + k * (x - prev)
        out.append(prev)
    return out


def macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    ef = ema(closes, MACD_FAST)
    es = ema(closes, MACD_SLOW)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, MACD_SIG)
    hist = [(a - b) * 2.0 for a, b in zip(dif, dea)]
    return dif, dea, hist


def rsi(closes: list[float], period: int) -> list[float]:
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    out = [50.0] * len(closes)
    if len(closes) <= period:
        return out
    avg_g = sum(gains[1 : period + 1]) / period
    avg_l = sum(losses[1 : period + 1]) / period
    out[period] = 100.0 - 100.0 / (1.0 + (avg_g / avg_l if avg_l else 1e9))
    for i in range(period + 1, len(closes)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i] = 100.0 - 100.0 / (1.0 + (avg_g / avg_l if avg_l else 1e9))
    return out


def sma(series: list[float], window: int) -> list[float]:
    out = [None] * len(series)
    acc = 0.0
    for i, x in enumerate(series):
        acc += x
        if i >= window:
            acc -= series[i - window]
        if i >= window - 1:
            out[i] = acc / window
    return out  # type: ignore[return-value]


def chan_fractals(highs: list[float], lows: list[float]) -> dict:
    """Classical Chan 分型: 3-bar top (middle high > both sides and middle low >= both sides)."""
    tops: list[int] = []
    bottoms: list[int] = []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1] and lows[i] >= lows[i - 1] and lows[i] >= lows[i + 1]:
            tops.append(i)
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1] and highs[i] <= highs[i - 1] and highs[i] <= highs[i + 1]:
            bottoms.append(i)
    pen = "none"
    if tops and bottoms:
        pen = "up" if bottoms[-1] > tops[-1] else "down"
    elif tops:
        pen = "down"
    elif bottoms:
        pen = "up"
    return {"top_count": len(tops), "bottom_count": len(bottoms), "pen": pen}


def load_bars(symbol: str) -> list[dict] | None:
    fp = DAILY_DIR / f"{symbol}.json.gz"
    if not fp.exists():
        return None
    with gzip.open(fp, "rt") as f:
        d = json.load(f)
    return d.get("bars") or None


def bar_date(bar: dict) -> str:
    return f"{bar['year']:04d}-{bar['month']:02d}-{bar['day']:02d}"


def extract_features(bars: list[dict], d1_date: str) -> dict | None:
    idx = next((i for i, b in enumerate(bars) if bar_date(b) == d1_date), None)
    if idx is None or idx < max(MA_WINDOWS):
        return None
    closes = [b["close"] for b in bars[: idx + 1]]
    highs = [b["high"] for b in bars[: idx + 1]]
    lows = [b["low"] for b in bars[: idx + 1]]
    opens = [b["open"] for b in bars[: idx + 1]]
    vols = [b["vol"] for b in bars[: idx + 1]]

    dif, dea, hist = macd(closes)
    f: dict = {
        "d1_close": closes[-1],
        "d1_open": opens[-1],
        "d1_high": highs[-1],
        "d1_low": lows[-1],
        "d1_close_location": (closes[-1] - lows[-1]) / (highs[-1] - lows[-1]) if highs[-1] > lows[-1] else 0.5,
        "d1_gap_pct": (opens[-1] - closes[-2]) / closes[-2] * 100.0,
        "macd_dif": dif[-1],
        "macd_dea": dea[-1],
        "macd_hist": hist[-1],
        "macd_dif_gt_dea": dif[-1] > dea[-1],
        "macd_hist_rising": hist[-1] > hist[-2],
    }

    # golden/dead cross within the 5-day window (dif crossing dea)
    gc = dc = 0
    for i in range(idx - WINDOW + 2, idx + 1):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            gc += 1
        if dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            dc += 1
    f["macd_golden_cross_in_5d"] = gc
    f["macd_dead_cross_in_5d"] = dc

    for p in RSI_PERIODS:
        r = rsi(closes, p)
        f[f"rsi{p}"] = r[-1]
        f[f"rsi{p}_5d_max"] = max(r[-WINDOW:])
        f[f"rsi{p}_5d_min"] = min(r[-WINDOW:])

    for w in MA_WINDOWS:
        ma_series = sma(closes, w)
        if ma_series[-1] is None:
            continue
        f[f"close_vs_ma{w}_pct"] = (closes[-1] / ma_series[-1] - 1.0) * 100.0

    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    if ma5[-1] and ma5[-WINDOW]:
        f["ma5_slope_5d_pct"] = (ma5[-1] / ma5[-WINDOW] - 1.0) * 100.0
    if ma5[-1] and ma10[-1]:
        f["ma5_gt_ma10"] = ma5[-1] > ma10[-1]

    f["ret_5d_pct"] = (closes[-1] / closes[-WINDOW] - 1.0) * 100.0
    f["ret_10d_pct"] = (closes[-1] / closes[-10] - 1.0) * 100.0 if len(closes) >= 10 else None
    f["ret_20d_pct"] = (closes[-1] / closes[-20] - 1.0) * 100.0 if len(closes) >= 20 else None

    avg_vol5 = st.mean(vols[-6:-1]) if len(vols) >= 6 else None
    f["vr5"] = (vols[-1] / avg_vol5) if avg_vol5 and avg_vol5 > 0 else None

    # Chan fractals on the 5-day window of the last 7 bars (need neighbors)
    f.update({f"chan_{k}": v for k, v in chan_fractals(highs[-7:], lows[-7:]).items()})

    # D1 up-days ratio in the 5-day window (strength)
    ups = sum(1 for i in range(idx - WINDOW + 1, idx + 1) if closes[i] > closes[i - 1])
    f["up_days_in_5d"] = ups

    return f


def summarize(vals: list) -> dict:
    nums = [v for v in vals if isinstance(v, (int, float))]
    bools = [v for v in vals if isinstance(v, bool)]
    strs = [v for v in vals if isinstance(v, str)]
    out: dict = {"n": len(vals)}
    if nums:
        nums_sorted = sorted(nums)
        out["mean"] = round(sum(nums) / len(nums), 4)
        out["median"] = round(nums_sorted[len(nums) // 2], 4)
        out["p25"] = round(nums_sorted[max(0, len(nums) // 4)], 4)
        out["p75"] = round(nums_sorted[min(len(nums) - 1, (3 * len(nums)) // 4)], 4)
    if bools:
        out["true_pct"] = round(100.0 * sum(1 for v in bools if v) / len(bools), 2)
    if strs:
        counter: dict[str, int] = defaultdict(int)
        for v in strs:
            counter[v] += 1
        out["mode"] = max(counter.items(), key=lambda kv: kv[1])
    return out


def main() -> None:
    with TRADES_CSV.open() as f:
        reader = csv.DictReader(f)
        profitable_keys = {(row["symbol"], row["d1_date"]) for row in reader}
    print(f"profitable keys: {len(profitable_keys)}")

    with gzip.open(REPLAY, "rt") as f:
        rows = json.load(f)

    bars_cache: dict[str, list[dict] | None] = {}
    pos_feats: list[dict] = []
    neg_feats: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for r in rows:
        if r["d1_date"] < "2025-01-01":
            continue
        if (r.get("d1_change_pct") or 0.0) <= 5.0:
            continue
        key = (r["symbol"], r["d1_date"])
        if key in seen:
            continue
        seen.add(key)
        sym = r["symbol"]
        if sym not in bars_cache:
            bars_cache[sym] = load_bars(sym)
        bars = bars_cache[sym]
        if not bars:
            continue
        feats = extract_features(bars, r["d1_date"])
        if feats is None:
            continue
        feats["_key"] = key
        (pos_feats if key in profitable_keys else neg_feats).append(feats)

    print(f"positive samples: {len(pos_feats)}  negative samples: {len(neg_feats)}")

    report: dict[str, dict] = {}
    keys = sorted({k for f in pos_feats + neg_feats for k in f if not k.startswith("_")})
    for k in keys:
        report[k] = {
            "profitable": summarize([f[k] for f in pos_feats if k in f and f[k] is not None]),
            "unprofitable": summarize([f[k] for f in neg_feats if k in f and f[k] is not None]),
        }

    # also per-symbol feature averages for the top profitable symbols
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for f in pos_feats:
        by_symbol[f["_key"][0]].append(f)
    top = sorted(by_symbol.items(), key=lambda kv: -len(kv[1]))[:30]
    per_symbol: list[dict] = []
    numeric_keys = [k for k in keys if any(
        isinstance(f.get(k), (int, float)) and not isinstance(f.get(k), bool) for f in pos_feats
    )]
    for sym, feats in top:
        row = {"symbol": sym, "profitable_count": len(feats)}
        for k in numeric_keys:
            vals = [f[k] for f in feats if isinstance(f.get(k), (int, float)) and not isinstance(f.get(k), bool)]
            row[k] = round(sum(vals) / len(vals), 4) if vals else None
        per_symbol.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "feature_comparison.json").open("w") as f:
        json.dump({
            "positive_n": len(pos_feats),
            "negative_n": len(neg_feats),
            "window_bars": WINDOW,
            "features": report,
        }, f, indent=2, ensure_ascii=False)

    with (OUT_DIR / "feature_comparison.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "feature",
            "pos_n", "pos_mean", "pos_median", "pos_p25", "pos_p75", "pos_true_pct",
            "neg_n", "neg_mean", "neg_median", "neg_p25", "neg_p75", "neg_true_pct",
            "mean_delta",
        ])
        for k in keys:
            p = report[k]["profitable"]
            n = report[k]["unprofitable"]
            delta = (p.get("mean", 0) - n.get("mean", 0)) if (p.get("mean") is not None and n.get("mean") is not None) else None
            w.writerow([
                k,
                p.get("n"), p.get("mean"), p.get("median"), p.get("p25"), p.get("p75"), p.get("true_pct"),
                n.get("n"), n.get("mean"), n.get("median"), n.get("p25"), n.get("p75"), n.get("true_pct"),
                round(delta, 4) if isinstance(delta, (int, float)) else None,
            ])

    with (OUT_DIR / "feature_by_symbol.csv").open("w", newline="") as f:
        w = csv.writer(f)
        header = ["symbol", "profitable_count"] + numeric_keys
        w.writerow(header)
        for row in per_symbol:
            w.writerow([row.get(c) for c in header])

    print("wrote:",
          OUT_DIR / "feature_comparison.json",
          OUT_DIR / "feature_comparison.csv",
          OUT_DIR / "feature_by_symbol.csv",
          sep="\n  ")


if __name__ == "__main__":
    main()
