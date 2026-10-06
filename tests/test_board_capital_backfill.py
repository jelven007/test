from __future__ import annotations

import unittest
from datetime import date

from banxia_strategy.board_capital_backfill import (
    BoardCapitalBackfill,
    CapitalResolver,
)


class Row:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return dict(self.value)


class Iloc:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, index):
        return Row(self.rows[index])


class Frame:
    def __init__(self, rows):
        self.rows = rows
        self.iloc = Iloc(rows)

    @property
    def empty(self):
        return not self.rows

    def to_dict(self, orient):
        assert orient == "records"
        return list(self.rows)


class Client:
    def finance(self, symbol):
        return Frame(
            [{
                "code": symbol,
                "zongguben": 100,
                "liutongguben": 80,
            }]
        )

    def xdxr(self, symbol):
        return Frame([])

    def close(self):
        pass


class Provider:
    workers = 1
    servers = (("server", 7709),)

    def securities(self):
        return [
            {
                "symbol": "600001",
                "name": "沪市样本",
                "market": "sh",
                "board": "main",
            },
            {
                "symbol": "300001",
                "name": "创业样本",
                "market": "sz",
                "board": "gem",
            },
        ]

    def _client(self, _server):
        return Client()

    @staticmethod
    def _close(client):
        client.close()


class Store:
    def __init__(self):
        self.rows = []
        self.sessions = [
            date(2026, 9, 28),
            date(2026, 9, 29),
            date(2026, 9, 30),
        ]

    def get_history_dates(self, **_kwargs):
        return self.sessions

    def get_history_bars_for_symbols(self, symbols, **_kwargs):
        values = {
            "600001": [
                {"trade_date": self.sessions[0], "close": 10},
                {"trade_date": self.sessions[2], "close": 12},
            ],
            "300001": [
                {"trade_date": self.sessions[1], "close": 20},
            ],
        }
        return {symbol: values[symbol] for symbol in symbols}

    def upsert_board_capital_history(self, rows):
        self.rows = list(rows)


class CapitalResolverTest(unittest.TestCase):
    def test_uses_next_event_before_capital_and_after_capital_on_event_date(self):
        resolver = CapitalResolver(
            {"zongguben": 999, "liutongguben": 888},
            [{
                "year": 2026,
                "month": 9,
                "day": 29,
                "qianzongguben": 10,
                "panqianliutong": 8,
                "houzongguben": 20,
                "panhouliutong": 18,
            }],
        )

        self.assertEqual(
            resolver.resolve(date(2026, 9, 28)),
            (100_000, 80_000, False),
        )
        self.assertEqual(
            resolver.resolve(date(2026, 9, 29)),
            (200_000, 180_000, False),
        )


class BoardCapitalBackfillTest(unittest.TestCase):
    def test_aggregates_four_boards_and_carries_suspended_close(self):
        store = Store()
        result = BoardCapitalBackfill(
            provider=Provider(),
            store=store,
            start_date=date(2026, 9, 28),
            end_date=date(2026, 9, 30),
            batch_size=2,
        ).run()

        self.assertEqual(result.completed_count, 2)
        self.assertEqual(result.daily_row_count, 12)
        indexed = {
            (row["trade_date"], row["board_code"]): row
            for row in store.rows
        }
        september_29 = indexed[(date(2026, 9, 29), "sh_main")]
        self.assertEqual(september_29["total_market_cap_cny"], 1000)
        self.assertEqual(september_29["float_market_cap_cny"], 800)
        self.assertEqual(september_29["stock_count"], 1)
        self.assertEqual(september_29["estimated_stock_count"], 1)
        september_30 = indexed[(date(2026, 9, 30), "sz_gem")]
        self.assertEqual(september_30["total_market_cap_cny"], 2000)
        self.assertEqual(september_30["float_market_cap_cny"], 1600)


if __name__ == "__main__":
    unittest.main()
