from __future__ import annotations

import json
import mimetypes
import re
import shutil
import sys
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import unquote, urlparse

from .application.persistence import build_market_persistence
from .intraday import IntradayMonitor, load_watchlist
from .research_files import LocalResearchStore
from .storage_config import StorageSettings
from .strategy_config import ConfigConflict, ConfigError, StrategyConfigStore


REPORT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ReportStore:
    def __init__(self, roots: Iterable[Path]):
        self.roots = [Path(root).expanduser() for root in roots]

    def _report_paths(self) -> Dict[str, Path]:
        reports: Dict[str, tuple[str, Path]] = {}
        for root in self.roots:
            if not root.exists():
                continue
            paths = (
                *root.glob("*/candidates.json"),
                *root.glob("strategies/*/*/candidates.json"),
            )
            for path in paths:
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
                    "strategy_version": report.get("strategy_version"),
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

    def delete_strategy(self, strategy_id: str) -> int:
        deleted = 0
        for root in self.roots:
            directory = root / "strategies" / strategy_id
            if directory.is_dir():
                shutil.rmtree(directory)
                deleted += 1
        return deleted


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def make_handler(store: ReportStore, static_root: Path, monitor=None, config_store=None, research_store=None):
    config_store = config_store or StrategyConfigStore(Path("config/strategy.json"))
    research_store = research_store or LocalResearchStore()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "BanxiaDashboard/0.1"

        def do_PUT(self) -> None:
            if urlparse(self.path).path != "/api/v1/strategy-config":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("参数内容长度不正确")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {"config", "revision"}:
                    raise ValueError("请求必须包含 config 和 revision")
                self._send_json(config_store.save(payload["config"], payload["revision"]))
            except (ValueError, OSError) as exc:
                status = 409 if isinstance(exc, ConfigConflict) else 503 if isinstance(exc, OSError) else 422
                self._send_json({"error": {"message": str(exc), "details": {
                    "fields": exc.errors if isinstance(exc, ConfigError) else {},
                }}}, status)

        def do_GET(self) -> None:
            path = unquote(urlparse(self.path).path)
            if path == "/api/v1/research":
                self._send_json({"items": research_store.list_research_runs()})
                return
            if path.startswith("/api/v1/research/"):
                self._send_research(path)
                return
            if path == "/api/v1/strategy-config":
                try:
                    self._send_json(config_store.payload())
                except (ValueError, OSError) as exc:
                    self._send_json({"error": {"message": str(exc)}}, 503)
                return
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
            if path == "/api/monitor":
                if monitor is None:
                    self._send_json({"error": "当日实盘未启动"}, HTTPStatus.SERVICE_UNAVAILABLE)
                else:
                    self._send_json(monitor.snapshot())
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
                path = "/stocks.html"
            elif path in ("/plan", "/plan/"):
                path = "/index.html"
            elif path in ("/monitor", "/monitor/"):
                path = "/monitor.html"
            elif path in ("/strategy", "/strategy/"):
                path = "/strategy.html"
            elif path in ("/research", "/research/"):
                path = "/research.html"
            elif path in ("/stocks", "/stocks/"):
                path = "/stocks.html"
            self._send_static(path)

        def _send_research(self, path):
            parts = path.removeprefix("/api/v1/research/").split("/")
            try:
                payload = research_store.get_research_run(parts[0])
            except ValueError:
                self._send_json({"error": {"message": "invalid research run id"}}, 400)
                return
            if payload is None:
                self._send_json({"error": {"message": "research run not found"}}, 404)
                return
            if len(parts) == 1:
                self._send_json(payload)
                return
            filename = (
                "optimized-strategy.json" if parts[1:] == ["strategy"]
                else parts[2] if len(parts) == 3 and parts[1] == "assets" else ""
            )
            asset = research_store.asset_path(parts[0], filename)
            if asset is None:
                self._send_json({"error": {"message": "research asset not found"}}, 404)
                return
            with asset.open("rb") as stream:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(asset.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="{asset.name}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                shutil.copyfileobj(stream, self.wfile)

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
    monitor=None,
    config_path=Path("config/strategy.json"),
    research_root=Path("research"),
) -> ThreadingHTTPServer:
    static_root = Path(__file__).with_name("web")
    store = ReportStore(report_roots)
    return ThreadingHTTPServer((host, port), make_handler(
        store, static_root, monitor, StrategyConfigStore(config_path), LocalResearchStore(research_root),
    ))


def select_watch_report(store, watch_date=None):
    if watch_date is not None:
        return store.get(watch_date)
    return store.latest()


def serve_dashboard(
    report_roots: Sequence[Path],
    host: str = "127.0.0.1",
    port: int = 8765,
    watch_date: Optional[str] = None,
    monitor_log_dir: Optional[Path] = Path("logs/intraday"),
    watchlist_path: Optional[Path] = Path("config/monitor_watchlist.json"),
) -> None:
    store = ReportStore(report_roots)
    report = select_watch_report(store, watch_date)
    settings = StorageSettings.from_env()
    storage_sink = None
    if settings.enabled and report is not None:
        config_path = Path("config/strategy.json")
        strategy_config = (
            json.loads(config_path.read_text(encoding="utf-8"))
            if config_path.exists()
            else {}
        )
        try:
            storage_sink = build_market_persistence(
                report,
                strategy_config=strategy_config,
                settings=settings,
            )
        except Exception as exc:
            if settings.required:
                raise
            print(f"[storage] dual-write disabled after initialization failure: {exc}", file=sys.stderr)
    monitor = IntradayMonitor(
        report, log_dir=monitor_log_dir, supplements=load_watchlist(watchlist_path),
        storage_sink=storage_sink, config_path=Path("config/strategy.json"),
    )
    server = make_server(report_roots, host, port, monitor)
    monitor.start()
    actual_host, actual_port = server.server_address[:2]
    print(f"Strategy dashboard: http://{actual_host}:{actual_port}")
    print(f"当日实盘：http://{actual_host}:{actual_port}/monitor （盘口1秒 / 分时60秒）")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        server.server_close()
