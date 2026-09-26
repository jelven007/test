from __future__ import annotations

import gzip
import hashlib
import json
import math
from datetime import date, datetime, time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from ..mootdx_provider import REFERENCE_FILES, MootdxProvider


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda item: item.isoformat()
        if isinstance(item, (date, datetime))
        else str(item),
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_time(value: Any, trade_date: date) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed_time = time.fromisoformat(text)
        except ValueError:
            return None
        return datetime.combine(trade_date, parsed_time, tzinfo=SHANGHAI)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


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
            collected_at = datetime.now(SHANGHAI)
            daily_snapshots = []
            quote_content = None
            if self.repository.is_trading_session(as_of_date):
                quotes = self.provider.quote_snapshots(
                    [item["symbol"] for item in bundle["securities"]]
                )
                daily_snapshots = [
                    self._daily_snapshot(
                        security,
                        quotes.get(security["symbol"]),
                        trade_date=as_of_date,
                        source_node=bundle["source_node"],
                        collected_at=collected_at,
                    )
                    for security in bundle["securities"]
                ]
                available_count = sum(
                    item["data_state"] == "available"
                    for item in daily_snapshots
                )
                if available_count / len(daily_snapshots) < 0.99:
                    raise RuntimeError(
                        "incomplete mootdx daily quotes: "
                        f"{available_count}/{len(daily_snapshots)} available"
                    )
                quote_content = _json_bytes(
                    {
                        item["symbol"]: item
                        for item in daily_snapshots
                    }
                )
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

            if quote_content is not None:
                assets["daily_quotes"] = self._put(
                    object_key=(
                        "daily-quotes/"
                        f"trade_date={as_of_date.isoformat()}/"
                        f"{_sha256(quote_content)}.json.gz"
                    ),
                    content=gzip.compress(quote_content, mtime=0),
                    content_type="application/gzip",
                    snapshot_id=snapshot_id,
                    dataset="security_daily_snapshot",
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
                "daily_quotes_sha256": (
                    _sha256(quote_content)
                    if quote_content is not None
                    else None
                ),
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
                    + len(daily_snapshots)
                ),
                "expected_count": (
                    sum(bundle["expected_counts"].values())
                    + len(daily_snapshots)
                ),
                "daily_quote_count": sum(
                    item["data_state"] == "available"
                    for item in daily_snapshots
                ),
                "missing_quote_count": sum(
                    item["data_state"] == "missing"
                    for item in daily_snapshots
                ),
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
                    daily_snapshots=daily_snapshots,
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

    @staticmethod
    def _daily_snapshot(
        security: Mapping[str, Any],
        quote: Optional[Mapping[str, Any]],
        *,
        trade_date: date,
        source_node: str,
        collected_at: datetime,
    ) -> dict[str, Any]:
        raw = dict(quote or {})
        close = _number(raw.get("price"))
        previous_close = _number(
            raw.get("last_close", raw.get("previous_close"))
        )
        available = bool(
            close is not None
            and close > 0
            and previous_close is not None
            and previous_close > 0
        )
        if not available:
            return {
                "symbol": security["symbol"],
                "exchange": security["exchange"],
                "instrument_type": security.get(
                    "instrument_type",
                    "stock",
                ),
                "trade_date": trade_date,
                "source_node": source_node,
                "source_time": _source_time(
                    raw.get("servertime") or raw.get("source_time"),
                    trade_date,
                ),
                "collected_at": collected_at,
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "previous_close": None,
                "change_pct": None,
                "volume": None,
                "amount": None,
                "data_state": "missing",
                "raw": raw,
            }
        return {
            "symbol": security["symbol"],
            "exchange": security["exchange"],
            "instrument_type": security.get("instrument_type", "stock"),
            "trade_date": trade_date,
            "source_node": source_node,
            "source_time": _source_time(
                raw.get("servertime") or raw.get("source_time"),
                trade_date,
            ),
            "collected_at": collected_at,
            "open": _number(raw.get("open")),
            "high": _number(raw.get("high")),
            "low": _number(raw.get("low")),
            "close": close,
            "previous_close": previous_close,
            "change_pct": round(
                (close / previous_close - 1) * 100,
                6,
            ),
            "volume": _number(
                raw.get("volume", raw.get("vol"))
            ),
            "amount": _number(
                raw.get("amount", raw.get("cumulative_amount_cny"))
            ),
            "data_state": "available",
            "raw": raw,
        }

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
