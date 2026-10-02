import copy
import json
import unittest
from pathlib import Path

from banxia_strategy.t1_research import (
    buy_proxy, cash_paid, minute_label, minute_quality, net_return, sell_proxy,
    split_name, summary, target_quote, wilson,
)


PROTOCOL = json.loads((Path(__file__).parents[1] / "docs/research/2026-limit-up-protocol.json").read_text())
MODEL = PROTOCOL["execution"]
EVENT = {"close": 10, "board": "main"}


def minutes(price=10.2, volume=10000):
    return {"prices": [price] * 240, "volumes": [volume] * 240}


class ChronologicalResearchTest(unittest.TestCase):
    def test_signal_precedes_fill_and_late_day_breach_does_not_change_buy(self):
        data = minutes()
        initial = buy_proxy(EVENT, {"open": 10.2}, data, "early", MODEL)
        self.assertEqual((initial["signal_time"], initial["buy_time"]), ("09:31", "09:32"))
        data["prices"][100:] = [9] * 140
        self.assertEqual(buy_proxy(EVENT, {"open": 10.2}, data, "early", MODEL), initial)

    def test_pre_fill_breach_cancels_permanently(self):
        data = minutes()
        data["prices"][1] = 9.99
        result = buy_proxy(EVENT, {"open": 10.2}, data, "early", MODEL)
        self.assertEqual(result["buy_status"], "observed_breach_before_fill")

    def test_one_price_limit_and_small_volume_are_not_fills(self):
        for data in (minutes(11), minutes(volume=1)):
            self.assertEqual(buy_proxy(EVENT, {"open": 10.2}, data, "early", MODEL)["buy_status"],
                             "no_pre10_fill_proxy")

    def test_ten_oclock_is_excluded(self):
        data = minutes(11)
        data["prices"][29] = 10.2
        self.assertEqual(minute_label(29), "10:00")
        self.assertNotEqual(buy_proxy(EVENT, {"open": 10.2}, data, "early", MODEL)["buy_status"],
                            "filled_proxy")
        data["prices"][28] = 10.2
        self.assertEqual(buy_proxy(EVENT, {"open": 10.2}, data, "early", MODEL)["buy_time"], "09:59")

    def test_breakout_cannot_fill_same_observation(self):
        data = minutes()
        data["prices"][1:3] = [10.3, 10.4]
        fill = buy_proxy(EVENT, {"open": 10.2}, data, "breakout", MODEL)
        self.assertEqual((fill["signal_time"], fill["buy_time"]), ("09:32", "09:33"))
        self.assertGreater(fill["buy_price"], 10.4)

    def test_reclaim_requires_prior_dip_above_reference(self):
        data = minutes()
        data["prices"][:4] = [10.2, 10.1, 10.21, 10.25]
        fill = buy_proxy(EVENT, {"open": 10.2}, data, "reclaim", MODEL)
        self.assertEqual((fill["signal_time"], fill["buy_time"]), ("09:33", "09:34"))

    def test_quantity_and_budget_known_before_future_fill(self):
        a, b = minutes(10.2), minutes(10.2)
        b["prices"][1] = 10.7
        fills = [buy_proxy(EVENT, {"open": 10.2}, data, "early", MODEL) for data in (a, b)]
        self.assertEqual(fills[0]["shares"], fills[1]["shares"])
        for fill in fills:
            self.assertLessEqual(cash_paid(fill["buy_price"], fill["shares"], MODEL), 10000)

    def test_target_is_net_of_minimum_commission_and_stamp_tax(self):
        target = target_quote(10.2, 900, MODEL)
        self.assertGreaterEqual(net_return(10.2, target, 900, MODEL), 5.1)
        self.assertLess(net_return(10.2, target - 0.01, 900, MODEL), 5.1)

    def test_star_market_requires_two_hundred_shares(self):
        event = {"close": 60, "board": "star"}
        result = buy_proxy(event, {"open": 61}, minutes(61), "early", MODEL)
        self.assertEqual(result["buy_status"], "below_one_lot")
        event["board"] = "main"
        self.assertEqual(buy_proxy(event, {"open": 61}, minutes(61), "early", MODEL)["shares"], 100)

    def test_sell_at_preset_quote_not_future_high(self):
        buy = {"buy_price": 10.2, "shares": 900}
        data = minutes(10.3)
        data["prices"][10] = 11.5
        result = sell_proxy(buy, {"close": 10.8}, {}, data, "main", "target", MODEL)
        self.assertEqual(result["sell_price"], target_quote(10.2, 900, MODEL))
        self.assertEqual(result["sell_time"], "09:41")
        self.assertLess(result["net_return_pct"], 5.3)

    def test_limit_down_exit_is_failure_in_denominator(self):
        sale = sell_proxy({"buy_price": 10.2, "shares": 900}, {"close": 10}, {},
                          minutes(9), "main", "target", MODEL)
        self.assertEqual(sale["sell_status"], "unfilled_d3")
        rows = [
            {"buy_status": "filled_proxy", "buy_date": "2026-01-06", "target_net_return_pct": 6},
            {"buy_status": "filled_proxy", "buy_date": "2026-01-06"},
        ]
        stats = summary(rows, "target")
        self.assertEqual((stats["buys"], stats["successes"], stats["success_rate"]), (2, 1, .5))
        self.assertEqual(stats["unknown_or_unfilled_exits"], 1)

    def test_auction_mixed_volume_cannot_prove_target_fill(self):
        data = minutes(10.2)
        data["prices"][0] = 11.5
        result = sell_proxy({"buy_price": 10.2, "shares": 900}, {"close": 10.2}, {},
                            data, "main", "target", MODEL)
        self.assertEqual(result["sell_time"], "14:56")
        self.assertLess(result["net_return_pct"], 5)

    def test_target_failure_flattens_after_submission_not_at_high(self):
        data = minutes(10.3)
        data["prices"][234:237] = [10.4, 10.1, 10]
        result = sell_proxy({"buy_price": 10.2, "shares": 900}, {"close": 10.2}, {},
                            data, "main", "target", MODEL)
        self.assertEqual(result["sell_time"], "14:56")
        self.assertLess(result["net_return_pct"], 0)

    def test_quality_checks_range_close_and_volume(self):
        data = minutes()
        bar = {"close": 10.2, "low": 10, "high": 10.3, "vol": 2400000}
        self.assertEqual(minute_quality(data, bar), "ok")
        wrong = copy.deepcopy(data)
        wrong["prices"][10] = 11
        self.assertEqual(minute_quality(wrong, bar), "daily_range_mismatch")
        wrong = copy.deepcopy(data)
        wrong["volumes"] = [1] * 240
        self.assertEqual(minute_quality(wrong, bar), "volume_mismatch")

    def test_split_purges_cross_boundary_returns(self):
        self.assertEqual(split_name({"buy_date": "2026-06-30", "sell_date": "2026-07-01"},
                                    PROTOCOL), "purged")
        self.assertEqual(split_name({"buy_date": "2026-09-01", "sell_date": "2026-09-02"},
                                    PROTOCOL), "holdout")

    def test_small_winning_sample_is_not_eighty_percent_evidence(self):
        self.assertLess(wilson(8, 10)[0], .8)


if __name__ == "__main__":
    unittest.main()
