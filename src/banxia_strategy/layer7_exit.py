"""T+1-compliant Layer 7 exit simulation for ordinary A-shares."""
from __future__ import annotations

from typing import Optional, Sequence


D3_LAST_TRIGGER_MINUTE_INDEX = 233  # 14:54
D3_FORCE_EXIT_MINUTE_INDEX = 235  # 14:56 after a 14:55 instruction


def simulate_t1_layer7_exit(
    buy_price: float,
    d3_prices: Sequence[float],
    *,
    tp_pct: float,
    sl_pct: float,
) -> tuple[float, str, Optional[int]]:
    """Return the D3 exit proxy price, reason, and minute index.

    A position opened on D2 cannot be sold until D3. A D3 opening gap beyond
    either threshold exits at the observed first-minute price rather than at
    the unreachable threshold. Later crossings use the threshold price. If
    no threshold is reached through 14:54, the 14:56 minute price proxies a
    force-exit instruction sent at 14:55.
    """
    if buy_price <= 0:
        raise ValueError("buy_price must be positive")
    if tp_pct <= 0 or sl_pct <= 0:
        raise ValueError("tp_pct and sl_pct must be positive")
    if len(d3_prices) <= D3_FORCE_EXIT_MINUTE_INDEX:
        raise ValueError("D3 minute prices must include the 14:56 sample")
    if any(price <= 0 for price in d3_prices):
        raise ValueError("D3 minute prices must be positive")

    take_profit = buy_price * (1.0 + tp_pct)
    stop_loss = buy_price * (1.0 - sl_pct)
    first = float(d3_prices[0])
    if first <= stop_loss:
        return first, "layer7_d3_stop_loss", 0
    if first >= take_profit:
        return first, "layer7_d3_take_profit", 0

    previous = first
    for index in range(1, D3_LAST_TRIGGER_MINUTE_INDEX + 1):
        current = float(d3_prices[index])
        low = min(previous, current)
        high = max(previous, current)
        stop_hit = low <= stop_loss
        target_hit = high >= take_profit
        if stop_hit:
            return stop_loss, "layer7_d3_stop_loss", index
        if target_hit:
            return take_profit, "layer7_d3_take_profit", index
        previous = current

    return (
        float(d3_prices[D3_FORCE_EXIT_MINUTE_INDEX]),
        "layer7_d3_force_exit",
        D3_FORCE_EXIT_MINUTE_INDEX,
    )
