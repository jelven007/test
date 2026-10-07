"""Fusion L7 paper-trading pipeline (L2+L4+L5+L6 → Layer 7 dynamic exit).

Candidate universe: full A-share scan on D1 with d1_change_pct > 3%, ST/*ST
and sealed limit-up removed. Layer 4 Rule A (last-30m r_last30 < -0.5% and
close_location_day < 0.75) selects weakness-to-strength patterns. Layer 5
LHB reverse filter is wired as a config toggle; when the provider has no LHB
feed, the first version falls back to pass-through (lhb_check_enabled=false).

On D2 the worker fetches minute bars, applies the Layer 6 gap gate
(gap_pct_d2 ∈ [-1.0, +4.0]), then enters at the first-minute price with the
10 000 CNY ≤1% participation guardrail. To comply with A-share T+1 rules,
Layer 7 starts only on D3: TP 5.0% / SL 2.5% as the first touched threshold,
then a 14:55 force-exit instruction proxied by the 14:56 minute price.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ..layer7_exit import simulate_t1_layer7_exit
from ..t1_research import cash_paid, cents, target_quote
from .paper_trading import (
    PaperRunResult,
    PaperTradingWorker,
    _daily_bar,
    _minute_data,
    _next_session,
    execution_model,
)


FUSION_CAMPAIGN_CODE = "fusion-l7-v1"
LAYER2_D1_CHANGE_MIN_PCT = 3.0
LAYER4_RULE_A_R_LAST30_MAX_PCT = -0.5
LAYER4_RULE_A_CLOSE_LOC_MAX = 0.75
LAYER6_GAP_MIN_PCT = -1.0
LAYER6_GAP_MAX_PCT = 4.0
LAYER7_TAKE_PROFIT_PCT = 5.0
LAYER7_STOP_LOSS_PCT = 2.5
MAX_CONCURRENT_POSITIONS = 5


def _is_st_name(name: str) -> bool:
    cleaned = name.replace(" ", "").upper()
    return "ST" in cleaned or "退" in cleaned


def _is_sealed(features: Mapping[str, Any]) -> bool:
    r = features.get("r_last30_pct")
    slope = features.get("slope_last30_bps_per_min")
    close_loc = features.get("close_location_day")
    return (
        r is not None
        and slope is not None
        and close_loc is not None
        and abs(r) < 1e-9
        and abs(slope) < 1e-9
        and close_loc >= 0.9999
    )


def _ret_pct(current: float, previous: float) -> Optional[float]:
    if previous is None or previous <= 0 or current is None or current <= 0:
        return None
    return (current / previous - 1.0) * 100.0


def extract_d1_features(
    minute_bars: Sequence[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    data = _minute_data(minute_bars)
    if data is None:
        return None
    prices = data["prices"]
    volumes = data["volumes"]
    day_high = max(prices)
    day_low = min(prices)
    day_close = prices[-1]
    day_vol = sum(volumes)
    if day_vol <= 0 or day_close <= 0 or day_high <= day_low:
        return None
    minute_returns = [
        _ret_pct(prices[i], prices[i - 1]) for i in range(1, 240)
    ]
    xs = list(range(30))
    last30 = prices[-30:]
    xbar = sum(xs) / 30.0
    ybar = sum(last30) / 30.0
    num = sum((xs[i] - xbar) * (last30[i] - ybar) for i in range(30))
    den = sum((xs[i] - xbar) ** 2 for i in range(30))
    slope = (num / den) / day_close * 100.0 if den > 0 else None
    return {
        "r_last30_pct": _ret_pct(day_close, prices[-31]),
        "slope_last30_bps_per_min": (
            slope * 100.0 if slope is not None else None
        ),
        "close_location_day": (day_close - day_low) / (day_high - day_low),
        "day_close": day_close,
        "minute_returns": minute_returns,
    }


def pass_layer4_rule_a(features: Mapping[str, Any]) -> bool:
    r = features.get("r_last30_pct")
    cl = features.get("close_location_day")
    return (
        r is not None
        and cl is not None
        and r < LAYER4_RULE_A_R_LAST30_MAX_PCT
        and cl < LAYER4_RULE_A_CLOSE_LOC_MAX
    )


def pass_layer6_gap(gap_pct_d2: float) -> bool:
    return LAYER6_GAP_MIN_PCT <= gap_pct_d2 <= LAYER6_GAP_MAX_PCT


def simulate_layer7_exit(
    buy_price: float,
    d3_prices: Sequence[float],
    *,
    tp_pct: float = LAYER7_TAKE_PROFIT_PCT / 100.0,
    sl_pct: float = LAYER7_STOP_LOSS_PCT / 100.0,
) -> tuple[float, str, Optional[int]]:
    return simulate_t1_layer7_exit(
        buy_price,
        d3_prices,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
    )


def _minute_index_to_time(index: int) -> time:
    base_minutes = (
        9 * 60 + 31 + index
        if index < 120
        else 13 * 60 + 1 + (index - 120)
    )
    return time(hour=base_minutes // 60, minute=base_minutes % 60)


@dataclass(frozen=True)
class FusionCandidate:
    symbol: str
    name: str
    industry: Optional[str]
    d1_close: float
    d1_change_pct: float
    r_last30_pct: float
    close_location_day: float


class FusionL7CandidateScanner:
    def __init__(
        self,
        *,
        repository: Any,
        provider: Any,
        logger: Any = None,
    ):
        self.repository = repository
        self.provider = provider
        self.logger = logger

    def scan(
        self,
        campaign_id: str,
        d1_date: date,
        d2_date: date,
    ) -> int:
        securities = list(self.provider.securities())
        scanned = 0
        kept = 0
        for security in securities:
            name = str(security.get("name") or "")
            if _is_st_name(name):
                continue
            symbol = str(security["symbol"])
            scanned += 1
            try:
                bars = self.provider.history_bars(
                    symbol, "day", end_date=d1_date, max_bars=5
                )
            except Exception:
                continue
            relevant = [
                bar for bar in bars if str(bar.get("date", ""))[:10] <= d1_date.isoformat()
            ]
            if len(relevant) < 2:
                continue
            relevant.sort(key=lambda bar: str(bar["date"]))
            prev_bar = relevant[-2]
            d1_bar = relevant[-1]
            if str(d1_bar.get("date", "")) != d1_date.isoformat():
                continue
            previous_close = float(prev_bar["close"])
            d1_close = float(d1_bar["close"])
            if previous_close <= 0 or d1_close <= 0:
                continue
            d1_change_pct = (d1_close / previous_close - 1.0) * 100.0
            if d1_change_pct < LAYER2_D1_CHANGE_MIN_PCT:
                continue
            try:
                minute = self.provider.historical_minutes(symbol, d1_date)
            except Exception:
                continue
            features = extract_d1_features(minute)
            if features is None or _is_sealed(features):
                continue
            if not pass_layer4_rule_a(features):
                continue
            record = {
                "d1_date": d1_date.isoformat(),
                "d2_date": d2_date.isoformat(),
                "symbol": symbol,
                "name": name,
                "industry": security.get("industry"),
                "d1_close": d1_close,
                "d1_change_pct": d1_change_pct,
                "r_last30_pct": features["r_last30_pct"],
                "close_location_day": features["close_location_day"],
                "evidence": {
                    "layer2_d1_change_pct_min": LAYER2_D1_CHANGE_MIN_PCT,
                    "layer4_rule_a": {
                        "r_last30_pct_max": LAYER4_RULE_A_R_LAST30_MAX_PCT,
                        "close_location_day_max": LAYER4_RULE_A_CLOSE_LOC_MAX,
                    },
                    "previous_close": previous_close,
                },
                "lhb_check_enabled": False,
            }
            self.repository.upsert_fusion_l7_candidate(campaign_id, record)
            kept += 1
        if self.logger is not None:
            self.logger.info(
                "fusion-l7 candidate scan complete",
                extra={
                    "d1_date": d1_date.isoformat(),
                    "d2_date": d2_date.isoformat(),
                    "scanned": scanned,
                    "kept": kept,
                },
            )
        return kept


class FusionL7PaperTradingWorker:
    def __init__(
        self,
        *,
        repository: Any,
        provider: Any,
        storage_settings: Any,
        strategy_config_path: Path,
        output_dir: Path,
        logger: Any = None,
        scanner: Optional[FusionL7CandidateScanner] = None,
    ):
        self.repository = repository
        self.provider = provider
        self.storage_settings = storage_settings
        self.strategy_config_path = strategy_config_path
        self.output_dir = output_dir
        self.logger = logger
        self.scanner = scanner or FusionL7CandidateScanner(
            repository=repository, provider=provider, logger=logger
        )
        self._base = PaperTradingWorker(
            repository=repository,
            provider=provider,
            storage_settings=storage_settings,
            strategy_config_path=strategy_config_path,
            output_dir=output_dir,
            logger=logger,
            campaign_code=FUSION_CAMPAIGN_CODE,
        )

    def run(self, target_date: date) -> PaperRunResult:
        campaign = self.repository.ensure_paper_campaign(FUSION_CAMPAIGN_CODE)
        sessions = sorted(
            session
            for session in self.provider.trading_dates()
            if session >= date.fromisoformat(campaign["started_on"])
        )
        self.repository.save_trading_sessions(sessions)

        exits = self._settle_exits(campaign, target_date)
        summary = self.repository.refresh_paper_campaign(
            campaign["campaign_id"]
        )
        plan_generated = False
        entries = 0
        if summary["status"] == "running":
            plan_generated = self._generate_plan(campaign, target_date, sessions)
            entries = self._evaluate_entries(campaign, target_date, sessions)
            summary = self.repository.refresh_paper_campaign(
                campaign["campaign_id"]
            )
        return PaperRunResult(
            trade_date=target_date.isoformat(),
            plan_generated=plan_generated,
            entries_processed=entries,
            exits_processed=exits,
            summary=summary,
        )

    def _generate_plan(
        self,
        campaign: Mapping[str, Any],
        target_date: date,
        sessions: Sequence[date],
    ) -> bool:
        if target_date not in sessions:
            return False
        previous_sessions = [s for s in sessions if s < target_date]
        if not previous_sessions:
            return False
        d1_date = previous_sessions[-1]
        existing = self.repository.list_fusion_l7_candidates(
            campaign["campaign_id"], target_date
        )
        if existing:
            return False
        self.scanner.scan(campaign["campaign_id"], d1_date, target_date)
        return True

    def _evaluate_entries(
        self,
        campaign: Mapping[str, Any],
        target_date: date,
        sessions: Sequence[date],
    ) -> int:
        candidates = self.repository.list_fusion_l7_candidates(
            campaign["campaign_id"], target_date
        )
        model = execution_model(LAYER7_TAKE_PROFIT_PCT)
        exit_date = _next_session(sessions, target_date)
        processed = 0
        taken = 0
        for candidate in candidates:
            if taken >= MAX_CONCURRENT_POSITIONS:
                break
            symbol = str(candidate["symbol"])
            base = {
                "fusion_candidate_id": candidate["candidate_id"],
                "symbol": symbol,
                "name": str(candidate["name"]),
                "industry": candidate.get("industry"),
                "reference_date": candidate["d1_date"],
                "entry_date": target_date.isoformat(),
            }
            minute = _minute_data(
                self.provider.historical_minutes(symbol, target_date)
            )
            bar = _daily_bar(self.provider, symbol, target_date)
            if minute is None or bar is None:
                self.repository.save_paper_entry(
                    campaign["campaign_id"],
                    {
                        **base,
                        "status": "entry_data_missing",
                        "rejection_reason": "D2 分钟或日线行情缺失",
                    },
                )
                processed += 1
                continue
            d1_close = float(candidate["d1_close"])
            d2_open = float(minute["prices"][0])
            gap_pct_d2 = (d2_open / d1_close - 1.0) * 100.0
            if not pass_layer6_gap(gap_pct_d2):
                self.repository.save_paper_entry(
                    campaign["campaign_id"],
                    {
                        **base,
                        "status": "rejected",
                        "rejection_reason": (
                            f"Layer 6 跳空出界 {gap_pct_d2:+.2f}%"
                        ),
                        "entry_evidence": {
                            "gap_pct_d2": gap_pct_d2,
                            "layer6_gap_min_pct": LAYER6_GAP_MIN_PCT,
                            "layer6_gap_max_pct": LAYER6_GAP_MAX_PCT,
                        },
                    },
                )
                processed += 1
                continue
            if exit_date is None:
                self.repository.save_paper_entry(
                    campaign["campaign_id"],
                    {
                        **base,
                        "status": "entry_data_missing",
                        "rejection_reason": "缺少 D3 交易日",
                        "entry_evidence": {"gap_pct_d2": gap_pct_d2},
                    },
                )
                processed += 1
                continue
            sample_price = d2_open
            buy_price = cents(
                sample_price * (1 + model["slippage_each_side"]), up=True
            )
            shares = (
                math.floor(
                    model["position_cny"]
                    / buy_price
                    / model["lot_shares"]
                )
                * model["lot_shares"]
            )
            while (
                shares > 0
                and cash_paid(buy_price, shares, model) > model["position_cny"]
            ):
                shares -= model["lot_shares"]
            capacity = (
                minute["volumes"][0]
                * 100
                * model["maximum_participation_in_observed_minute_volume"]
            )
            if shares < model["minimum_shares_by_board"]["main"]:
                rejection = "1 万元模拟预算不足一手"
            elif shares > capacity:
                rejection = "模拟委托超过该分钟成交量的 1%"
            else:
                rejection = None
            if rejection:
                self.repository.save_paper_entry(
                    campaign["campaign_id"],
                    {
                        **base,
                        "status": "rejected",
                        "rejection_reason": rejection,
                        "entry_evidence": {
                            "gap_pct_d2": gap_pct_d2,
                            "capacity_shares": capacity,
                            "requested_shares": shares,
                        },
                    },
                )
                processed += 1
                continue
            quote = target_quote(buy_price, shares, model)
            self.repository.save_paper_entry(
                campaign["campaign_id"],
                {
                    **base,
                    "exit_date": exit_date.isoformat(),
                    "status": "open",
                    "entry_time": "09:31:00",
                    "entry_price": buy_price,
                    "entry_sample_price": sample_price,
                    "shares": shares,
                    "target_price": quote,
                    "entry_evidence": {
                        "gap_pct_d2": gap_pct_d2,
                        "entry_day_close": bar["close"],
                        "position_cny": model["position_cny"],
                        "capacity_shares": capacity,
                        "execution_model": model,
                        "layer7_take_profit_pct": LAYER7_TAKE_PROFIT_PCT,
                        "layer7_stop_loss_pct": LAYER7_STOP_LOSS_PCT,
                        "layer7_exit_session": "D3",
                        "layer7_force_exit_instruction": "14:55",
                        "layer7_force_exit_proxy": "14:56",
                    },
                },
            )
            processed += 1
            taken += 1
        return processed

    def _settle_exits(
        self,
        campaign: Mapping[str, Any],
        target_date: date,
    ) -> int:
        records = self.repository.list_paper_pending_exits(
            campaign["campaign_id"], target_date
        )
        model = execution_model(LAYER7_TAKE_PROFIT_PCT)
        processed = 0
        for record in records:
            exit_date = date.fromisoformat(record["exit_date"])
            d3_minute = _minute_data(
                self.provider.historical_minutes(record["symbol"], exit_date)
            )
            if d3_minute is None:
                self.repository.save_paper_exit(
                    record["paper_trade_id"],
                    {
                        "status": "exit_data_missing",
                        "exit_reason": "Layer 7 D3 分钟数据缺失",
                        "exit_evidence": {},
                    },
                )
                processed += 1
                continue
            buy_price = float(record["entry_price"])
            exit_price_raw, reason, minute_index = simulate_layer7_exit(
                buy_price,
                d3_minute["prices"],
            )
            exit_time = _minute_index_to_time(minute_index or 0)
            sell_price = cents(
                exit_price_raw * (1 - model["slippage_each_side"]), up=False
            )
            shares = int(record["shares"])
            gross = (sell_price - buy_price) * shares
            commission_sell = max(
                sell_price * shares * model["commission_rate_each_side"],
                model["minimum_commission_each_side_cny"],
            )
            commission_buy = max(
                buy_price * shares * model["commission_rate_each_side"],
                model["minimum_commission_each_side_cny"],
            )
            transfer_sell = sell_price * shares * model["transfer_fee_each_side"]
            transfer_buy = buy_price * shares * model["transfer_fee_each_side"]
            stamp = sell_price * shares * model["sell_stamp_tax"]
            total_cost = (
                commission_sell
                + commission_buy
                + transfer_sell
                + transfer_buy
                + stamp
            )
            net = gross - total_cost
            cost_basis = cash_paid(buy_price, shares, model)
            net_return_pct = (net / cost_basis * 100.0) if cost_basis > 0 else 0.0
            self.repository.save_paper_exit(
                record["paper_trade_id"],
                {
                    "status": "closed",
                    "exit_time": exit_time.isoformat(),
                    "exit_price": sell_price,
                    "exit_reason": reason,
                    "net_return_pct": net_return_pct,
                    "positive": net_return_pct > 0,
                    "exit_evidence": {
                        "layer7_reason": reason,
                        "minute_index": minute_index,
                        "d3_prices_length": len(d3_minute["prices"]),
                        "d3_open": d3_minute["prices"][0],
                        "exit_date": exit_date.isoformat(),
                        "t1_compliant": True,
                    },
                },
            )
            processed += 1
        return processed
