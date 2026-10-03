"""Layer 1: 市场情绪门禁 (Sentiment Gate) — compute per-day score from
zt-pool / zb-pool snapshots.

Scoring components (炒股养家派的量化版):
  1. ladder_height    — 当日最高连板数 (max 连板数 in zt pool).
                        Reflects "有没有高度", 直接代表龙头强度.
  2. zt_count         — 当日涨停总数. 市场热度基础量.
  3. lb2_plus         — 2 连板及以上的个数. 中段情绪.
  4. first_board_cnt  — 首板数量 (连板数==1). 情绪发酵的新血.
  5. zb_rate          — 炸板率 = zb_pool / (zb_pool + zt_pool). 情绪强度反指标.
  6. survive_rate     — 昨日涨停今日仍收红(涨跌幅>0)的比例. 情绪延续指标.

每个指标分别做"相对本样本窗口的分位数" (0..1), 得到 0..100 情绪分.
分数越高越过热; 分数越低越冰点. 四分位切分 冰点/启动/发酵/高潮.

当前数据窗口 2026-09-09 ~ 2026-09-24 (真实交易日 12 天), 不足以做严格
quantile 校准, 但可以生成本窗口相对分数 + 规则化门禁用于后续增量累积.

每日 cron 后调用:
  python scripts/analyze_sentiment_layer.py
写入: research/sentiment-features/sentiment_daily.csv.gz
"""
from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ZT_DIR = ROOT / "research" / "sentiment-features" / "zt_pool"
ZB_DIR = ROOT / "research" / "sentiment-features" / "zb_pool"
OUT_FP = ROOT / "research" / "sentiment-features" / "sentiment_daily.csv.gz"
DAILY_CACHE = ROOT / "research" / "monthly-target-v1-3pct" / "daily" / "000001.json.gz"

HOLIDAY_RANGE = ("2026-10-01", "2026-10-07")


def real_trading_days() -> set[str]:
    with gzip.open(DAILY_CACHE, "rt") as f:
        d = json.load(f)
    return {b["datetime"][:10] for b in d.get("bars", [])}


