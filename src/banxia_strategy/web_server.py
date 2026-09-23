from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import unquote, urlparse


REPORT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ReportStore:
    def __init__(self, roots: Iterable[Path]):
        self.roots = [Path(root).expanduser() for root in roots]

    def _report_paths(self) -> Dict[str, Path]:
        reports: Dict[str, tuple[str, Path]] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for path in root.glob("*/candidates.json"):
                try:
                    payload = self._read(path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                as_of = payload.get("as_of")
                if not isinstance(as_of, str) or not REPORT_DATE.fullmatch(as_of):
                    continue
                generated_at = str(payload.get("generated_at", ""))
                current = reports.get(as_of)
                if current is None or generated_at > current[0]:
                    reports[as_of] = (generated_at, path)
        return {as_of: item[1] for as_of, item in reports.items()}

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("report must be a JSON object")
        if not isinstance(payload.get("market"), dict):
            raise ValueError("report market is missing")
        if not isinstance(payload.get("candidates"), list):
            raise ValueError("report candidates are missing")
        return payload

    def list_reports(self) -> List[Dict[str, Any]]:
        result = []
        for as_of, path in sorted(self._report_paths().items(), reverse=True):
            report = self._read(path)
            market = report["market"]
            result.append(
                {
                    "as_of": as_of,
                    "next_session": report.get("next_session"),
                    "generated_at": report.get("generated_at"),
                    "regime": market.get("regime"),
                    "market_score": market.get("score"),
                    "candidate_count": len(report["candidates"]),
                }
            )
        return result

    def get(self, as_of: str) -> Optional[Dict[str, Any]]:
        if not REPORT_DATE.fullmatch(as_of):
            return None
        path = self._report_paths().get(as_of)
        return self._read(path) if path else None

    def latest(self) -> Optional[Dict[str, Any]]:
        paths = self._report_paths()
        if not paths:
            return None
        return self._read(paths[max(paths)])


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_handler(store: ReportStore, static_root: Path):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "BanxiaDashboard/0.1"

        def do_GET(self) -> None:
            path = unquote(urlparse(self.path).path)
            if path == "/api/health":
                self._send_json(
                    {
                        "status": "ok",
                        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                )
                return
            if path == "/api/reports":
                reports = store.list_reports()
                self._send_json({"reports": reports, "count": len(reports)})
                return
            if path == "/api/reports/latest":
                report = store.latest()
                if report is None:
                    self._send_json(
                        {"error": "尚未找到策略日报，请先运行 banxia-strategy run"},
                        HTTPStatus.NOT_FOUND,
                    )
                else:
                    self._send_json(report)
                return
            match = re.fullmatch(r"/api/reports/(\d{4}-\d{2}-\d{2})", path)
            if match:
                report = store.get(match.group(1))
                if report is None:
                    self._send_json({"error": "未找到该交易日的报告"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(report)
                return
            if path == "/":
                path = "/index.html"
            self._send_static(path)

        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, request_path: str) -> None:
            relative = request_path.lstrip("/")
            if not relative or ".." in Path(relative).parts:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target = static_root / relative
            try:
                if not target.is_file() or not target.resolve().is_relative_to(static_root.resolve()):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = target.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type, _ = mimetypes.guess_type(target.name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type or 'application/octet-stream'}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[web] {self.address_string()} {fmt % args}")

    return DashboardHandler


def make_server(
    report_roots: Sequence[Path],
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    static_root = Path(__file__).with_name("web")
    store = ReportStore(report_roots)
    return ThreadingHTTPServer((host, port), make_handler(store, static_root))


def serve_dashboard(
    report_roots: Sequence[Path],
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = make_server(report_roots, host, port)
    actual_host, actual_port = server.server_address[:2]
    print(f"Strategy dashboard: http://{actual_host}:{actual_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
