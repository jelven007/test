from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Mapping, Optional

from ..mootdx_provider import REFERENCE_FILES, MootdxProvider


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


class MarketReferenceSync:
    """Archive and atomically publish the complete market reference bundle."""

    def __init__(
        self,
        *,
        repository: Any,
        object_store: Any,
        provider: Optional[MootdxProvider] = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.provider = provider or MootdxProvider()

    def run(self, as_of_date: date) -> Mapping[str, Any]:
        snapshot_id = self.repository.begin_market_reference_snapshot(
            as_of_date
        )
        try:
            bundle = self.provider.reference_snapshot()
            catalog = _json_bytes(bundle["raw_securities"])
            assets = {
                "security_catalog": self._put(
                    object_key=(
                        "security-catalog/"
                        f"as_of_date={as_of_date.isoformat()}/"
                        f"{_sha256(catalog)}.json.gz"
                    ),
                    content=gzip.compress(catalog, mtime=0),
                    content_type="application/gzip",
                    snapshot_id=snapshot_id,
                    dataset="security_catalog",
                )
            }
            for filename in REFERENCE_FILES:
                content = bundle["files"][filename]
                assets[filename] = self._put(
                    object_key=(
                        f"reference/file={filename}/"
                        f"as_of_date={as_of_date.isoformat()}/"
                        f"{_sha256(content)}.bin"
                    ),
                    content=content,
                    content_type="application/octet-stream",
                    snapshot_id=snapshot_id,
                    dataset=filename,
                )

            content_manifest = {
                "security_catalog_sha256": _sha256(catalog),
                "reference_files": {
                    filename: _sha256(bundle["files"][filename])
                    for filename in REFERENCE_FILES
                },
                "expected_counts": {
                    str(key): int(value)
                    for key, value in bundle["expected_counts"].items()
                },
            }
            content_sha256 = _sha256(_json_bytes(content_manifest))
            source_version = (
                f"mootdx/{_package_version('mootdx')} "
                f"tdxpy/{_package_version('tdxpy')}"
            )
            manifest = {
                "snapshot_id": snapshot_id,
                "dataset": "market_reference",
                "as_of_date": as_of_date.isoformat(),
                "source_node": bundle["source_node"],
                "source_version": source_version,
                "schema_version": 1,
                "content_sha256": content_sha256,
                "row_count": (
                    len(bundle["raw_securities"])
                    + len(bundle["memberships"])
                ),
                "expected_count": sum(bundle["expected_counts"].values()),
                "assets": {
                    name: {
                        "object_key": asset.object_key,
                        "content_hash": asset.content_hash,
                        "size_bytes": asset.size_bytes,
                    }
                    for name, asset in assets.items()
                },
                **content_manifest,
            }
            manifest_asset = self._put(
                object_key=(
                    "manifests/dataset=market_reference/"
                    f"snapshot_id={snapshot_id}/manifest.json"
                ),
                content=_json_bytes(manifest),
                content_type="application/json",
                snapshot_id=snapshot_id,
                dataset="manifest",
            )
            published_snapshot_id = (
                self.repository.publish_market_reference_snapshot(
                    snapshot_id,
                    source_node=bundle["source_node"],
                    source_version=source_version,
                    content_sha256=content_sha256,
                    raw_object_key=manifest_asset.object_key,
                    row_count=manifest["row_count"],
                    expected_count=manifest["expected_count"],
                    securities=bundle["securities"],
                    memberships=bundle["memberships"],
                )
            )
            return {
                **manifest,
                "snapshot_id": published_snapshot_id,
                "raw_object_key": manifest_asset.object_key,
            }
        except Exception as exc:
            self.repository.fail_market_reference_snapshot(snapshot_id, exc)
            raise

    def _put(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        snapshot_id: str,
        dataset: str,
    ) -> Any:
        return self.object_store.put(
            object_key=object_key,
            content=content,
            content_type=content_type,
            metadata={
                "report_id": snapshot_id,
                "format": "raw",
                "dataset": dataset,
                "snapshot_id": snapshot_id,
            },
        )