def load_pool(dir_path: Path, trading_days: set[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for fp in sorted(dir_path.glob("*.csv.gz")):
        tag = fp.stem.replace(".csv", "")
        iso = f"{tag[:4]}-{tag[4:6]}-{tag[6:8]}"
        if iso not in trading_days:
            continue
        if HOLIDAY_RANGE[0] <= iso <= HOLIDAY_RANGE[1]:
            continue
        try:
            df = pd.read_csv(gzip.open(fp, "rt"))
        except pd.errors.EmptyDataError:
            continue
        if len(df) == 0:
            continue
        df["date"] = iso
        out[iso] = df
    return out


def _norm_sym(code) -> str:
    try:
        return f"{int(code):06d}"
    except Exception:
        return str(code).zfill(6)


def compute_daily_features(zt: dict[str, pd.DataFrame], zb: dict[str, pd.DataFrame]) -> pd.DataFrame:
    days = sorted(set(zt.keys()) | set(zb.keys()))
    prev_zt_syms: dict[str, set[str]] = {}
    rows = []
    for d in days:
        zt_df = zt.get(d)
        zb_df = zb.get(d)
        n_zt = len(zt_df) if zt_df is not None else 0
        n_zb = len(zb_df) if zb_df is not None else 0

        if zt_df is not None and "连板数" in zt_df.columns:
            ladder_height = int(zt_df["连板数"].max())
            lb2_plus = int((zt_df["连板数"] >= 2).sum())
            lb3_plus = int((zt_df["连板数"] >= 3).sum())
            first_board_cnt = int((zt_df["连板数"] == 1).sum())
        else:
            ladder_height = lb2_plus = lb3_plus = first_board_cnt = 0

        # Yesterday-ZT survive rate: 昨日涨停今日是否红盘
        prev_syms = prev_zt_syms.get(d) or set()
        survive = None
        if prev_syms and zt_df is not None:
            # survived if symbol appears in today's zt (still limit-up) OR
            # we lack today's close, so we can only confirm "still limit-up"
            today_zt_syms = {_norm_sym(c) for c in zt_df["代码"].tolist()}
            still_zt = len(prev_syms & today_zt_syms)
            # survive rate ≈ 今日仍涨停 / 昨日涨停数
            survive = still_zt / len(prev_syms) if prev_syms else None

        zb_rate = (n_zb / (n_zt + n_zb)) if (n_zt + n_zb) else None

        rows.append({
            "date": d,
            "n_zt": n_zt,
            "n_zb": n_zb,
            "ladder_height": ladder_height,
            "lb2_plus": lb2_plus,
            "lb3_plus": lb3_plus,
            "first_board_cnt": first_board_cnt,
            "zb_rate": round(zb_rate, 4) if zb_rate is not None else None,
            "yesterday_zt_still_zt_rate": round(survive, 4) if survive is not None else None,
        })

        if zt_df is not None:
            prev_zt_syms[_shift_next_day(d, zt)] = {_norm_sym(c) for c in zt_df["代码"].tolist()}

    return pd.DataFrame(rows)


def _shift_next_day(d: str, zt_map: dict) -> str:
    """Pick the next day in sorted zt_map after d, so we can anchor yesterday→today."""
    days = sorted(zt_map.keys())
    try:
        idx = days.index(d)
    except ValueError:
        return "__none__"
    return days[idx + 1] if idx + 1 < len(days) else "__none__"


def quantile_score(series: pd.Series, higher_is_hotter: bool = True) -> pd.Series:
    """Rank-based 0..100 score inside the window."""
    s = series.copy()
    valid = s.dropna()
    if len(valid) == 0:
        return pd.Series([None] * len(s), index=s.index)
    ranks = valid.rank(pct=True)
    if not higher_is_hotter:
        ranks = 1.0 - ranks
    out = pd.Series([None] * len(s), index=s.index, dtype=object)
    out.loc[valid.index] = (ranks * 100.0).round(2)
    return out


def classify_state(score: float) -> str:
    if score is None or pd.isna(score):
        return "unknown"
    if score < 25:
        return "ice"        # 冰点
    if score < 50:
        return "start"      # 启动
    if score < 75:
        return "ferment"    # 发酵
    return "climax"         # 高潮


POSITION_MULT = {
    "ice": 1.5,
    "start": 1.0,
    "ferment": 0.75,
    "climax": 0.0,
    "unknown": 1.0,
}


def main() -> None:
    trading_days = real_trading_days()
    print(f"real trading days in daily cache: {len(trading_days)}  "
          f"(latest: {max(trading_days)})")

    zt = load_pool(ZT_DIR, trading_days)
    zb = load_pool(ZB_DIR, trading_days)
    print(f"zt days usable: {len(zt)}  range: {min(zt)}~{max(zt)}" if zt else "no zt")
    print(f"zb days usable: {len(zb)}  range: {min(zb)}~{max(zb)}" if zb else "no zb")

    df = compute_daily_features(zt, zb)
    if len(df) == 0:
        print("no sentiment rows computed")
        return

    # Score each component (window-relative).
    # 炸板率: 越高说明封板失败多, 情绪在恶化, 并不是"过热". 这里反向打分.
    df["s_ladder"] = quantile_score(df["ladder_height"].astype(float), higher_is_hotter=True)
    df["s_zt_cnt"] = quantile_score(df["n_zt"].astype(float), higher_is_hotter=True)
    df["s_lb2"] = quantile_score(df["lb2_plus"].astype(float), higher_is_hotter=True)
    df["s_first"] = quantile_score(df["first_board_cnt"].astype(float), higher_is_hotter=True)
    df["s_zb"] = quantile_score(df["zb_rate"].astype(float), higher_is_hotter=False)
    df["s_survive"] = quantile_score(df["yesterday_zt_still_zt_rate"].astype(float), higher_is_hotter=True)

    # Composite: 高度 25% + 涨停数 20% + 二板 20% + 首板 15% + 炸板率 10% + 延续率 10%
    def composite(row):
        parts = []
        weights = []
        for key, w in [("s_ladder", 0.25), ("s_zt_cnt", 0.2), ("s_lb2", 0.2),
                        ("s_first", 0.15), ("s_zb", 0.1), ("s_survive", 0.1)]:
            v = row.get(key)
            if v is not None and not pd.isna(v):
                parts.append(float(v) * w)
                weights.append(w)
        if not weights:
            return None
        return round(sum(parts) / sum(weights), 2)

    df["sentiment_score"] = df.apply(composite, axis=1)
    df["sentiment_state"] = df["sentiment_score"].apply(classify_state)
    df["position_multiplier"] = df["sentiment_state"].map(POSITION_MULT)
    df["can_open_new"] = df["sentiment_state"].isin(["ice", "start", "ferment"])

    OUT_FP.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT_FP, "wt", encoding="utf-8") as f:
        df.to_csv(f, index=False)

    latest = df.iloc[-1].to_dict()
    latest_json = {
        "latest_trading_day": latest["date"],
        "n_zt": int(latest["n_zt"]),
        "ladder_height": int(latest["ladder_height"]),
        "lb2_plus": int(latest["lb2_plus"]),
        "first_board_cnt": int(latest["first_board_cnt"]),
        "zb_rate": float(latest["zb_rate"]) if pd.notna(latest["zb_rate"]) else None,
        "yesterday_zt_still_zt_rate": (
            float(latest["yesterday_zt_still_zt_rate"])
            if pd.notna(latest["yesterday_zt_still_zt_rate"]) else None
        ),
        "sentiment_score": float(latest["sentiment_score"]),
        "sentiment_state": latest["sentiment_state"],
        "position_multiplier": float(latest["position_multiplier"]),
        "can_open_new": bool(latest["can_open_new"]),
        "window_days": int(len(df)),
        "window_start": df.iloc[0]["date"],
        "window_end": latest["date"],
    }
    OUT_JSON = OUT_FP.with_suffix("").with_suffix(".latest.json")
    OUT_JSON.write_text(json.dumps(latest_json, ensure_ascii=False, indent=2))

    print("\n=== Sentiment daily (window-relative) ===")
    cols = ["date", "n_zt", "ladder_height", "lb2_plus", "first_board_cnt",
            "zb_rate", "yesterday_zt_still_zt_rate",
            "sentiment_score", "sentiment_state", "position_multiplier", "can_open_new"]
    print(df[cols].to_string(index=False))
    print(f"\nwrote {OUT_FP}")
    print(f"wrote {OUT_JSON}")

    # Distribution summary
    state_counts = df["sentiment_state"].value_counts().to_dict()
    print(f"\nstate distribution: {state_counts}")
    print(f"\nlatest ({latest['date']}): state={latest['sentiment_state']}  "
          f"score={latest['sentiment_score']:.2f}  "
          f"position_mult={latest['position_multiplier']}  "
          f"can_open_new={latest['can_open_new']}")


if __name__ == "__main__":
    main()
