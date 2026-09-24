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
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from .mootdx_provider import MootdxProvider


SHANGHAI = ZoneInfo("Asia/Shanghai")
WATCH_CODES = ("002635", "603328", "002849")
PHASE_LABELS = {
    "pre": "盘前等待", "auction": "集合竞价", "pause": "竞价结束，等待开盘",
    "morning": "早盘交易", "lunch": "午间休市", "afternoon": "午盘交易",
    "closed": "已收盘", "weekend": "休市",
}


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


def phase_at(now):
    if now.weekday() >= 5:
        return "weekend"
    minute = now.hour * 60 + now.minute
    for end, phase in [
        (555, "pre"), (565, "auction"), (570, "pause"), (690, "morning"),
        (780, "lunch"), (900, "afternoon"),
    ]:
        if minute < end:
            return phase
    return "closed"


def quote_time(raw, now):
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?", str(raw or ""))
    if not match:
        return None
    try:
        return now.replace(
            hour=int(match[1]), minute=int(match[2]), second=int(match[3]), microsecond=0
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
            "price": close, "volume": volume,
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
        "bid": number(raw.get("bid1")),
        "ask": number(raw.get("ask1")),
        "bid_volume": number(raw.get("bid_vol1")),
        "quote_time": source_time.isoformat(timespec="seconds") if source_time else None,
        "quote_age_seconds": quote_age,
        "bar_time": candles[-1]["time"] if candles else None,
        "fresh": fresh, "candles": candles,
        "minute_volume_ratio": ratio,
    }


def advice(state, label, reason, tone="neutral"):
    return {"state": state, "label": label, "reason": reason, "tone": tone}


def evaluate(quote, plan, now, plan_date):
    """仅对尚未买入的观察仓位判断；缺少确认条件时不生成买入指令。"""
    phase = phase_at(now)
    if plan_date != now.date().isoformat():
        return advice("expired", "计划日期不匹配", "当前行情不属于这份次日计划，暂停入场判断。", "muted")
    if plan.get("eligible") is False:
        return advice("ineligible", "不参与 · 静态门槛未通过", plan["eligibility_reason"], "muted")
    if phase in ("pre", "weekend"):
        return advice("pre", "等待竞价", "尚无可执行的竞价结果；当前不挂买单。")
    if phase in ("lunch", "closed"):
        return advice("closed", "休市观察", "原策略10:00前入场窗口已结束，不新增接力仓位。", "muted")
    if not quote["fresh"]:
        return advice("stale", "行情待同步", "行情时间过旧、当日分钟线缺失或时间无法核验，暂停判断。", "risk")
    if not quote["price"]:
        return advice("no_quote", "暂无有效报价", "零价格不是跌停或买点；等待有效成交或竞价报价。")
    if quote["previous_close"] is None or abs(quote["previous_close"] - plan["previous_close"]) > 0.011:
        return advice("reference_changed", "昨收基准变化", "盘口昨收与日报不一致，可能涉及除权或数据异常，停止沿用旧价位。", "risk")
    if any(plan[key] is None for key in ("open_min_pct", "open_max_pct", "reject_min_pct", "reject_max_pct")):
        return advice("missing_rules", "入场参数缺失", "日报中缺少可识别的竞价条件，不能自动判断。", "risk")
    if phase in ("auction", "pause"):
        return advice("auction", "等待开盘确认", "竞价报价仍可能变化；以最终开盘涨幅检查条件，不在此阶段给出买入确认。")
    opening_pct = quote["open_change_pct"]
    if opening_pct is None:
        return advice("no_open", "等待有效开盘价", "缺少今日开盘价，无法验证竞价筛选条件。")
    if opening_pct < plan["reject_min_pct"] or opening_pct > plan["reject_max_pct"]:
        return advice("reject_open", "放弃 · 开盘超限", f"开盘涨幅{opening_pct:+.2f}%越过原策略放弃阈值。", "risk")
    if quote["low"] and quote["low"] < plan["previous_close"] - 0.001:
        return advice("reject_low", "放弃 · 跌破昨收", "当日最低价已跌破昨收，保守执行放弃条件，不因随后反弹恢复入场。", "risk")
    if not plan["open_min_pct"] <= opening_pct <= plan["open_max_pct"]:
        return advice("outside_open", "不参与 · 竞价未通过", f"开盘涨幅{opening_pct:+.2f}%不在原策略合格区间内。", "muted")
    if now.hour >= 10:
        return advice("window_closed", "不追 · 入场窗口结束", "已到10:00或之后，按原计划不新增一进二仓位。", "muted")
    limit = plan["limit_price"]
    if quote["price"] >= limit - 0.001:
        sealed = (
            quote["bid"] is not None and quote["bid"] >= limit - 0.001
            and quote["ask"] == 0 and (quote["bid_volume"] or 0) > 0
        )
        if sealed:
            return advice(
                "sealed", "封板快照 · 待核验",
                "已触及参考涨停价且当前买一有封单；仍需核验板块助攻、量能和回封过程，不能据此自动买入。",
                "focus",
            )
        return advice("at_limit", "触板 · 等待封稳", "已触及参考涨停价，盘口尚未确认封住；不提前认定二板成功。", "focus")
    if quote["price"] >= limit * 0.99:
        return advice("near_limit", "临近二板 · 等待确认", "距离参考涨停价不足1%；观察封板与板块配合，不把接近涨停当作买点。", "focus")
    return advice("watch", "观察 · 不提前买", "竞价条件通过，但尚未形成二板确认；继续等10:00前的封板或首次快速回封。")


