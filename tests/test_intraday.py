from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen

from banxia_strategy.intraday import (
    SHANGHAI, WATCH_CODES, IntradayMonitor, evaluate, load_watchlist, normalize_quote, phase_at, plan_for,
)
from banxia_strategy.web_server import make_server


def candidate():
    return {
        "code": "002635", "name": "安洁科技", "latest_price": 16.79,
        "score": 89.2, "industry": "5G概念", "position_limit_pct": 20,
        "entry_trigger": "次日竞价涨幅位于0.5%～5.0%；仅在10:00前放量封二板时观察",
        "invalidation": "竞价低于-2%或高于7.0%、开盘快速跌破昨日收盘价则放弃",
    }


def report():
    return {"as_of": "2026-09-23", "next_session": "2026-09-24", "candidates": [candidate()]}


def raw_quote():
    return {
        "price": 17.5, "open": 17.1, "high": 17.6, "low": 17.0,
        "last_close": 16.79, "servertime": "9:45:00.000",
        "bid1": 17.49, "ask1": 17.5, "bid_vol1": 100, "amount": 500000000,
    }


def bars():
    return [
        {"datetime": f"2026-09-24 09:{minute}", "close": 17.5, "vol": 100}
        for minute in range(39, 45)
    ]


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 9, 45, tzinfo=SHANGHAI)
        self.plan = plan_for(candidate())

    def decision(self, raw=None, now=None, minutes=None, plan_date="2026-09-24"):
        now = now or self.now
        quote = normalize_quote(
            raw if raw is not None else raw_quote(),
            minutes if minutes is not None else bars(), now, self.plan,
        )
        return evaluate(quote, self.plan, now, plan_date)

    def test_session_boundaries(self):
        expected = {"09:14": "pre", "09:15": "auction", "09:25": "pause", "09:30": "morning",
                    "11:30": "lunch", "13:00": "afternoon", "15:00": "closed"}
        for clock, phase in expected.items():
            with self.subTest(clock=clock):
                now = datetime.fromisoformat(f"2026-09-24T{clock}:00+08:00")
                self.assertEqual(phase_at(now), phase)

    def test_preopen_zero_is_not_a_trade_or_a_drop(self):
        raw = {key: 0 for key in raw_quote()}
        result = self.decision(raw=raw, now=self.now.replace(hour=9, minute=5))
        self.assertEqual(result["state"], "pre")

    def test_future_placeholders_and_prior_day_bars_are_discarded(self):
        invalid = [
            {"datetime": "2026-09-23 15:00", "close": 16.79, "vol": 1000},
            {"datetime": "2026-09-24 09:46", "close": 17.5, "vol": 1000},
            {"datetime": "2026-09-24 09:44", "close": 17.5, "vol": 5.8e-39},
        ]
        q = normalize_quote(raw_quote(), invalid, self.now, self.plan)
        self.assertEqual(q["candles"], [])
        self.assertFalse(q["fresh"])
        self.assertEqual(self.decision(minutes=invalid)["state"], "stale")

    def test_open_outside_conditions_and_intraday_drop_are_rejected(self):
        for fields, state in [
            ({"open": 18.2}, "reject_open"),
            ({"open": 16.79}, "outside_open"),
            ({"low": 16.78, "price": 18.47}, "reject_low"),
        ]:
            with self.subTest(fields=fields):
                self.assertEqual(self.decision(raw={**raw_quote(), **fields})["state"], state)

    def test_sealed_snapshot_requires_manual_verification(self):
        raw = {**raw_quote(), "price": 18.47, "bid1": 18.47, "ask1": 0}
        self.assertEqual(self.decision(raw=raw)["state"], "sealed")
        self.assertIn("不能据此自动买入", self.decision(raw=raw)["reason"])
        raw["ask1"] = 18.47
        self.assertEqual(self.decision(raw=raw)["state"], "at_limit")

    def test_unknown_ask_cannot_confirm_a_sealed_board(self):
        raw = {**raw_quote(), "price": 18.47, "bid1": 18.47}
        raw.pop("ask1")
        self.assertEqual(self.decision(raw=raw)["state"], "at_limit")

    def test_expired_reference_stale_quote_and_missing_open_stop_judgment(self):
        self.assertEqual(self.decision(plan_date="2026-09-23")["state"], "expired")
        self.assertEqual(self.decision(raw={**raw_quote(), "last_close": 16.0})["state"], "reference_changed")
        self.assertEqual(self.decision(raw={**raw_quote(), "servertime": "9:30:00"})["state"], "stale")
        self.assertEqual(self.decision(raw={**raw_quote(), "open": 0})["state"], "no_open")

    def test_window_closes_at_ten(self):
        now = self.now.replace(hour=10, minute=0)
        raw = {**raw_quote(), "servertime": "10:00:00"}
        minutes = [{"datetime": "2026-09-24 09:59", "close": 17.5, "vol": 100}]
        self.assertEqual(self.decision(raw=raw, now=now, minutes=minutes)["state"], "window_closed")

    def test_volume_reference_and_decimal_rounding(self):
        q = normalize_quote(raw_quote(), bars()[:-1] + [
            {"datetime": "2026-09-24 09:44", "close": 17.5, "vol": 200}
        ], self.now, self.plan)
        self.assertEqual(q["minute_volume_ratio"], 2)
        self.assertEqual(plan_for({**candidate(), "latest_price": 23})["auction_low"], 23.12)


