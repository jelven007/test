from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..intraday import WATCH_CODES, load_watchlist
from ..web_server import ReportStore, select_watch_report


@dataclass(frozen=True)
class ActivePlan:
    report: Mapping[str, Any]
    candidates: Tuple[Mapping[str, Any], ...]


def load_active_plan(
    report_dirs: Sequence[Path],
    *,
    watch_date: Optional[str],
    watchlist_path: Path,
) -> ActivePlan:
    store = ReportStore(report_dirs)
    report = select_watch_report(store, watch_date)
    if report is None:
        requested = f" for {watch_date}" if watch_date else ""
        raise RuntimeError(f"no strategy report available{requested}")
    report = copy.deepcopy(report)
    report_candidates: Dict[str, Mapping[str, Any]] = {
        str(item["code"]): {
            **item,
            "origin": "report",
            "reference_date": report.get("as_of"),
            "plan_date": report.get("next_session"),
        }
        for item in report.get("candidates", [])
    }
    supplements = load_watchlist(watchlist_path)
    ordered_codes = tuple(
        dict.fromkeys(
            [
                *(str(item["code"]) for item in supplements),
                *WATCH_CODES,
            ]
        )
    )
    for item in supplements:
        report_candidates.setdefault(str(item["code"]), item)
    missing = sorted(set(ordered_codes) - set(report_candidates))
    if missing:
        raise RuntimeError(
            f"strategy report and watchlist do not contain: {', '.join(missing)}"
        )
    return ActivePlan(
        report=report,
        candidates=tuple(report_candidates[code] for code in ordered_codes),
    )
