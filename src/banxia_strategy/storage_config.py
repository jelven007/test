from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


STORAGE_MODES = frozenset({"off", "best_effort", "required"})


def _integer(value: Optional[str], default: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _boolean(value: Optional[str], default: bool = False) -> bool:
    if value in (None, ""):
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean setting must be true/false, yes/no, on/off, or 1/0")


@dataclass(frozen=True)
class StorageSettings:
    mode: str = "off"
    postgres_dsn: str = "postgresql://banxia:banxia-local@127.0.0.1:5432/banxia"
    clickhouse_host: str = "127.0.0.1"
    clickhouse_port: int = 8123
    clickhouse_database: str = "banxia"
    clickhouse_user: str = "banxia"
    clickhouse_password: str = "banxia-local"
    redis_url: str = "redis://:banxia-local@127.0.0.1:6379/0"
    minio_endpoint: str = "127.0.0.1:9002"
    minio_access_key: str = "banxia"
    minio_secret_key: str = "banxia-local-secret"
    minio_secure: bool = False
    minio_report_bucket: str = "strategy-reports"
    strategy_version: str = "v2"
    code_commit: str = "unknown"
    monitor_snapshot_ttl_seconds: int = 172800
    writer_queue_size: int = 1024
    writer_batch_size: int = 32

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def required(self) -> bool:
        return self.mode == "required"

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "StorageSettings":
        if environ is None:
            import os

            environ = os.environ
        mode = environ.get("BANXIA_STORAGE_MODE", "off").strip().lower()
        if mode not in STORAGE_MODES:
            raise ValueError(
                "BANXIA_STORAGE_MODE must be off, best_effort, or required"
            )
        return cls(
            mode=mode,
            postgres_dsn=environ.get(
                "BANXIA_POSTGRES_DSN",
                cls.postgres_dsn,
            ),
            clickhouse_host=environ.get(
                "BANXIA_CLICKHOUSE_HOST",
                cls.clickhouse_host,
            ),
            clickhouse_port=_integer(
                environ.get("BANXIA_CLICKHOUSE_PORT"),
                cls.clickhouse_port,
                "BANXIA_CLICKHOUSE_PORT",
            ),
            clickhouse_database=environ.get(
                "BANXIA_CLICKHOUSE_DATABASE",
                cls.clickhouse_database,
            ),
            clickhouse_user=environ.get(
                "BANXIA_CLICKHOUSE_USER",
                cls.clickhouse_user,
            ),
            clickhouse_password=environ.get(
                "BANXIA_CLICKHOUSE_PASSWORD",
                cls.clickhouse_password,
            ),
            redis_url=environ.get("BANXIA_REDIS_URL", cls.redis_url),
            minio_endpoint=environ.get(
                "BANXIA_MINIO_ENDPOINT",
                cls.minio_endpoint,
            ),
            minio_access_key=environ.get(
                "BANXIA_MINIO_ACCESS_KEY",
                cls.minio_access_key,
            ),
            minio_secret_key=environ.get(
                "BANXIA_MINIO_SECRET_KEY",
                cls.minio_secret_key,
            ),
            minio_secure=_boolean(environ.get("BANXIA_MINIO_SECURE")),
            minio_report_bucket=environ.get(
                "BANXIA_MINIO_REPORT_BUCKET",
                cls.minio_report_bucket,
            ),
            strategy_version=environ.get(
                "BANXIA_STRATEGY_VERSION",
                cls.strategy_version,
            ),
            code_commit=environ.get("BANXIA_CODE_COMMIT", cls.code_commit),
            monitor_snapshot_ttl_seconds=_integer(
                environ.get("BANXIA_MONITOR_SNAPSHOT_TTL_SECONDS"),
                cls.monitor_snapshot_ttl_seconds,
                "BANXIA_MONITOR_SNAPSHOT_TTL_SECONDS",
            ),
            writer_queue_size=_integer(
                environ.get("BANXIA_WRITER_QUEUE_SIZE"),
                cls.writer_queue_size,
                "BANXIA_WRITER_QUEUE_SIZE",
            ),
            writer_batch_size=_integer(
                environ.get("BANXIA_WRITER_BATCH_SIZE"),
                cls.writer_batch_size,
                "BANXIA_WRITER_BATCH_SIZE",
            ),
        )
