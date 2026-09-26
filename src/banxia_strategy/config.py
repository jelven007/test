from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Tuple

from .storage_config import StorageSettings


def _integer(environ: Mapping[str, str], name: str, default: int) -> int:
    value = int(environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _float(environ: Mapping[str, str], name: str, default: float) -> float:
    value = float(environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _schedule(
    environ: Mapping[str, str],
    name: str,
    default: str,
) -> Tuple[str, ...]:
    values = tuple(
        item.strip()
        for item in environ.get(name, default).split(",")
        if item.strip()
    )
    if not values:
        raise ValueError(f"{name} must contain at least one HH:MM value")
    for value in values:
        parts = value.split(":")
        if (
            len(parts) != 2
            or not all(part.isdigit() for part in parts)
            or len(parts[0]) != 2
            or len(parts[1]) != 2
            or not 0 <= int(parts[0]) <= 23
            or not 0 <= int(parts[1]) <= 59
        ):
            raise ValueError(f"{name} contains invalid time {value!r}")
    return values


@dataclass(frozen=True)
class RuntimeSettings:
    environment: str = "local"
    service_name: str = "banxia"
    log_level: str = "INFO"
    kafka_bootstrap_servers: str = "127.0.0.1:29092"
    kafka_topic_prefix: str = ""
    report_dirs: Tuple[Path, ...] = (Path("scheduled_reports"), Path("reports"))
    report_output_dir: Path = Path("reports")
    report_date: Optional[str] = None
    report_schedule: Tuple[str, ...] = ("16:30", "23:30")
    reference_sync_schedule: Tuple[str, ...] = ("16:20",)
    strategy_config_path: Path = Path("config/strategy.json")
    watchlist_path: Path = Path("config/monitor_watchlist.json")
    watch_date: Optional[str] = None
    wal_path: Path = Path("data/wal/market-collector.sqlite3")
    wal_max_bytes: int = 512 * 1024 * 1024
    quote_interval_seconds: float = 1.0
    idle_interval_seconds: float = 60.0
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_token: Optional[str] = None
    metrics_port: Optional[int] = None
    storage: StorageSettings = field(default_factory=StorageSettings)

    def topic(self, name: str) -> str:
        return f"{self.kafka_topic_prefix}{name}"

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        *,
        service_name: Optional[str] = None,
    ) -> "RuntimeSettings":
        source = os.environ if environ is None else environ
        report_dirs = tuple(
            Path(item).expanduser()
            for item in source.get(
                "BANXIA_REPORT_DIRS",
                "scheduled_reports,reports",
            ).split(",")
            if item.strip()
        )
        if not report_dirs:
            raise ValueError("BANXIA_REPORT_DIRS must contain at least one path")
        return cls(
            environment=source.get("BANXIA_ENVIRONMENT", "local"),
            service_name=service_name or source.get("BANXIA_SERVICE_NAME", "banxia"),
            log_level=source.get("BANXIA_LOG_LEVEL", "INFO").upper(),
            kafka_bootstrap_servers=source.get(
                "BANXIA_KAFKA_BOOTSTRAP_SERVERS",
                "127.0.0.1:29092",
            ),
            kafka_topic_prefix=source.get("BANXIA_KAFKA_TOPIC_PREFIX", ""),
            report_dirs=report_dirs,
            report_output_dir=Path(
                source.get("BANXIA_REPORT_OUTPUT_DIR", "reports")
            ).expanduser(),
            report_date=source.get("BANXIA_REPORT_DATE") or None,
            report_schedule=_schedule(
                source,
                "BANXIA_REPORT_SCHEDULE",
                "16:30,23:30",
            ),
            reference_sync_schedule=_schedule(
                source,
                "BANXIA_REFERENCE_SYNC_SCHEDULE",
                "16:20",
            ),
            strategy_config_path=Path(
                source.get("BANXIA_STRATEGY_CONFIG", "config/strategy.json")
            ).expanduser(),
            watchlist_path=Path(
                source.get(
                    "BANXIA_MONITOR_WATCHLIST",
                    "config/monitor_watchlist.json",
                )
            ).expanduser(),
            watch_date=source.get("BANXIA_WATCH_DATE") or None,
            wal_path=Path(
                source.get(
                    "BANXIA_COLLECTOR_WAL_PATH",
                    "data/wal/market-collector.sqlite3",
                )
            ).expanduser(),
            wal_max_bytes=_integer(
                source,
                "BANXIA_COLLECTOR_WAL_MAX_BYTES",
                512 * 1024 * 1024,
            ),
            quote_interval_seconds=_float(
                source,
                "BANXIA_QUOTE_INTERVAL_SECONDS",
                1.0,
            ),
            idle_interval_seconds=_float(
                source,
                "BANXIA_IDLE_INTERVAL_SECONDS",
                60.0,
            ),
            api_host=source.get("BANXIA_API_HOST", "127.0.0.1"),
            api_port=_integer(source, "BANXIA_API_PORT", 8765),
            api_token=source.get("BANXIA_API_TOKEN") or None,
            metrics_port=(
                _integer(source, "BANXIA_METRICS_PORT", 9100)
                if source.get("BANXIA_METRICS_PORT")
                else None
            ),
            storage=StorageSettings.from_env(source),
        )
