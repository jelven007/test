"""Read completed local experiments for the standalone dashboard."""
import json
import uuid
from pathlib import Path


ASSET_NAMES = frozenset({
    "experiment.tar.gz", "result.json", "optimized-strategy.json",
    "report.md", "baseline-daily.csv", "optimized-daily.csv",
})


class LocalResearchStore:
    def __init__(self, root=Path("research")):
        self.root = Path(root).resolve()

    def _path(self, run_id):
        uuid.UUID(run_id)
        for path in self.root.glob(f"*/runs/{run_id}/result.json"):
            if path.resolve().is_relative_to(self.root):
                return path
        return None

    def list_research_runs(self):
        items = []
        for path in self.root.glob("*/runs/*/result.json"):
            try:
                payload = self.get_research_run(path.parent.name)
                if payload and payload.get("status") == "completed" and payload.get("input_sha256"):
                    items.append({key: payload[key] for key in (
                        "run_id", "start_date", "end_date", "created_at",
                    )})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(items, key=lambda item: item["created_at"], reverse=True)[:50]

    def get_research_run(self, run_id):
        path = self._path(run_id)
        return json.loads(path.read_text(encoding="utf-8")) if path else None

    def asset_path(self, run_id, filename):
        if filename not in ASSET_NAMES:
            return None
        path = self._path(run_id)
        asset = path.parent / filename if path else None
        if asset and asset.is_file() and asset.resolve().is_relative_to(path.parent.resolve()):
            return asset
        return None
