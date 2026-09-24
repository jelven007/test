from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from banxia_strategy.intraday import WATCH_CODES
from banxia_strategy.web_server import ReportStore, make_server, select_watch_report


def report(as_of: str, generated_at: str, candidate_count: int = 1):
    return {
        "as_of": as_of,
        "next_session": "2026-09-24",
        "generated_at": generated_at,
        "data_source": "test",
        "data_sessions": [as_of],
        "market": {
            "regime": "中性试错",
            "score": 56.3,
            "limit_up_count": 49,
            "broken_board_count": 31,
            "broken_board_data_available": True,
            "break_rate_pct": 38.8,
            "max_board": 4,
        },
        "candidates": [{"code": f"60000{index}"} for index in range(candidate_count)],
        "rejected_count": 5,
        "disclaimer": "test only",
    }


def write_report(root: Path, payload):
    output = root / payload["as_of"]
    output.mkdir(parents=True, exist_ok=True)
    (output / "candidates.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class ReportStoreTest(unittest.TestCase):
    def test_watch_report_selection_uses_latest_dynamic_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = report("2026-09-23", "2026-09-23T16:20:00+08:00")
            baseline["candidates"] = [{"code": code} for code in WATCH_CODES]
            write_report(root, baseline)
            write_report(root, report("2026-09-24", "2026-09-24T16:20:00+08:00"))
            store = ReportStore([root])
            self.assertEqual(select_watch_report(store)["as_of"], "2026-09-24")
            self.assertEqual(select_watch_report(store, "2026-09-24")["as_of"], "2026-09-24")
            self.assertIsNone(select_watch_report(store, "2026-09-20"))

    def test_lists_reports_newest_first_and_loads_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_report(root, report("2026-09-22", "2026-09-22T16:20:00+08:00"))
            write_report(root, report("2026-09-23", "2026-09-23T16:20:00+08:00", 2))

            store = ReportStore([root])

            self.assertEqual(
                [item["as_of"] for item in store.list_reports()],
                ["2026-09-23", "2026-09-22"],
            )
            self.assertEqual(store.latest()["as_of"], "2026-09-23")
            self.assertEqual(store.list_reports()[0]["candidate_count"], 2)

    def test_prefers_newer_duplicate_report_and_skips_invalid_json(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first)
            second_root = Path(second)
            write_report(first_root, report("2026-09-23", "2026-09-23T16:20:00+08:00"))
            write_report(second_root, report("2026-09-23", "2026-09-23T17:20:00+08:00", 3))
            invalid_dir = second_root / "2026-09-22"
            invalid_dir.mkdir()
            (invalid_dir / "candidates.json").write_text("{broken", encoding="utf-8")

            store = ReportStore([first_root, second_root])

            self.assertEqual(len(store.latest()["candidates"]), 3)
            self.assertEqual(len(store.list_reports()), 1)


class DashboardServerTest(unittest.TestCase):
    def test_serves_dashboard_and_report_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_report(root, report("2026-09-23", "2026-09-23T16:20:00+08:00"))
            server = make_server([root], "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base_url}/", timeout=2) as response:
                    html = response.read().decode("utf-8")
                with urlopen(f"{base_url}/api/reports/latest", timeout=2) as response:
                    payload = json.load(response)

                self.assertIn("次日执行台", html)
                self.assertEqual(payload["as_of"], "2026-09-23")
                with self.assertRaises(HTTPError) as context:
                    urlopen(f"{base_url}/api/reports/2026-09-20", timeout=2)
                self.assertEqual(context.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class SharedNavigationTest(unittest.TestCase):
    def test_dashboard_pages_use_the_same_primary_menu(self):
        web_root = Path(__file__).resolve().parents[1] / "src/banxia_strategy/web"
        blocks = []
        for filename in ("index.html", "monitor.html"):
            html = (web_root / filename).read_text(encoding="utf-8")
            match = re.search(
                r'<nav class="primary-nav" aria-label="主导航">(.*?)</nav>',
                html,
                re.DOTALL,
            )
            self.assertIsNotNone(match)
            block = match.group(1).replace(' aria-current="page"', "")
            blocks.append(re.sub(r"\s+", " ", block).strip())
        self.assertEqual(blocks[0], blocks[1])
        self.assertEqual(
            blocks[0],
            '<a href="/">次日计划</a> <a href="/monitor">盘中监控</a>',
        )


if __name__ == "__main__":
    unittest.main()
