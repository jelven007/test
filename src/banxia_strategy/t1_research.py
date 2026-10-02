"""Chronological execution proxies for research, never live order instructions.

Minute samples have no bid/ask or intraminute path. Reported fills are conditional
price/volume proxies. Future observations are outcomes, not entry filters.
"""
from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from itertools import product
from statistics import mean, median


def cents(value, up=False):
    rounding = ROUND_CEILING if up else ROUND_HALF_UP
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=rounding))


def minute_label(index):
    minute = 9 * 60 + 31 + index if index < 120 else 13 * 60 + 1 + index - 120
    return f"{minute // 60:02d}:{minute % 60:02d}"


def cash_paid(price, shares, model):
    value = price * shares
    return value + max(model["minimum_commission_each_side_cny"],
                       value * model["commission_rate_each_side"]) + value * model["transfer_fee_each_side"]


def cash_received(price, shares, model):
    value = price * shares
    return value - max(model["minimum_commission_each_side_cny"],
                       value * model["commission_rate_each_side"]) - value * (
                           model["transfer_fee_each_side"] + model["sell_stamp_tax"])


def net_return(buy_price, sale_price, shares, model):
    return (cash_received(sale_price, shares, model) / cash_paid(buy_price, shares, model) - 1) * 100


def minute_quality(minute, bar):
    if minute is None:
        return "missing"
    prices, volumes = minute.get("prices", []), minute.get("volumes", [])
    if len(prices) != 240 or len(volumes) != 240:
        return "incomplete"
    if not all(math.isfinite(p) and p > 0 for p in prices):
        return "invalid_price"
    if not all(math.isfinite(v) and v >= 0 for v in volumes):
        return "invalid_volume"
    if abs(prices[-1] - bar["close"]) > 0.011:
        return "close_mismatch"
    if min(prices) < bar["low"] - 0.011 or max(prices) > bar["high"] + 0.011:
        return "daily_range_mismatch"
    daily_volume = float(bar.get("vol", bar.get("volume", 0)) or 0)
    if daily_volume <= 0 or abs(sum(volumes) / daily_volume - 1) > 0.02:
        return "volume_mismatch"
    return "ok"


def buy_proxy(event, bar, minute, mode, model):
    previous = float(event["close"])
    opening = float(bar["open"])
    ratio = 0.1 if event["board"] == "main" else 0.2
    upper = cents(previous * (1 + ratio))
    limit = min(cents(previous * model["buy_limit_vs_d1_close"]), upper - 0.01)
    # Size is known when submitting the order, not chosen using its future fill.
    shares = math.floor(model["position_cny"] / limit / model["lot_shares"]) * model["lot_shares"]
    while shares > 0 and cash_paid(limit, shares, model) > model["position_cny"]:
        shares -= model["lot_shares"]
    if shares < model["minimum_shares_by_board"][event["board"]]:
        return {"buy_status": "below_one_lot"}
    if opening < previous - 0.001:
        return {"buy_status": "opening_below_reference"}
    prices, volumes = minute["prices"], minute["volumes"]
    pending, dipped, signal_index = False, False, None
    for index in range(29):  # Last sample eligible is 09:59; exclude 10:00.
        price = prices[index]
        if price < previous - 0.001:
            return {"buy_status": "observed_breach_before_fill"}
        if pending:
            fill = cents(price * (1 + model["slippage_each_side"]), up=True)
            capacity = volumes[index] * 100 * model["maximum_participation_in_observed_minute_volume"]
            if price < upper - 0.001 and fill <= limit + 0.001 and shares <= capacity:
                return {
                    "buy_status": "filled_proxy", "signal_time": minute_label(signal_index),
                    "buy_time": minute_label(index), "buy_index": index,
                    "buy_sample_price": price, "buy_price": fill, "shares": shares,
                    "buy_minute_volume_hands": volumes[index],
                    "buy_participation_pct": shares / (volumes[index] * 100) * 100,
                    "order_limit": limit,
                }
        signal = (
            (mode == "early" and index == 0)
            or (mode == "breakout" and index >= 1 and price >= max(opening, prices[0]) + 0.009)
            or (mode == "reclaim" and dipped and price >= opening + 0.009)
        )
        if signal and not pending:
            pending, signal_index = True, index
        if price < opening - 0.001:
            dipped = True
    return {"buy_status": "no_pre10_fill_proxy"}


def target_quote(buy_price, shares, model):
    goal = model["target_net_pct"]
    price = cents(buy_price * (1 + goal / 100), up=True)
    while net_return(buy_price, price, shares, model) < goal:
        price = cents(price + 0.01)
    return price


