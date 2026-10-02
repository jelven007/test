from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Optional, Sequence

from .mootdx_provider import _bar_date, _finite_number, _json_value


GEM_PRICE_LIMIT_REFORM = date(2020, 8, 24)


def limit_ratio(symbol: str, name: str, trade_date: date) -> Decimal:
    if symbol.startswith(("688", "689")):
        return Decimal("0.20")
    if symbol.startswith(("300", "301")) and trade_date >= GEM_PRICE_LIMIT_REFORM:
        return Decimal("0.20")
    return Decimal("0.05") if "ST" in name.upper() else Decimal("0.10")


def _rounded(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_price(previous_close: float, symbol: str, name: str, trade_date: date) -> float:
    value = Decimal(str(previous_close)) * (1 + limit_ratio(symbol, name, trade_date))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _capital_date(value: Any, fallback: Optional[date] = None) -> Optional[date]:
    try:
        return datetime.strptime(str(int(float(value))), "%Y%m%d").date()
    except (TypeError, ValueError, OverflowError):
        return fallback


def industry_from_f10(text: str) -> Optional[str]:
    """Read the industry field, never substitute a concept/theme for an industry."""
    match = re.search(r"行业类别\s*[｜|│]\s*([^｜|│\r\n]+)", text or "")
    return match.group(1).strip() if match else None


def historical_capital(
    finance: Mapping[str, Any], actions: Sequence[Mapping[str, Any]], session: date,
    collected_date: date,
) -> tuple[Optional[float], Optional[float], Optional[date], str]:
    events = []
    for action in actions:
        stamp = _bar_date(dict(action))
        before = tuple(_finite_number(action.get(k)) for k in ("qianzongguben", "panqianliutong"))
        after = tuple(_finite_number(action.get(k)) for k in ("houzongguben", "panhouliutong"))
        if all(v is not None and v > 0 for v in (*before, *after)):
            events.append((stamp, before, after))
    events.sort(key=lambda item: item[0])
    # The next change's before-capital is closest evidence of the historical state.
    next_event = next((item for item in events if item[0] > session), None)
    prior_event = next((item for item in reversed(events) if item[0] <= session), None)
    if next_event:
        stamp, values, _ = next_event
    elif prior_event:
        stamp, _, values = prior_event
    else:
        values = tuple(_finite_number(finance.get(k)) for k in ("zongguben", "liutongguben"))
        total, floating = (v if v is not None and v > 0 else None for v in values)
        return total, floating, _capital_date(finance.get("updated_date"), collected_date), "current_snapshot_estimate"
    return values[0] * 10000, values[1] * 10000, stamp, "xdxr_capital_history"


def reference_price(previous_close: float, actions: Sequence[Mapping[str, Any]]) -> Optional[float]:
    value = previous_close
    for action in actions:
        category = int(action.get("category") or 0)
        if category in (11, 12):  # shrink/split share events need a source reference price
            return None
        if category == 1:
            dividend, rights, bonus, rights_price = (
                _finite_number(action.get(k)) or 0
                for k in ("fenhong", "peigu", "songzhuangu", "peigujia")
            )
            value = (value * 10 - dividend + rights * rights_price) / (10 + bonus + rights)
    return _rounded(value) if value > 0 else None


def build_limit_up_history_rows(
    *,
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    securities: Mapping[str, Mapping[str, Any]],
    finances: Mapping[str, Mapping[str, Any]],
    start_date: date,
    end_date: date,
    collected_at: datetime,
    actions: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
    industries: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """Derive close-at-limit observations; retain the evidence/uncertainty per row."""
    results = []
    for symbol, raw_bars in histories.items():
        security = securities.get(symbol)
        if security is None:
            continue
        bars = sorted({ _bar_date(dict(b)): dict(b) for b in raw_bars }.values(), key=_bar_date)
        finance = finances.get(symbol, {})
        corporate_actions = (actions or {}).get(symbol, ())
        action_dates: dict[date, list] = {}
        for action in corporate_actions:
            action_dates.setdefault(_bar_date(dict(action)), []).append(action)
        ipo = _capital_date(finance.get("ipo_date"))
        ipo_sessions = [i for i, b in enumerate(bars) if ipo and _bar_date(b) >= ipo]
        # Only count IPO sessions if the actual listing day is present in our bars.
        unlimited = set(ipo_sessions[:5]) if (
            ipo and ipo >= date(2023, 4, 10) and
            any(_bar_date(b) == ipo for b in bars)
        ) else set()
        flags = [False] * len(bars)
        for index in range(1, len(bars)):
            current = bars[index]
            session = _bar_date(current)
            values = [_finite_number(current.get(k)) for k in ("close", "open", "high", "low")]
            previous = _finite_number(bars[index - 1].get("close"))
            if previous is None or previous <= 0 or any(v is None or v <= 0 for v in values):
                continue
            close, opening, high, low = values
            previous_session = _bar_date(bars[index - 1])
            relevant_actions = [
                a for stamp, items in action_dates.items()
                if previous_session < stamp <= session for a in items
            ]
            reference = reference_price(previous, relevant_actions)
            if reference is None or index in unlimited:
                continue
            # mootdx has no historical ST status. Match the 5/10% price bands,
            # and explicitly identify the 5% cases as unverified candidates.
            board = str(security["board"])
            ratios = (Decimal("0.20"),) if board in ("gem", "star") else (Decimal("0.10"), Decimal("0.05"))
            matched = next((ratio for ratio in ratios if abs(
                close - float((Decimal(str(reference)) * (1 + ratio)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ))
            ) <= 0.001), None)
            flags[index] = matched is not None
            if matched is None or not start_date <= session <= end_date:
                continue
            consecutive, cursor = 1, index - 1
            while cursor >= 0 and flags[cursor]:
                consecutive, cursor = consecutive + 1, cursor - 1
            prior_5d = _finite_number(bars[index - 5].get("close")) if index >= 5 else None
            total, floating, capital_date, basis = historical_capital(
                finance, corporate_actions, session, collected_at.date()
            )
            exchange = str(security.get("exchange") or security["market"])
            volume = _finite_number(current.get("vol", current.get("volume")))
            industry_full = (industries or {}).get(symbol)
            row = {
                "trade_date": session,
                "instrument_id": f"{exchange}:stock:{symbol}",
                "symbol": symbol, "name": str(security.get("name") or symbol),
                "exchange": exchange, "board": board,
                "industry": industry_full.split("-")[0].strip() if industry_full else "未分类",
                "open": opening, "high": high, "low": low, "close": close,
                "previous_close": reference, "limit_price": close,
                "volume_hands": volume, "amount_cny": _finite_number(current.get("amount")),
                "total_shares": total, "float_shares": floating,
                "total_market_cap_cny": close * total if total else None,
                "float_market_cap_cny": close * floating if floating else None,
                "turnover_pct": volume * 10000 / floating if volume is not None and floating else None,
                "amplitude_pct": (high - low) / reference * 100,
                "open_change_pct": (opening / reference - 1) * 100,
                "change_pct": (close / reference - 1) * 100,
                "return_5d_pct": (close / prior_5d - 1) * 100 if prior_5d and prior_5d > 0 else None,
                "consecutive_limit_days": consecutive,
                "capital_as_of_date": capital_date,
                "classification_as_of_date": collected_at.date(),
                "source": "mootdx", "collected_at": collected_at,
                "raw": {
                    "bar": _json_value(current), "unadjusted_previous_close": previous,
                    "reference_basis": "xdxr" if relevant_actions else "previous_close",
                    "capital_basis": basis,
                    "classification_basis": "current_mootdx_f10",
                    "industry_full": industry_full,
                    "limit_ratio": float(matched),
                    "limit_rule_basis": "unverified_5pct_candidate" if matched == Decimal("0.05") else "board_price_band",
                    "return_5d_basis": "unadjusted_close",
                },
            }
            for field in ("amplitude_pct", "open_change_pct", "change_pct", "return_5d_pct", "turnover_pct"):
                value = row[field]
                row[field] = round(value, 6) if value is not None and math.isfinite(value) else None
            results.append(row)
    return sorted(results, key=lambda item: (item["trade_date"], item["symbol"]))
