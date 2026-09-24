from __future__ import annotations

import hashlib
from datetime import timedelta
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

from ..ports.storage import ReportAsset


FORMAT_BY_SUFFIX = {
    ".md": "markdown",
    ".json": "json",
    ".csv": "csv",
}


class MinioObjectAssetStore:
    """Immutable report object writer backed by an S3-compatible MinIO API."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        bucket: str = "strategy-reports",
        secure: bool = False,
        ensure_bucket: bool = False,
        client: Any = None,
    ):
        if client is None:
            try:
                from minio import Minio
            except ImportError as exc:
                raise RuntimeError(
                    "MinIO adapter requires `pip install -e '.[storage]'`"
                ) from exc
            client = Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )
        self.client = client
        self.bucket = bucket
        if ensure_bucket and not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    @staticmethod
    def _validate_key(object_key: str) -> str:
        path = PurePosixPath(object_key)
        if (
            not object_key
            or object_key.startswith("/")
            or ".." in path.parts
            or str(path) != object_key
        ):
            raise ValueError("object_key must be a normalized relative path")
        return object_key

    def put(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> ReportAsset:
        object_key = self._validate_key(object_key)
        content_hash = hashlib.sha256(content).hexdigest()
        source_metadata = metadata or {}
        object_metadata = {
            str(key).replace("_", "-"): str(value)
            for key, value in source_metadata.items()
        }
        object_metadata["sha256"] = content_hash
        self.client.put_object(
            self.bucket,
            object_key,
            BytesIO(content),
            length=len(content),
            content_type=content_type,
            metadata=object_metadata,
        )
        suffix = PurePosixPath(object_key).suffix.lower()
        return ReportAsset(
            report_id=str(source_metadata.get("report_id") or content_hash),
            format=str(
                source_metadata.get("format")
                or FORMAT_BY_SUFFIX.get(suffix)
                or suffix.lstrip(".")
                or "binary"
            ),
            object_key=object_key,
            content_hash=content_hash,
            content_type=content_type,
            size_bytes=len(content),
        )

    def presigned_get_url(
        self,
        object_key: str,
        *,
        expires_seconds: int = 300,
    ) -> str:
        if expires_seconds <= 0:
            raise ValueError("expires_seconds must be positive")
        return str(
            self.client.presigned_get_object(
                self.bucket,
                self._validate_key(object_key),
                expires=timedelta(seconds=expires_seconds),
            )
        )

    def ready(self) -> bool:
        try:
            return bool(self.client.bucket_exists(self.bucket))
        except Exception:
            return False

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