def sell_proxy(buy, buy_bar, sell_bar, minute, board, mode, model):
    lower = cents(float(buy_bar["close"]) * (0.9 if board == "main" else 0.8))
    prices, volumes = minute["prices"], minute["volumes"]
    participation = model["maximum_participation_in_observed_minute_volume"]
    shares, buy_price = buy["shares"], buy["buy_price"]
    quote = target_quote(buy_price, shares, model)

    def usable(index):
        return prices[index] > lower + 0.001 and shares <= volumes[index] * 100 * participation

    def filled(index, price, reason):
        return {
            "sell_status": "filled_proxy", "sell_time": minute_label(index),
            "sell_price": price, "sell_sample_price": prices[index],
            "sell_minute_volume_hands": volumes[index], "sell_reason": reason,
            "net_return_pct": net_return(buy_price, price, shares, model),
            "target_quote": quote,
        }

    if mode == "target":
        required_sample = quote / (1 - model["slippage_each_side"]) + 0.01
        for index in range(1, 234):  # Exclude auction-mixed first volume; through 14:54.
            if usable(index) and prices[index] >= required_sample - 1e-9:
                return filled(index, quote, "precommitted_target")
        eligible = range(235, 237)  # Send 14:55; observed fill 14:56 or 14:57.
    else:
        eligible = range(1, 237)  # Send after 09:31; later retries if unfilled.
    for index in eligible:
        if usable(index):
            price = max(lower, math.floor(prices[index] * (1 - model["slippage_each_side"]) * 100 + 1e-9) / 100)
            return filled(index, price, "early_exit" if mode == "early" else "end_of_day_exit")
    return {"sell_status": "unfilled_d3", "sell_reason": "limit_down_or_capacity", "target_quote": quote}


def wilson(successes, count):
    if not count:
        return [0.0, 1.0]
    z = 1.959963984540054
    probability = successes / count
    denominator = 1 + z * z / count
    center = (probability + z * z / (2 * count)) / denominator
    half = z * math.sqrt(probability * (1 - probability) / count + z * z / (4 * count * count)) / denominator
    return [center - half, center + half]


def summary(rows, exit_mode):
    filled = [r for r in rows if r["buy_status"] == "filled_proxy"]
    returns = [r[f"{exit_mode}_net_return_pct"] for r in filled
               if r.get(f"{exit_mode}_net_return_pct") is not None]
    successes = sum(r.get(f"{exit_mode}_net_return_pct", -math.inf) > 5
                    for r in filled if r.get(f"{exit_mode}_net_return_pct") is not None)
    interval = wilson(successes, len(filled))
    return {
        "signals_in_filtered_universe": len(rows), "buys": len(filled),
        "known_exits": len(returns), "unknown_or_unfilled_exits": len(filled) - len(returns),
        "successes": successes,
        "success_rate": successes / len(filled) if filled else None,
        "wilson_lower": interval[0], "wilson_upper": interval[1],
        "mean_known_return_pct": mean(returns) if returns else None,
        "median_known_return_pct": median(returns) if returns else None,
        "worst_known_return_pct": min(returns) if returns else None,
        "positive_rate_known": sum(v > 0 for v in returns) / len(returns) if returns else None,
        "entry_dates": len({r["buy_date"] for r in filled}),
    }


def split_name(row, protocol):
    for stage in ("train", "validation", "holdout"):
        start, end = protocol["selection"][stage]
        if start <= row["buy_date"] <= end:
            return stage if row["sell_date"] <= end else "purged"
    return "outside"


def rules(protocol):
    grid = protocol["grid"]
    for i, values in enumerate(product(*grid.values())):
        yield {"id": f"R{i + 1:04d}", **dict(zip(grid, values))}


def matches(row, rule):
    if row["entry_mode"] != rule["entry"] or row["board"] != "main":
        return False
    if row["consecutive_limit_days"] != 1 or (row["amount_cny"] or 0) < 200000000:
        return False
    for field, bounds in (
        ("d2_open_gap_pct", rule["d2_open_gap_pct"]),
        ("turnover_pct", rule["d1_turnover_pct"]),
        ("float_market_cap_cny", rule["d1_float_cap_cny"]),
    ):
        value = row.get(field)
        if value is None or not bounds[0] <= value <= bounds[1]:
            return False
    if row["amplitude_pct"] < rule["d1_min_amplitude_pct"]:
        return False
    maximum = rule["d1_max_return_5d_pct"]
    return maximum is None or (
        row.get("return_5d_pct") is not None and row["return_5d_pct"] <= maximum)


def cluster_interval(rows, exit_mode, samples=2000, seed=20261002):
    import random
    groups = defaultdict(lambda: [0, 0])
    for row in rows:
        if row["buy_status"] != "filled_proxy":
            continue
        groups[row["buy_date"]][1] += 1
        value = row.get(f"{exit_mode}_net_return_pct")
        groups[row["buy_date"]][0] += int(value is not None and value > 5)
    if not groups:
        return [None, None]
    counts, rng = list(groups.values()), random.Random(seed)
    ratios = []
    for _ in range(samples):
        drawn = rng.choices(counts, k=len(counts))
        ratios.append(sum(x[0] for x in drawn) / sum(x[1] for x in drawn))
    ratios.sort()
    return [ratios[int(samples * 0.025)], ratios[min(samples - 1, int(samples * 0.975))]]
