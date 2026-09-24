from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict

from .domain.intraday import (
    ACTIVE_PHASES,
    PHASE_LABELS,
    SHANGHAI,
    advice,
    evaluate,
    phase_at,
)
from .mootdx_provider import MootdxProvider


WATCH_CODES = ("002635", "603328", "002849")


def now_shanghai():
    return datetime.now(SHANGHAI)


def number(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def price_at(close, pct):
    result = Decimal(str(close)) * (1 + Decimal(str(pct)) / 100)
    return float(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def load_watchlist(path):
    """读取独立补充清单，不改写原日报及其评分。"""
    if path is None:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise ValueError("监控补充清单必须包含 candidates 数组")
    for key in ("as_of", "next_session"):
        datetime.strptime(payload.get(key, ""), "%Y-%m-%d")
    if payload["next_session"] <= payload["as_of"]:
        raise ValueError("监控计划交易日必须晚于昨收日期")
    if payload.get("data_source") != "mootdx":
        raise ValueError("监控补充数据必须来自 mootdx")
    result = []
    codes = set()
    for item in payload["candidates"]:
        if not isinstance(item, dict):
            raise ValueError("补充股票必须为对象")
        code = item.get("code", "")
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) or code in codes:
            raise ValueError("补充股票代码必须为不重复的六位字符串")
        if not item.get("name") or number(item.get("latest_price"), 0) <= 0:
            raise ValueError(f"{code} 缺少股票名称或有效昨收")
        if not isinstance(item.get("eligible"), bool) or not item.get("eligibility_reason"):
            raise ValueError(f"{code} 缺少静态筛选结果或依据")
        if any(not isinstance(item.get(key), str) for key in ("entry_trigger", "invalidation")):
            raise ValueError(f"{code} 缺少入场和放弃条件")
        codes.add(code)
        result.append({
            **item, "score": None, "origin": "supplement",
            "reference_date": payload["as_of"], "plan_date": payload["next_session"],
            "verified_at": payload.get("verified_at"), "data_source": payload["data_source"],
        })
    return result


def quote_time(raw, now):
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?", str(raw or ""))
    if not match:
        return None
    try:
        return now.replace(
            hour=int(match[1]), minute=int(match[2]), second=int(match[3]),
            microsecond=int((match[4] or "").ljust(6, "0")[:6]),
        )
    except ValueError:
        return None


def plan_for(candidate):
    entry = re.search(
        r"位于(-?\d+(?:\.\d+)?)%[～~-](-?\d+(?:\.\d+)?)%",
        candidate.get("entry_trigger", ""),
    )
    abandon = re.search(
        r"低于(-?\d+(?:\.\d+)?)%或高于(-?\d+(?:\.\d+)?)%",
        candidate.get("invalidation", ""),
    )
    low, high = (float(entry[1]), float(entry[2])) if entry else (None, None)
    reject_low, reject_high = (
        (float(abandon[1]), float(abandon[2])) if abandon else (None, None)
    )
    close = number(candidate.get("latest_price"), 0)
    return {
        "previous_close": close,
        "limit_price": price_at(close, 10),
        "open_min_pct": low, "open_max_pct": high,
        "reject_min_pct": reject_low, "reject_max_pct": reject_high,
        "auction_low": price_at(close, low) if low is not None else None,
        "auction_high": price_at(close, high) if high is not None else None,
        "position_limit_pct": candidate.get("position_limit_pct"),
        "entry_trigger": candidate.get("entry_trigger"),
        "invalidation": candidate.get("invalidation"),
        "eligible": candidate.get("eligible", True),
        "eligibility_reason": candidate.get("eligibility_reason", "原日报入选候选，仍需通过盘中条件。"),
    }


def normalize_quote(raw, bars, now, plan):
    candles = []
    for bar in bars:
        try:
            stamp = datetime.fromisoformat(str(bar["datetime"])).replace(tzinfo=SHANGHAI)
        except (KeyError, TypeError, ValueError):
            continue
        volume = number(bar.get("vol"), 0)
        close = number(bar.get("close"), 0)
        # 忽略昨日分钟线、尚未到来的占位线与没有成交的占位值。
        if stamp.date() != now.date() or stamp > now or volume < 0.01 or close <= 0:
            continue
        candles.append({
            "time": stamp.isoformat(timespec="seconds"),
            "price": close,
            "open": number(bar.get("open"), close),
            "high": number(bar.get("high"), close),
            "low": number(bar.get("low"), close),
            "close": close,
            "volume": volume,
            "amount": number(bar.get("amount"), 0),
        })
    candles.sort(key=lambda item: item["time"])
    source_time = quote_time(raw.get("servertime"), now)
    price = number(raw.get("price"), 0)
    opening = number(raw.get("open"), 0)
    previous_close = number(raw.get("last_close"), 0)
    reference = plan["previous_close"]
    phase = phase_at(now)
    quote_age = (now - source_time).total_seconds() if source_time else None
    bar_age = (
        (now - datetime.fromisoformat(candles[-1]["time"])).total_seconds()
        if candles else None
    )
    clock_fresh = quote_age is not None and -5 <= quote_age <= 180
    valid_day = bar_age is not None and 0 <= bar_age <= 180
    fresh = clock_fresh and (valid_day if phase in ("morning", "afternoon") else True)
    ratio = None
    if len(candles) >= 6:
        baseline = sum(candle["volume"] for candle in candles[-6:-1]) / 5
        if baseline > 0:
            ratio = round(candles[-1]["volume"] / baseline, 2)
    return {
        "price": price if price > 0 else None,
        "open": opening if opening > 0 else None,
        "high": number(raw.get("high")) or None,
        "low": number(raw.get("low")) or None,
        "previous_close": previous_close or None,
        "change_pct": round((price / reference - 1) * 100, 2) if price > 0 and reference else None,
        "open_change_pct": round((opening / reference - 1) * 100, 4) if opening > 0 and reference else None,
        "amount": number(raw.get("amount")) if number(raw.get("amount"), 0) >= 1 else None,
        "volume": number(raw.get("vol")) if number(raw.get("vol"), 0) >= 0 else None,
        "bid": number(raw.get("bid1")),
        "ask": number(raw.get("ask1")),
        "bid_volume": number(raw.get("bid_vol1")),
        "ask_volume": number(raw.get("ask_vol1")),
        "quote_time": source_time.isoformat(
            timespec="milliseconds" if source_time.microsecond else "seconds"
        ) if source_time else None,
        "quote_age_seconds": quote_age,
        "bar_time": candles[-1]["time"] if candles else None,
        "fresh": fresh, "candles": candles,
        "minute_volume_ratio": ratio,
    }


class MootdxLiveSource:
    """Reuse one quote connection and refresh minute bars on their native cadence."""

    def __init__(self, provider=None, bar_interval=60, clock=None):
        self.provider = provider or MootdxProvider(
            servers=[("117.34.114.14", 7709), ("117.34.114.15", 7709)]
        )
        self.bar_interval = max(1, bar_interval)
        self.clock = clock or time.monotonic
        self.client = None
        self.next_server_index = 0
        self.bars = {}
        self.bars_updated_at = None

    def close(self):
        if self.client is not None:
            self.provider._close(self.client)
            self.client = None

    def _fetch_from_client(self, codes):
        frame = self.client.quotes(symbol=list(codes))
        if frame is None or frame.empty:
            raise RuntimeError("未返回盘口数据")
        rows = {str(row["code"]): row for row in frame.to_dict(orient="records")}
        now = self.clock()
        refresh_bars = (
            self.bars_updated_at is None
            or now - self.bars_updated_at >= self.bar_interval
            or any(code not in self.bars for code in codes)
        )
        if refresh_bars:
            for code in codes:
                try:
                    frame = self.client.bars(symbol=code, frequency=8, offset=240)
                    records = frame.to_dict(orient="records") if frame is not None else []
                    if records or code not in self.bars:
                        self.bars[code] = records
                except Exception:
                    self.bars.setdefault(code, [])
            self.bars_updated_at = now
        return {
            code: (
                {"quote": rows[code], "bars": self.bars.get(code, [])}
                if code in rows else {"error": "未返回该股票盘口"}
            )
            for code in codes
        }

    def fetch(self, codes):
        last_error = None
        if self.client is not None:
            try:
                return self._fetch_from_client(codes)
            except Exception as exc:
                last_error = exc
                self.close()

        start_index = self.next_server_index
        for offset in range(len(self.provider.servers)):
            index = (start_index + offset) % len(self.provider.servers)
            try:
                self.client = self.provider._client(self.provider.servers[index])
                self.next_server_index = (index + 1) % len(self.provider.servers)
                return self._fetch_from_client(codes)
            except Exception as exc:
                last_error = exc
                self.close()
        raise RuntimeError(f"mootdx 行情连接失败：{last_error}")


class IntradayMonitor:
    def __init__(self, report, codes=WATCH_CODES, source=None, interval=None, log_dir=None, clock=None,
                 supplements=None, quote_interval=1, bar_interval=60, idle_interval=60,
                 storage_sink=None):
        self.report = copy.deepcopy(report or {})
        candidates = {
            item["code"]: {
                **item, "origin": "report", "reference_date": self.report.get("as_of"),
                "plan_date": self.report.get("next_session"),
            }
            for item in self.report.get("candidates", [])
        }
        additions = copy.deepcopy(supplements or [])
        # 盘中重筛名单优先展示，原日报股票仍保留在后面追踪。
        self.codes = tuple(dict.fromkeys([*(item["code"] for item in additions), *codes]))
        for item in additions:
            # 同代码优先沿用原日报，避免补充配置覆盖原评分与规则。
            candidates.setdefault(item["code"], item)
        self.candidates = [candidates[code] for code in self.codes if code in candidates]
        if interval is not None:
            quote_interval = interval
        self.quote_interval = max(1, quote_interval)
        self.bar_interval = max(1, bar_interval)
        self.idle_interval = max(self.quote_interval, idle_interval)
        self.interval = self.quote_interval
        self.source = source or MootdxLiveSource(bar_interval=self.bar_interval)
        self.storage_sink = storage_sink
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.clock = clock or now_shanghai
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.events = deque(maxlen=40)
        self.data: Dict[str, Any] = {
            "revision": 0, "stocks": [], "collected_at": None, "last_success_at": None,
            "next_poll_at": None, "error": None, "log_error": None,
            "storage_error": None,
        }

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="mootdx-monitor", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=6)
        close = getattr(self.source, "close", None)
        if close is not None:
            close()
        close_storage = getattr(self.storage_sink, "close", None)
        if close_storage is not None:
            close_storage()

    def _collection_interval(self, now):
        return self.quote_interval if phase_at(now) in ACTIVE_PHASES else self.idle_interval

    def _next_delay(self, now):
        delay = self._collection_interval(now)
        boundary = None
        phase = phase_at(now)
        if phase == "pre":
            boundary = now.replace(hour=9, minute=15, second=0, microsecond=0)
        elif phase == "lunch":
            boundary = now.replace(hour=13, minute=0, second=0, microsecond=0)
        if boundary is not None and boundary > now:
            delay = min(delay, (boundary - now).total_seconds())
        return max(0.1, delay)

    def _run(self):
        while not self.stop_event.is_set():
            started_at = self.clock()
            started = time.monotonic()
            self.poll_once()
            delay = self._next_delay(started_at) - (time.monotonic() - started)
            self.stop_event.wait(max(0.05, delay))

    def poll_once(self):
        started = self.clock()
        next_poll = (started + timedelta(seconds=self._next_delay(started))).isoformat(
            timespec="seconds"
        )
        try:
            if len(self.candidates) != len(self.codes):
                missing = sorted(set(self.codes) - {item["code"] for item in self.candidates})
                raise RuntimeError(f"日报和补充清单未包含监控股票：{', '.join(missing)}，请检查 --watch-date")
            batch = self.source.fetch(self.codes)
            now = self.clock()
            stocks = []
            for candidate in self.candidates:
                code = candidate["code"]
                item = batch.get(code, {})
                plan = plan_for(candidate)
                quote = normalize_quote(item.get("quote", {}), item.get("bars", []), now, plan)
                decision = evaluate(quote, plan, now, candidate.get("plan_date"))
                if item.get("error") or not item.get("quote"):
                    decision = advice("unavailable", "行情读取失败", item.get("error", "没有该股票数据"), "risk")
                stocks.append({
                    "code": code, "name": candidate["name"], "industry": candidate.get("industry"),
                    "score": candidate.get("score"), "plan": plan, "quote": quote, "advice": decision,
                    "origin": candidate["origin"], "reference_date": candidate.get("reference_date"),
                    "plan_date": candidate.get("plan_date"), "verified_at": candidate.get("verified_at"),
                })
            with self.lock:
                old = {stock["code"]: stock["advice"]["state"] for stock in self.data["stocks"]}
                for stock in stocks:
                    if old.get(stock["code"]) != stock["advice"]["state"]:
                        self.events.appendleft({
                            "time": now.isoformat(timespec="seconds"),
                            "name": stock["name"], "code": stock["code"],
                            **stock["advice"],
                        })
                self.data.update(
                    stocks=stocks, collected_at=now.isoformat(timespec="seconds"),
                    last_success_at=now.isoformat(timespec="seconds"),
                    next_poll_at=next_poll, error=None, revision=self.data["revision"] + 1,
                )
        except Exception as exc:
            with self.lock:
                self.data.update(
                    error=str(exc), next_poll_at=next_poll,
                    revision=self.data["revision"] + 1,
                )
        record = self.snapshot()
        if self.log_dir is not None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                log_record = copy.deepcopy(record)
                # 日志保留各次报价与判断；分钟曲线可由行情源重新读取。
                for stock in log_record["stocks"]:
                    stock["quote"].pop("candles", None)
                path = self.log_dir / f"{self.clock().date().isoformat()}.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(log_record, ensure_ascii=False, allow_nan=False) + "\n")
                with self.lock:
                    self.data["log_error"] = None
            except OSError as exc:
                with self.lock:
                    self.data["log_error"] = str(exc)
        if self.storage_sink is not None:
            try:
                self.storage_sink.submit(record)
                with self.lock:
                    self.data["storage_error"] = None
            except Exception as exc:
                with self.lock:
                    self.data["storage_error"] = str(exc)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            data = copy.deepcopy(self.data)
            events = list(self.events)
        collected = data["collected_at"]
        age = (now - datetime.fromisoformat(collected)).total_seconds() if collected else None
        phase = phase_at(now)
        active_interval = self._collection_interval(now)
        delayed = age is not None and age > active_interval + max(5, active_interval / 2)
        data.update(
            server_time=now.isoformat(timespec="seconds"),
            interval_seconds=active_interval,
            active_interval_seconds=active_interval,
            quote_interval_seconds=self.quote_interval,
            bar_interval_seconds=self.bar_interval,
            idle_interval_seconds=self.idle_interval,
            phase=phase, phase_label=PHASE_LABELS[phase],
            report_date=self.report.get("as_of"), plan_date=self.report.get("next_session"),
            source="mootdx", age_seconds=round(age, 1) if age is not None else None,
            delayed=delayed, events=events, requested_codes=list(self.codes),
            watchlist=[{"code": item["code"], "name": item["name"]} for item in self.candidates],
        )
        if self.storage_sink is not None:
            data["storage"] = dict(self.storage_sink.status())
            if data.get("storage_error"):
                data["storage"]["submit_error"] = data["storage_error"]
        for stock in data["stocks"]:
            if data["error"] or delayed:
                stock["advice"] = advice(
                    "stale", "更新中断 · 暂停判断",
                    "保留最后一次报价供核对；等待行情恢复，不使用旧判断入场。", "risk",
                )
                stock["quote"]["fresh"] = False
            elif stock["advice"]["state"] != "unavailable":
                quote = stock["quote"]
                try:
                    stamp = datetime.fromisoformat(quote["quote_time"])
                except (TypeError, ValueError):
                    stamp = None
                quote_age = (now - stamp).total_seconds() if stamp else None
                bar_age = (
                    (now - datetime.fromisoformat(quote["bar_time"])).total_seconds()
                    if quote["bar_time"] else None
                )
                quote["quote_age_seconds"] = quote_age
                quote["fresh"] = (
                    quote_age is not None and -5 <= quote_age <= 180
                    and (phase not in ("morning", "afternoon") or
                         (bar_age is not None and 0 <= bar_age <= 180))
                )
                stock["advice"] = evaluate(quote, stock["plan"], now, stock["plan_date"])
        return data
