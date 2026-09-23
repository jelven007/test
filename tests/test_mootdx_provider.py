from __future__ import annotations

import unittest
from datetime import date

from banxia_strategy.mootdx_provider import (
    _extract_pools,
    _limit_price,
    _seal_statistics,
)


def bar(day: int, close: float, high: float, amount: float = 500_000_000):
    return {
        "datetime": f"2026-09-{day:02d} 15:00",
        "open": close,
        "close": close,
        "high": high,
        "low": close,
        "vol": 500_000,
        "amount": amount,
    }


class MootdxCalculationTest(unittest.TestCase):
    def test_limit_price_uses_exchange_rounding(self):
        self.assertEqual(_limit_price(10.01, "普通股份"), 11.01)
        self.assertEqual(_limit_price(10.01, "ST样本"), 10.51)

    def test_extracts_limit_up_broken_board_and_board_height(self):
        histories = {
            "600001": [
                bar(18, 10.00, 10.00),
                bar(21, 11.00, 11.00),
                bar(22, 12.10, 12.10),
                bar(23, 12.00, 13.31),
            ]
        }
        limits, broken = _extract_pools(histories, {"600001": "样本股份"})

        self.assertEqual(limits[date(2026, 9, 21)][0]["连板数"], 1)
        self.assertEqual(limits[date(2026, 9, 22)][0]["连板数"], 2)
        self.assertEqual(broken[date(2026, 9, 23)][0]["代码"], "600001")

    def test_seal_statistics_counts_reopenings(self):
        prices = [10.8, 11.0, 11.0, 10.99, 11.0, 10.98, 11.0]
        first, last, breaks = _seal_statistics(prices, 11.0)

        self.assertEqual(first, "09:32:00")
        self.assertEqual(last, "09:37:00")
        self.assertEqual(breaks, 2)


if __name__ == "__main__":
    unittest.main()
