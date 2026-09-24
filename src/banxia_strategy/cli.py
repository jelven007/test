from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence

from .application.persistence import persist_report_copy
from .mootdx_provider import MootdxProvider
from .storage_config import StorageSettings
from .strategy import StrategyConfig, StrategyEngine, write_report
from .web_server import serve_dashboard


def _parse_date(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="banxia-strategy",
        description="Generate an A-share next-session conditional watchlist.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Fetch market data and create a daily report")
    run.add_argument("--date", type=_parse_date, help="Analysis date, default: today")
    run.add_argument(
        "--config",
        type=Path,
        default=Path("config/strategy.json"),
        help="Strategy JSON configuration",
    )
    run.add_argument(
        "--output",
        type=Path,
        default=Path("reports"),
        help="Output directory",
    )

    doctor = subparsers.add_parser("doctor", help="Check the market data connection")
    doctor.add_argument(
        "--config",
        type=Path,
        default=Path("config/strategy.json"),
        help="Strategy JSON configuration",
    )

    serve = subparsers.add_parser("serve", help="Start the local strategy dashboard")
    serve.add_argument("--host", default="127.0.0.1", help="Listening host")
    serve.add_argument("--port", type=int, default=8765, help="Listening port")
    serve.add_argument("--watch-date", type=_parse_date, help="盘中监控使用的日报日期，格式 YYYY-MM-DD")
    serve.add_argument(
        "--watchlist", type=Path, default=Path("config/monitor_watchlist.json"),
        help="补充监控清单，包含独立的昨收、计划日期及静态筛选结果",
    )
    serve.add_argument(
        "--monitor-log-dir", type=Path, default=Path("logs/intraday"),
        help="每分钟行情与判断日志的保存目录",
    )
    serve.add_argument(
        "--reports-dir",
        action="append",
        type=Path,
        dest="report_dirs",
        help="Report directory; can be supplied more than once",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    config = StrategyConfig.from_file(args.config)
    report = StrategyEngine(MootdxProvider(), config).run(args.date)
    paths = write_report(report, args.output)
    persistence = persist_report_copy(
        report.to_dict(),
        paths,
        strategy_config=asdict(config),
        settings=StorageSettings.from_env(),
        enqueue_events=True,
    )
    print(
        f"{report.as_of}: {report.market['regime']}, "
        f"{len(report.candidates)} candidates, "
        f"market score {report.market['score']}, "
        f"next session {report.next_session or 'unknown'}"
    )
    for candidate in report.candidates:
        print(
            f"{candidate.rank}. {candidate.code} {candidate.name} "
            f"{candidate.score:.1f} {candidate.strategy}"
        )
    print(f"Markdown: {paths['markdown'].resolve()}")
    print(f"CSV: {paths['csv'].resolve()}")
    print(f"JSON: {paths['json'].resolve()}")
    if persistence.enabled:
        if persistence.identity is not None:
            print(f"PostgreSQL run: {persistence.identity.run_id}")
        print(f"MinIO assets: {len(persistence.assets)}")
        for error in persistence.errors:
            print(f"[storage] {error}", file=sys.stderr)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    StrategyConfig.from_file(args.config)
    provider = MootdxProvider()
    dates = [session for session in provider.trading_dates() if session <= date.today()]
    if not dates:
        raise RuntimeError("Trading calendar is empty")
    for session in reversed(dates[-15:]):
        rows = provider.limit_up_pool(session)
        if rows:
            print(
                f"OK: source={provider.source_name}, "
                f"{session.isoformat()}, limit-up rows={len(rows)}"
            )
            return 0
    raise RuntimeError("No limit-up pool data found in the last 15 sessions")


def _serve(args: argparse.Namespace) -> int:
    report_dirs = args.report_dirs or [Path("scheduled_reports"), Path("reports")]
    serve_dashboard(
        report_dirs, args.host, args.port,
        watch_date=args.watch_date.isoformat() if args.watch_date else None,
        monitor_log_dir=args.monitor_log_dir,
        watchlist_path=args.watchlist,
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "serve":
            return _serve(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported command: {args.command}")
    return 2