class FakeSource:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def fetch(self, codes):
        self.calls += 1
        if self.fail:
            raise RuntimeError("测试断线")
        return {code: {"quote": raw_quote(), "bars": bars()} for code in codes}


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 9, 45, tzinfo=SHANGHAI)
        self.source = FakeSource()
        self.monitor = IntradayMonitor(
            report(), codes=["002635"], source=self.source, clock=lambda: self.now,
        )

    def test_repeated_http_reads_do_not_refetch_market_data(self):
        self.monitor.poll_once()
        with tempfile.TemporaryDirectory() as directory:
            server = make_server([Path(directory)], port=0, monitor=self.monitor)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                for _ in range(3):
                    with urlopen(base + "/api/monitor", timeout=2) as response:
                        self.assertEqual(json.load(response)["stocks"][0]["advice"]["state"], "watch")
                with urlopen(base + "/monitor", timeout=2) as response:
                    self.assertIn("盘中监控", response.read().decode())
                self.assertEqual(self.source.calls, 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_failure_retains_last_quote_but_suppresses_advice_and_recovers(self):
        self.monitor.poll_once()
        self.source.fail = True
        self.monitor.poll_once()
        state = self.monitor.snapshot()
        self.assertEqual(state["stocks"][0]["quote"]["price"], 17.5)
        self.assertEqual(state["stocks"][0]["advice"]["state"], "stale")
        self.assertEqual(state["error"], "测试断线")
        self.source.fail = False
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "watch")

    def test_elapsed_time_invalidates_cached_advice(self):
        self.monitor.poll_once()
        self.now += timedelta(seconds=91)
        self.assertTrue(self.monitor.snapshot()["delayed"])
        self.assertEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "stale")

    def test_market_phase_changes_between_two_polls(self):
        self.now = self.now.replace(minute=14, second=50)
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "pre")
        self.now += timedelta(seconds=15)
        self.assertEqual(self.monitor.snapshot()["phase"], "auction")
        self.assertNotEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "pre")

    def test_quote_becomes_stale_before_next_collection(self):
        self.now = self.now.replace(minute=47, second=40)
        self.source.fetch = lambda codes: {
            "002635": {"quote": raw_quote(), "bars": [
                {"datetime": "2026-09-24 09:47", "close": 17.5, "vol": 100}
            ]}
        }
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "watch")
        self.now += timedelta(seconds=30)
        self.assertEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "stale")

    def test_events_change_only_on_decision_changes_and_snapshot_is_isolated(self):
        self.monitor.poll_once()
        self.monitor.poll_once()
        first = self.monitor.snapshot()
        self.assertEqual(len(first["events"]), 1)
        first["stocks"][0]["advice"]["state"] = "changed"
        self.assertEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "watch")

    def test_partial_missing_quote_and_missing_candidate_do_not_look_valid(self):
        self.source.fetch = lambda codes: {}
        self.monitor.poll_once()
        self.assertEqual(self.monitor.snapshot()["stocks"][0]["advice"]["state"], "unavailable")
        missing = IntradayMonitor(report(), codes=["603328"], source=self.source)
        missing.poll_once()
        self.assertIn("未包含", missing.snapshot()["error"])

    def test_poller_runs_without_http_requests_and_stops(self):
        updated = threading.Event()
        source = FakeSource()
        original_fetch = source.fetch

        def fetch(codes):
            result = original_fetch(codes)
            updated.set()
            return result

        source.fetch = fetch
        monitor = IntradayMonitor(report(), codes=["002635"], source=source)
        monitor.start()
        try:
            self.assertTrue(updated.wait(timeout=2))
        finally:
            monitor.stop()
        self.assertFalse(monitor.thread.is_alive())
        self.assertEqual(source.calls, 1)

    def test_poll_log_persists_quotes_and_rules_without_curve_duplication(self):
        with tempfile.TemporaryDirectory() as directory:
            self.monitor.log_dir = Path(directory)
            self.monitor.poll_once()
            path = Path(directory) / "2026-09-24.jsonl"
            data = json.loads(path.read_text())
            self.assertEqual(data["stocks"][0]["quote"]["price"], 17.5)
            self.assertNotIn("candles", data["stocks"][0]["quote"])
            self.assertTrue(self.monitor.snapshot()["stocks"][0]["quote"]["candles"])


class SupplementalWatchlistTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 9, 45, tzinfo=SHANGHAI)
        self.additions = load_watchlist(
            Path(__file__).resolve().parents[1] / "config/monitor_watchlist.json"
        )
        self.report = {
            **report(), "candidates": [{**candidate(), "code": code} for code in WATCH_CODES],
        }

    def monitor(self, additions=None):
        additions = self.additions if additions is None else additions
        records = self.report["candidates"] + additions
        quotes = {}
        for item in records:
            close = item["latest_price"]
            limit = plan_for(item)["limit_price"]
            quotes[item["code"]] = {
                "quote": {
                    **raw_quote(), "price": limit, "open": close * 1.02, "low": close,
                    "high": limit, "last_close": close, "bid1": limit, "ask1": 0,
                },
                "bars": [{**bar, "close": limit} for bar in bars()],
            }
        source = FakeSource()
        source.fetch = lambda codes: {code: quotes[code] for code in codes}
        return IntradayMonitor(
            self.report, supplements=additions, source=source, clock=lambda: self.now,
        )

    def test_eight_stocks_keep_original_scores_and_supplement_provenance(self):
        original = json.dumps(self.report)
        monitor = self.monitor()
        monitor.poll_once()
        state = monitor.snapshot()
        self.assertIsNone(state["error"])
        self.assertEqual(state["requested_codes"], [
            *WATCH_CODES, "002909", "002819", "002119", "603396", "600293",
        ])
        self.assertEqual(len(state["stocks"]), 8)
        self.assertEqual(len(state["watchlist"]), 8)
        self.assertEqual(state["interval_seconds"], 60)
        self.assertEqual(state["stocks"][0]["score"], candidate()["score"])
        for stock in state["stocks"][3:]:
            self.assertIsNone(stock["score"])
            self.assertEqual(stock["origin"], "supplement")
            self.assertEqual(stock["reference_date"], "2026-09-23")
        self.assertEqual(json.dumps(self.report), original)

    def test_static_rejection_survives_sealed_quote_and_snapshot_reevaluation(self):
        monitor = self.monitor()
        monitor.poll_once()
        for _ in range(2):
            stocks = {stock["code"]: stock for stock in monitor.snapshot()["stocks"]}
            self.assertEqual(stocks["002909"]["advice"]["state"], "sealed")
            rejected = stocks["603396"]
            self.assertEqual(rejected["advice"]["state"], "ineligible")
            self.assertIn("2亿元", rejected["advice"]["reason"])
            self.assertEqual(rejected["plan"]["position_limit_pct"], 0)
        monitor.source.fetch = lambda codes: (_ for _ in ()).throw(RuntimeError("测试断线"))
        monitor.poll_once()
        rejected = next(s for s in monitor.snapshot()["stocks"] if s["code"] == "603396")
        self.assertFalse(rejected["plan"]["eligible"])
        self.assertIn("2亿元", rejected["plan"]["eligibility_reason"])

    def test_supplement_plan_date_is_independent_from_report(self):
        self.additions[0]["plan_date"] = "2026-09-25"
        monitor = self.monitor()
        monitor.poll_once()
        stocks = {stock["code"]: stock for stock in monitor.snapshot()["stocks"]}
        self.assertEqual(stocks["002635"]["advice"]["state"], "sealed")
        self.assertEqual(stocks["002909"]["advice"]["state"], "expired")

    def test_duplicate_supplement_keeps_report_rules_and_single_row(self):
        duplicate = {**self.additions[0], "code": WATCH_CODES[0], "eligible": False}
        monitor = self.monitor([duplicate])
        self.assertEqual(len(monitor.codes), 3)
        self.assertEqual(monitor.candidates[0]["score"], candidate()["score"])
        self.assertEqual(monitor.candidates[0]["latest_price"], candidate()["latest_price"])
        self.assertEqual(monitor.candidates[0]["origin"], "report")

    def test_missing_eligibility_or_duplicate_code_is_rejected(self):
        for edits in ("missing_eligibility", "duplicate", "invalid_close", "invalid_date"):
            with self.subTest(edits=edits), tempfile.TemporaryDirectory() as directory:
                payload = {
                    "as_of": "2026-09-23", "next_session": "2026-09-24",
                    "data_source": "mootdx", "candidates": [dict(self.additions[0])],
                }
                if edits == "missing_eligibility":
                    payload["candidates"][0].pop("eligible")
                elif edits == "duplicate":
                    payload["candidates"].append(dict(self.additions[0]))
                elif edits == "invalid_close":
                    payload["candidates"][0]["latest_price"] = 0
                else:
                    payload["next_session"] = "2026-09-22"
                path = Path(directory) / "watchlist.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_watchlist(path)


if __name__ == "__main__":
    unittest.main()
