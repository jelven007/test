"""Persist immutable research artifacts without changing the live watchlist."""
from __future__ import annotations

import hashlib
import json
import tarfile
from dataclasses import asdict
from pathlib import Path

from .adapters.minio import MinioObjectAssetStore
from .adapters.postgres import PostgresStorage, _json
from .research_data import snapshot_hash, write_json


def save_research(repository, payload, assets):
    with repository.connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO banxia.research_run
                (run_id, start_date, end_date, created_at, input_sha256, payload, assets)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (run_id) DO NOTHING""",
                (payload["run_id"], payload["start_date"], payload["end_date"],
                 payload["created_at"], payload["input_sha256"], _json(payload), _json(assets)),
            )
            for variant in ("baseline", "optimized"):
                for day in payload[variant]["days"]:
                    cursor.execute(
                        """INSERT INTO banxia.research_daily
                        (run_id, variant, reference_date, plan_date, accuracy_pct,
                         observed_count, hit_count, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (run_id, variant, reference_date) DO NOTHING""",
                        (payload["run_id"], variant, day["reference_date"], day["plan_date"],
                         day["summary"]["accuracy_pct"], day["summary"]["observed_count"],
                         day["summary"]["hit_count"], _json(day)),
                    )
            strategy = payload["strategy"]
            cursor.execute(
                """INSERT INTO banxia.research_strategy
                (strategy_id, run_id, revision, config, metadata)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (strategy_id) DO NOTHING""",
                (strategy["strategy_id"], payload["run_id"], strategy["revision"],
                 _json(strategy["config"]), _json(strategy)),
            )


def persist_research(payload, directory: Path, snapshot_path: Path, settings):
    if payload.get("input_sha256") != snapshot_hash(snapshot_path):
        raise ValueError("输入快照哈希不一致，拒绝归档")
    if json.loads((directory / "result.json").read_text()) != payload:
        raise ValueError("磁盘结果与入库结果不一致，拒绝归档")
    repository = PostgresStorage(settings.postgres_dsn)
    store = MinioObjectAssetStore(
        settings.minio_endpoint, settings.minio_access_key, settings.minio_secret_key,
        bucket=settings.minio_report_bucket, secure=settings.minio_secure, ensure_bucket=True,
    )
    archive = directory / "experiment.tar.gz"
    files = sorted(path for path in directory.rglob("*")
                   if path.is_file() and path.name not in {"experiment.tar.gz", "persistence.json"})
    with tarfile.open(archive, "w:gz") as stream:
        for path in files:
            stream.add(path, arcname=str(Path("experiment") / path.relative_to(directory)))
        stream.add(snapshot_path, arcname="inputs/snapshot.json")
    assets = []
    try:
        for filename, content_type in [
            ("experiment.tar.gz", "application/gzip"), ("result.json", "application/json"),
            ("optimized-strategy.json", "application/json"), ("report.md", "text/markdown"),
            ("baseline-daily.csv", "text/csv"), ("optimized-daily.csv", "text/csv"),
        ]:
            content = (directory / filename).read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            asset = store.put(
                object_key=f"research/{payload['run_id']}/{digest[:16]}/{filename}",
                content=content, content_type=content_type,
            )
            assets.append({"filename": filename, **asdict(asset)})
        save_research(repository, payload, assets)
        write_json(directory / "persistence.json", {
            "run_id": payload["run_id"], "postgres": "saved", "assets": assets,
        })
    finally:
        repository.close()
        store.close()
