"""Forward-only paper execution for the first-board positive-return campaign."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from ..execution_analysis import classify_candidate
from ..intraday import plan_for
from ..t1_research import (
    cash_paid,
    cents,
    minute_quality,
    sell_proxy,
    target_quote,
)
from .report_worker import ReportWorker


CAMPAIGN_CODE = "first-board-positive-v1"
PAPER_POSITION_CNY = 10_000.0


def execution_model(target_net_pct: float) -> dict[str, Any]:
    return {
        "position_cny": PAPER_POSITION_CNY,
        "lot_shares": 100,
        "minimum_shares_by_board": {"main": 100},
        "slippage_each_side": 0.001,
        "commission_rate_each_side": 0.0003,
        "minimum_commission_each_side_cny": 5.0,
        "transfer_fee_each_side": 0.00001,
        "sell_stamp_tax": 0.0005,
        "maximum_participation_in_observed_minute_volume": 0.01,
        "target_net_pct": target_net_pct,
    }


def _minute_data(
    bars: Sequence[Mapping[str, Any]],
) -> Optional[dict[str, list[float]]]:
    if len(bars) != 240:
        return None
    prices = []
    volumes = []
    for bar in bars:
        try:
            price = float(bar["close"])
            volume = float(bar.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None
        if not math.isfinite(volume) or volume < 0:
            return None
        prices.append(price)
        volumes.append(volume)
    return {"prices": prices, "volumes": volumes}


def _next_session(
    sessions: Sequence[date],
    current: date,
) -> Optional[date]:
    return next((session for session in sessions if session > current), None)


def _daily_bar(
    provider: Any,
    symbol: str,
    session: date,
) -> Optional[dict[str, Any]]:
    bars = provider.history_bars(
        symbol,
        "day",
        end_date=session,
        max_bars=20,
    )
    return next(
        (
            {
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar.get("volume") or 0),
            }
            for bar in reversed(bars)
            if str(bar.get("date") or bar.get("time", ""))[:10]
            == session.isoformat()
        ),
        None,
    )


@dataclass(frozen=True)
class PaperRunResult:
    trade_date: str
    plan_generated: bool
    entries_processed: int
    exits_processed: int
    summary: Mapping[str, Any]


class PaperTradingWorker:
    def __init__(
        self,
        *,
        repository: Any,
        provider: Any,
        storage_settings: Any,
        strategy_config_path: Path,
        output_dir: Path,
        logger: Any = None,
        report_generator: Optional[Callable[[str, date], None]] = None,
    ):
        self.repository = repository
        self.provider = provider
        self.storage_settings = storage_settings
        self.strategy_config_path = strategy_config_path
        self.output_dir = output_dir
        self.logger = logger
        self.report_generator = report_generator

    def run(self, target_date: date) -> PaperRunResult:
        campaign = self.repository.ensure_paper_campaign(CAMPAIGN_CODE)
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
            plan_generated = self._generate_plan(
                campaign,
                target_date,
                sessions,
            )
            entries = self._evaluate_entries(
                campaign,
                target_date,
                sessions,
            )
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
        existing = self.repository.get_strategy_day(
            campaign["strategy_id"],
            target_date.isoformat(),
        )
        if existing and existing.get("next_plan"):
            return False
        if self.report_generator is not None:
            self.report_generator(campaign["strategy_id"], target_date)
            return True
        worker = ReportWorker(
            strategy_config_path=self.strategy_config_path,
            output_dir=self.output_dir,
            storage_settings=self.storage_settings,
            provider=self.provider,
            strategy_id=campaign["strategy_id"],
        )
        worker.run(target_date)
        return True

    def _evaluate_entries(
        self,
        campaign: Mapping[str, Any],
        target_date: date,
        sessions: Sequence[date],
    ) -> int:
        records = self.repository.list_paper_entry_candidates(
            campaign["campaign_id"],
            target_date,
        )
        target_pct = float(
            campaign["config_snapshot"].get(
                "next_day_take_profit_pct",
                5.1,
            )
        )
        model = execution_model(target_pct)
        processed = 0
        for record in records:
            candidate = record["candidate"]
            symbol = str(candidate["code"])
            entry_date = date.fromisoformat(record["entry_date"])
            minute = _minute_data(
                self.provider.historical_minutes(symbol, entry_date)
            )
            bar = _daily_bar(self.provider, symbol, entry_date)
            base = {
                "plan_id": record["plan_id"],
                "symbol": symbol,
                "name": str(candidate["name"]),
                "industry": candidate.get("industry"),
                "reference_date": record["reference_date"],
                "entry_date": record["entry_date"],
            }
            quality = (
                minute_quality(minute, bar)
                if minute is not None and bar is not None
                else "missing"
            )
            if quality != "ok":
                self.repository.save_paper_entry(
                    campaign["campaign_id"],
                    {
                        **base,
                        "status": "entry_data_missing",
                        "rejection_reason": f"D2 行情质量检查失败：{quality}",
                    },
                )
                processed += 1
                continue
            prepared = {
                **candidate,
                "plan": candidate.get("plan") or plan_for(candidate),
            }
            classified = classify_candidate(
                prepared,
                record["entry_date"],
                bar,
                minute,
            )
            if not classified["buyable"]:
                self.repository.save_paper_entry(
                    campaign["campaign_id"],
                    {
                        **base,
                        "status": "rejected",
                        "rejection_reason": classified["reason"],
                        "entry_evidence": classified,
                    },
                )
                processed += 1
                continue
            buy_index = int(classified["buy_index"])
            sample_price = float(minute["prices"][buy_index])
            buy_price = cents(
                sample_price * (1 + model["slippage_each_side"]),
                up=True,
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
                and cash_paid(buy_price, shares, model)
                > model["position_cny"]
            ):
                shares -= model["lot_shares"]
            capacity = (
                minute["volumes"][buy_index]
                * 100
                * model[
                    "maximum_participation_in_observed_minute_volume"
                ]
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
                            **classified,
                            "capacity_shares": capacity,
                            "requested_shares": shares,
                        },
                    },
                )
                processed += 1
                continue
            exit_date = _next_session(sessions, entry_date)
            if exit_date is None:
                self.repository.save_paper_entry(
                    campaign["campaign_id"],
                    {
                        **base,
                        "status": "entry_data_missing",
                        "rejection_reason": "缺少 D3 交易日",
                        "entry_evidence": classified,
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
                    "entry_time": classified["buy_window_time"],
                    "entry_price": buy_price,
                    "entry_sample_price": sample_price,
                    "shares": shares,
                    "target_price": quote,
                    "entry_evidence": {
                        **classified,
                        "entry_day_close": bar["close"],
                        "position_cny": model["position_cny"],
                        "capacity_shares": capacity,
                        "execution_model": model,
                    },
                },
            )
            processed += 1
        return processed

    def _settle_exits(
        self,
        campaign: Mapping[str, Any],
        target_date: date,
    ) -> int:
        records = self.repository.list_paper_pending_exits(
            campaign["campaign_id"],
            target_date,
        )
        model = execution_model(
            float(
                campaign["config_snapshot"].get(
                    "next_day_take_profit_pct",
                    5.1,
                )
            )
        )
        processed = 0
        for record in records:
            exit_date = date.fromisoformat(record["exit_date"])
            minute = _minute_data(
                self.provider.historical_minutes(
                    record["symbol"],
                    exit_date,
                )
            )
            sell_bar = _daily_bar(
                self.provider,
                record["symbol"],
                exit_date,
            )
            quality = (
                minute_quality(minute, sell_bar)
                if minute is not None and sell_bar is not None
                else "missing"
            )
            if quality != "ok":
                self.repository.save_paper_exit(
                    record["paper_trade_id"],
                    {
                        "status": "exit_data_missing",
                        "exit_reason": f"D3 行情质量检查失败：{quality}",
                        "exit_evidence": {},
                    },
                )
                processed += 1
                continue
            buy = {
                "shares": record["shares"],
                "buy_price": record["entry_price"],
            }
            outcome = sell_proxy(
                buy,
                {
                    "close": float(
                        record["entry_evidence"]["entry_day_close"]
                    )
                },
                sell_bar,
                minute,
                "main",
                "target",
                model,
            )
            if outcome["sell_status"] != "filled_proxy":
                result = {
                    "status": "exit_unfilled",
                    "exit_reason": outcome["sell_reason"],
                    "positive": False,
                    "exit_evidence": {
                        **outcome,
                        "exit_day_close": sell_bar["close"],
                    },
                }
            else:
                net_return_pct = float(outcome["net_return_pct"])
                result = {
                    "status": "closed",
                    "exit_time": outcome["sell_time"],
                    "exit_price": outcome["sell_price"],
                    "exit_reason": outcome["sell_reason"],
                    "net_return_pct": net_return_pct,
                    "positive": net_return_pct > 0,
                    "exit_evidence": {
                        **outcome,
                        "exit_day_close": sell_bar["close"],
                    },
                }
            self.repository.save_paper_exit(
                record["paper_trade_id"],
                result,
            )
            processed += 1
        return processed
