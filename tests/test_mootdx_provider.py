from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from banxia_strategy.mootdx_provider import (
    REFERENCE_FILES,
    MootdxProvider,
    _extract_pools,
    _json_value,
    _limit_price,
    _seal_statistics,
    _stock_blocks_from_records,
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
    def test_json_values_remove_postgres_incompatible_nul_characters(self):
        self.assertEqual(
            _json_value(
                {
                    "name\x00": "万 科Ａ\x00",
                    "nested": [b"\xcd\xf2\xbf\xc6\x00"],
                }
            ),
            {
                "name": "万 科Ａ",
                "nested": ["万科"],
            },
        )

    def test_stock_blocks_are_deduplicated_from_raw_blockname_records(self):
        blocks = _stock_blocks_from_records(
            [
                {"blockname": "新能源车", "code": "000001"},
                {"blockname": "新能源车", "code": "000001"},
                {"blockname": "新能源车", "code": "300001"},
                {"blockname": "半导体", "code": "688001"},
                {"blockname": "指数板块", "code": "399001"},
                {"blockname": "", "code": "000001"},
            ],
            {"000001", "300001", "688001"},
        )

        self.assertEqual(
            blocks,
            {
                "半导体": ("688001",),
                "新能源车": ("000001", "300001"),
            },
        )

    def test_all_a_share_universe_keeps_growth_and_star_boards_separate(self):
        class LowLevelClient:
            @staticmethod
            def get_security_list(market, start):
                if start:
                    return []
                return (
                    [
                        {"code": "000001", "name": "上证指数", "pre_close": 3900},
                        {"code": "600001", "name": "沪市主板", "pre_close": 10},
                        {"code": "688001", "name": "科创样本", "pre_close": 20},
                        {"code": "900901", "name": "沪市B股", "pre_close": 1},
                    ]
                    if market == 1
                    else [
                        {"code": "000001", "name": "深市主板", "pre_close": 11},
                        {"code": "300001", "name": "创业样本", "pre_close": 21},
                        {"code": "399001", "name": "深证成指", "pre_close": 12000},
                        {"code": "200001", "name": "深市B股", "pre_close": 2},
                    ]
                )

        class Client:
            client = LowLevelClient()

            @staticmethod
            def stock_count(_market):
                return 4

            @staticmethod
            def close():
                pass

        provider = MootdxProvider(
            servers=[("example", 7709)],
            client_factory=lambda _server: Client(),
        )

        securities = provider.securities()

        self.assertEqual(
            [item["symbol"] for item in securities],
            ["000001", "300001", "600001", "688001"],
        )
        self.assertEqual(
            {item["symbol"]: item["board"] for item in securities},
            {
                "000001": "main",
                "300001": "gem",
                "600001": "main",
                "688001": "star",
            },
        )

    def test_reference_snapshot_fetches_catalog_and_all_files_from_one_node(self):
        contents = {
            filename: f"raw:{filename}".encode()
            for filename in REFERENCE_FILES
        }

        class LowLevelClient:
            @staticmethod
            def get_security_list(market, start):
                if start:
                    return []
                if market == 1:
                    return [
                        {"code": "600001", "name": "沪市主板"},
                        {"code": "000001", "name": "上证指数"},
                    ]
                return [
                    {"code": "000001", "name": "深市主板"},
                    {"code": "399001", "name": "深证成指"},
                ]

            @staticmethod
            def get_block_info_meta(filename):
                return {"size": len(contents[filename])}

            @staticmethod
            def get_block_info(filename, start, size):
                return contents[filename][start : start + size]

        class Client:
            client = LowLevelClient()

            @staticmethod
            def stock_count(_market):
                return 2

            @staticmethod
            def close():
                pass

        provider = MootdxProvider(
            servers=[("example", 7709)],
            client_factory=lambda _server: Client(),
        )

        def blocks(_content, filename):
            return [
                {
                    "blockname": f"{filename}-板块",
                    "code": "600001",
                    "block_type": 1,
                }
            ]

        with patch.object(provider, "_parse_block_records", side_effect=blocks):
            result = provider.reference_snapshot()

        self.assertEqual(result["source_node"], "example:7709")
        self.assertEqual(set(result["files"]), set(REFERENCE_FILES))
        self.assertEqual(
            [item["symbol"] for item in result["securities"]],
            ["000001", "600001"],
        )
        self.assertEqual(
            {item["block_type"] for item in result["memberships"]},
            {"default", "concept", "style", "index"},
        )

    def test_historical_minutes_receive_stable_market_times(self):
        class Frame:
            empty = False

            @staticmethod
            def to_dict(orient):
                return [
                    {"price": 10.1, "vol": 100},
                    {"price": 10.2, "vol": 200},
                ]

        class Client:
            @staticmethod
            def minutes(**_kwargs):
                return Frame()

            @staticmethod
            def close():
                pass

        provider = MootdxProvider(
            servers=[("example", 7709)],
            client_factory=lambda _server: Client(),
        )

        result = provider.historical_minutes(
            "600001",
            date(2026, 9, 24),
        )

        self.assertEqual(result[0]["time"], "2026-09-24T09:31:00+08:00")
        self.assertEqual(result[1]["close"], 10.2)
        self.assertEqual(result[1]["volume"], 200.0)

    def test_kline_bars_use_native_period_and_stop_at_selected_date(self):
        class Frame:
            def to_dict(self, orient):
                self.orient = orient
                return [
                    bar(22, 10.0, 10.4),
                    bar(23, 10.5, 10.8),
                    bar(24, 11.0, 11.2),
                ]

        class Client:
            def __init__(self):
                self.calls = []

            def bars(self, **kwargs):
                self.calls.append(kwargs)
                return Frame()

            def close(self):
                pass

        client = Client()
        provider = MootdxProvider(
            servers=[("example", 7709)],
            client_factory=lambda _server: client,
        )

        result = provider.kline_bars(
            "600001",
            "week",
            date(2026, 9, 23),
            limit=1,
        )

        self.assertEqual(client.calls[0]["frequency"], 5)
        self.assertEqual(client.calls[0]["offset"], 800)
        self.assertEqual(
            result,
            [
                {
                    "date": "2026-09-23",
                    "open": 10.5,
                    "high": 10.8,
                    "low": 10.5,
                    "close": 10.5,
                    "volume": 500000.0,
                    "amount": 500000000.0,
                }
            ],
        )

    def test_kline_bars_reject_invalid_period(self):
        provider = MootdxProvider(
            servers=[("example", 7709)],
            client_factory=lambda _server: None,
        )

        with self.assertRaisesRegex(ValueError, "K线周期"):
            provider.kline_bars(
                "600001",
                "quarter",
                date(2026, 9, 23),
            )

    def test_missing_holiday_data_never_guesses_a_trading_day(self):
        for result in (None, RuntimeError("unavailable")):
            options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with patch("mootdx.utils.holiday._holiday", **options):
                with self.assertRaisesRegex(RuntimeError, "停止推测"):
                    MootdxProvider._is_holiday(date(2026, 9, 25))

    def test_holiday_comparison_normalizes_timestamp_index_to_dates(self):
        import pandas as pd
        calendar = pd.DataFrame({"国家": ["中国"]}, index=pd.to_datetime(["2026-09-25"]))
        with patch("mootdx.utils.holiday._holiday", return_value=calendar):
            self.assertTrue(MootdxProvider._is_holiday(date(2026, 9, 25)))
            self.assertFalse(MootdxProvider._is_holiday(date(2026, 9, 28)))

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
