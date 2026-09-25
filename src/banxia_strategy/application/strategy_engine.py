from __future__ import annotations

from datetime import datetime
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from ..contracts.topics import (
    MARKET_FEATURE_REALTIME,
    MARKET_QUOTE_SNAPSHOT,
    STRATEGY_DECISION,
    STRATEGY_PLAN_CREATED,
)
from ..domain.events import EventEnvelope
from ..domain.intraday import IRREVERSIBLE_STATES, evaluate, resolve_transition
from ..intraday import plan_for
from ..ports.messaging import ConsumedEvent, EventConsumer
from ..ports.storage import DecisionRecord, DecisionRepository


class StrategyEventProcessor:
    """Apply one market event to the authoritative PostgreSQL state machine."""

    def __init__(
        self,
        *,
        repository: DecisionRepository,
        plan_id: str,
        strategy_version_id: str,
        strategy_version: str = "v1",
        candidates: Sequence[Mapping[str, Any]],
        minimum_sector_sample_size: int = 2,
        minimum_sector_rise_ratio: float = 0.5,
    ):
        self.repository = repository
        self.replace_plan(
            plan_id=plan_id,
            strategy_version_id=strategy_version_id,
            strategy_version=strategy_version,
            candidates=candidates,
        )
        self.minimum_sector_sample_size = minimum_sector_sample_size
        self.minimum_sector_rise_ratio = minimum_sector_rise_ratio
        self.features: Dict[str, Mapping[str, Any]] = {}

    def replace_plan(
        self,
        *,
        plan_id: str,
        strategy_version_id: str,
        strategy_version: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> None:
        self.plan_id = plan_id
        self.strategy_version_id = strategy_version_id
        self.strategy_version = strategy_version
        self.candidates = {
            str(candidate.get("code") or candidate["symbol"]): dict(candidate)
            for candidate in candidates
        }

    def process(self, consumed: ConsumedEvent) -> Optional[EventEnvelope]:
        source = consumed.event
        if source.event_type == MARKET_FEATURE_REALTIME:
            self.features[str(source.payload["symbol"])] = source.payload
            return None
        if source.event_type != MARKET_QUOTE_SNAPSHOT:
            return None
        payload = source.payload
        symbol = str(payload["symbol"])
        candidate = self.candidates.get(symbol)
        if candidate is None:
            return None
        source_time = datetime.fromisoformat(str(payload["source_time"]))
        collected_at = datetime.fromisoformat(str(payload["collected_at"]))
        if source_time.date().isoformat() != str(candidate["plan_date"]):
            return None
        age = (collected_at - source_time).total_seconds()
        plan = plan_for(candidate)
        opening = payload.get("open")
        previous_close = plan["previous_close"]
        quote = {
            "price": payload.get("price"),
            "open": opening,
            "high": payload.get("high"),
            "low": payload.get("low"),
            "previous_close": payload.get("previous_close"),
            "change_pct": (
                round((float(payload["price"]) / previous_close - 1) * 100, 4)
                if payload.get("price") is not None and previous_close
                else None
            ),
            "open_change_pct": (
                round((float(opening) / previous_close - 1) * 100, 4)
                if opening is not None and previous_close
                else None
            ),
            "amount": payload.get("cumulative_amount_cny"),
            "volume": payload.get("cumulative_volume"),
            "bid": payload.get("bid1"),
            "ask": payload.get("ask1"),
            "bid_volume": payload.get("bid1_volume"),
            "ask_volume": payload.get("ask1_volume"),
            "quote_time": source_time.isoformat(),
            "fresh": -5 <= age <= plan.get("quote_max_age_seconds", 180),
        }
        proposed = evaluate(
            quote,
            plan,
            collected_at,
            str(candidate["plan_date"]),
        )
        feature = self.features.get(symbol, {})
        if feature.get("trade_date") and str(feature["trade_date"]) != str(candidate["plan_date"]):
            feature = {}
        feature_attributes = feature.get("attributes", {})
        sector_sample_size = feature_attributes.get("sector_sample_size")
        sector_rise_ratio = feature.get("sector_rise_ratio")
        minimum_sample = plan.get("minimum_sector_sample_size", self.minimum_sector_sample_size)
        minimum_ratio = plan.get("minimum_sector_rise_ratio", self.minimum_sector_rise_ratio)
        if (
            proposed["state"] in {"watch", "near_limit", "at_limit", "sealed"}
            and sector_sample_size is not None
            and int(sector_sample_size) >= minimum_sample
            and sector_rise_ratio is not None
            and float(sector_rise_ratio) < minimum_ratio
        ):
            proposed = {
                "state": "watch",
                "label": "板块确认不足",
                "reason": (
                    f"同题材上涨比例{float(sector_rise_ratio):.0%}，"
                    f"低于{minimum_ratio:.0%}确认线，继续观察。"
                ),
                "tone": "muted",
            }
        elif (
            proposed["state"] in {"watch", "near_limit", "at_limit", "sealed"}
            and "minimum_sector_sample_size" in plan
            and (sector_sample_size is None or int(sector_sample_size) < minimum_sample or sector_rise_ratio is None)
        ):
            proposed = {
                "state": "watch", "label": "板块样本不足",
                "reason": f"同题材至少需要{minimum_sample}个有效样本，当前无法完成确认。",
                "tone": "muted",
            }
        current = self.repository.get(self.plan_id, symbol)
        previous = (
            {
                "state": current.state,
                "reason": current.reason,
                "label": current.state,
                "tone": "risk" if current.irreversible else "neutral",
            }
            if current is not None
            else {}
        )
        resolved = resolve_transition(previous, proposed)
        state = str(resolved["state"])
        version = 1 if current is None else current.version + 1
        occurred_at = source_time
        decision_event = EventEnvelope.create(
            event_type=STRATEGY_DECISION,
            producer="strategy-engine",
            occurred_at=occurred_at,
            identity={
                "plan_id": self.plan_id,
                "symbol": symbol,
                "source_event_id": source.event_id,
                "state": state,
            },
            payload={
                "decision_id": "",
                "plan_id": self.plan_id,
                "symbol": symbol,
                "previous_state": current.state if current is not None else None,
                "state": state,
                "label": resolved.get("label", state),
                "reason_code": state,
                "reason": str(resolved["reason"]),
                "irreversible": state in IRREVERSIBLE_STATES,
                "source_event_id": source.event_id,
                "source_event_type": source.event_type,
                "strategy_version": self.strategy_version,
                "strategy_version_id": self.strategy_version_id,
                "updated_at": collected_at,
                "rule_inputs": {
                    "plan": plan,
                    "quote": quote,
                    "feature": feature,
                },
            },
            trace_id=source.trace_id,
        )
        decision_event.payload["decision_id"] = decision_event.event_id
        decision = DecisionRecord(
            plan_id=self.plan_id,
            symbol=symbol,
            state=state,
            reason_code=state,
            reason=str(resolved["reason"]),
            source_event_id=source.event_id,
            strategy_version=self.strategy_version_id,
            irreversible=state in IRREVERSIBLE_STATES,
            occurred_at=occurred_at,
            version=version,
        )
        applied = self.repository.apply(
            input_event_id=source.event_id,
            decision=decision,
            outbox_event=decision_event,
            topic=consumed.topic,
            partition=consumed.partition,
            offset=consumed.offset,
        )
        return decision_event if applied else None


class StrategyWorker:
    def __init__(
        self,
        *,
        consumer: EventConsumer,
        processor: StrategyEventProcessor,
        plan_loader: Optional[
            Callable[[str], Optional[Mapping[str, Any]]]
        ] = None,
        plans_loader=None,
    ):
        self.consumer = consumer
        self.processor = processor
        self.plan_loader = plan_loader
        self.plans_loader = plans_loader
        self.processors = {}
        self.last_reload = 0.0

    def reload_plans(self):
        plans = self.plans_loader()
        active = {}
        for plan in plans:
            plan_id = str(plan["plan_id"])
            candidates = [{**item, "code": item["symbol"], "plan_date": plan["trade_date"],
                           "reference_date": plan["reference_date"]} for item in plan["candidates"]]
            processor = self.processors.get(plan_id)
            if processor is None:
                processor = StrategyEventProcessor(
                    repository=self.processor.repository, plan_id=plan_id,
                    strategy_version_id=str(plan["strategy_version_id"]),
                    strategy_version=str(plan["strategy_version"]), candidates=candidates,
                )
            else:
                processor.replace_plan(plan_id=plan_id, strategy_version_id=str(plan["strategy_version_id"]),
                                       strategy_version=str(plan["strategy_version"]), candidates=candidates)
            active[plan_id] = processor
        self.processors = active
        self.last_reload = time.monotonic()

    def run_once(self, timeout: float = 1.0) -> bool:
        if self.plans_loader is not None and time.monotonic() - self.last_reload >= 5:
            self.reload_plans()
        consumed = self.consumer.poll(timeout)
        if consumed is None:
            return False
        if consumed.event.event_type == STRATEGY_PLAN_CREATED:
            if self.plans_loader is not None:
                self.reload_plans()
            elif self.plan_loader is not None:
                trade_date = str(consumed.event.payload["trade_date"])
                plan = self.plan_loader(trade_date)
                if plan is None:
                    raise RuntimeError(
                        f"strategy plan projection is unavailable for {trade_date}"
                    )
                candidates = [
                    {
                        **candidate,
                        "code": candidate["symbol"],
                        "plan_date": plan["trade_date"],
                        "reference_date": plan["reference_date"],
                    }
                    for candidate in plan["candidates"]
                ]
                self.processor.replace_plan(
                    plan_id=str(plan["plan_id"]),
                    strategy_version_id=str(plan["strategy_version_id"]),
                    strategy_version=str(plan["strategy_version"]),
                    candidates=candidates,
                )
            self.consumer.commit(consumed)
            return True
        if self.plans_loader is not None:
            for processor in self.processors.values():
                processor.process(consumed)
        else:
            self.processor.process(consumed)
        self.consumer.commit(consumed)
        return True

    def close(self) -> None:
        for target in (self.consumer, self.processor.repository):
            close = getattr(target, "close", None)
            if close is not None:
                close()