class MootdxLiveSource:
    def fetch(self, codes):
        provider = MootdxProvider(servers=[("117.34.114.14", 7709), ("117.34.114.15", 7709)])
        client = None
        for server in provider.servers:
            try:
                client = provider._client(server)
                frame = client.quotes(symbol=list(codes))
                if frame is None or frame.empty:
                    raise RuntimeError("未返回盘口数据")
                rows = {str(row["code"]): row for row in frame.to_dict(orient="records")}
                result = {}
                for code in codes:
                    if code not in rows:
                        result[code] = {"error": "未返回该股票盘口"}
                        continue
                    try:
                        bars = client.bars(symbol=code, frequency=8, offset=240)
                        records = bars.to_dict(orient="records") if bars is not None else []
                        result[code] = {"quote": rows[code], "bars": records}
                    except Exception:
                        result[code] = {"quote": rows[code], "bars": []}
                return result
            except Exception as exc:
                last_error = exc
            finally:
                if client is not None:
                    provider._close(client)
        raise RuntimeError(f"mootdx 行情连接失败：{last_error}")


class IntradayMonitor:
    def __init__(self, report, codes=WATCH_CODES, source=None, interval=60, log_dir=None, clock=None,
                 supplements=None):
        self.report = copy.deepcopy(report or {})
        candidates = {
            item["code"]: {
                **item, "origin": "report", "reference_date": self.report.get("as_of"),
                "plan_date": self.report.get("next_session"),
            }
            for item in self.report.get("candidates", [])
        }
        additions = copy.deepcopy(supplements or [])
        self.codes = tuple(dict.fromkeys([*codes, *(item["code"] for item in additions)]))
        for item in additions:
            # 同代码优先沿用原日报，避免补充配置覆盖原评分与规则。
            candidates.setdefault(item["code"], item)
        self.candidates = [candidates[code] for code in self.codes if code in candidates]
        self.source = source or MootdxLiveSource()
        self.interval = interval
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.clock = clock or now_shanghai
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.events = deque(maxlen=40)
        self.data: Dict[str, Any] = {
            "revision": 0, "stocks": [], "collected_at": None, "last_success_at": None,
            "next_poll_at": None, "error": None, "log_error": None,
        }

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="mootdx-monitor", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.poll_once()
            self.stop_event.wait(max(1, self.interval - (time.monotonic() - started)))

    def poll_once(self):
        started = self.clock()
        next_poll = (started + timedelta(seconds=self.interval)).isoformat(timespec="seconds")
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
        if self.log_dir is not None:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                record = self.snapshot()
                # 日志保留各次报价与判断；分钟曲线可由行情源重新读取。
                for stock in record["stocks"]:
                    stock["quote"].pop("candles", None)
                path = self.log_dir / f"{self.clock().date().isoformat()}.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                with self.lock:
                    self.data["log_error"] = None
            except OSError as exc:
                with self.lock:
                    self.data["log_error"] = str(exc)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            data = copy.deepcopy(self.data)
            events = list(self.events)
        collected = data["collected_at"]
        age = (now - datetime.fromisoformat(collected)).total_seconds() if collected else None
        delayed = age is not None and age > self.interval + 30
        phase = phase_at(now)
        data.update(
            server_time=now.isoformat(timespec="seconds"),
            interval_seconds=self.interval, phase=phase, phase_label=PHASE_LABELS[phase],
            report_date=self.report.get("as_of"), plan_date=self.report.get("next_session"),
            source="mootdx", age_seconds=round(age, 1) if age is not None else None,
            delayed=delayed, events=events, requested_codes=list(self.codes),
            watchlist=[{"code": item["code"], "name": item["name"]} for item in self.candidates],
        )
        for stock in data["stocks"]:
            if data["error"] or delayed:
                stock["advice"] = advice(
                    "stale", "更新中断 · 暂停判断",
                    "保留最后一次报价供核对；等待行情恢复，不使用旧判断入场。", "risk",
                )
                stock["quote"]["fresh"] = False
            elif stock["advice"]["state"] != "unavailable":
                quote = stock["quote"]
                stamp = quote_time(
                    quote["quote_time"][11:19] if quote["quote_time"] else None, now
                )
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
