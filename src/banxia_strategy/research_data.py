"""Dated, resumable mootdx inputs for retrospective strategy research."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .mootdx_provider import MootdxProvider, _bar_date, _extract_pools


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False,
                   default=lambda item: item.isoformat()), encoding="utf-8",
    )
    temporary.replace(path)


class ResearchCollector(MootdxProvider):
    """Never enrich past dates with today's order book."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.minute_prices = {}

    def _assign_themes(self, target):
        # Theme assignment is performed separately inside each dated replay.
        pass

    def _detail_worker(self, server, codes, session, current_session):
        result = super()._detail_worker(server, codes, session, False)
        for code, details in result.items():
            self.minute_prices[f"{session}:{code}"] = details.get("prices") or []
        return result

    def collect(self, start: date, end: date, output: Path):
        if start > end:
            raise ValueError("start must be on or before end")
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        if end >= now.date():
            raise ValueError("只使用已完整收盘的历史日期，end 必须早于今天")
        raw_path = output / "raw.json"
        if raw_path.exists():
            raw = json.loads(raw_path.read_text())
            if raw["requested_start"] != start.isoformat() or raw["requested_end"] != end.isoformat():
                raise ValueError("缓存区间与请求不一致，请使用新的输出目录")
            self._names = raw["names"]
            self._histories = raw["histories"]
            self._concepts = raw["concepts"]
            self._concept_sizes = raw["concept_sizes"]
            self._industry_codes = raw["industry_codes"]
            self._calendar = [date.fromisoformat(value) for value in raw["calendar"]]
            self._limit_pools, self._broken_pools = _extract_pools(self._histories, self._names)
        else:
            print("采集 mootdx 主板日线与交易日历…", flush=True)
            self.trading_dates()
            self._load_histories()
            self._histories = {
                code: [row for row in rows if _bar_date(row) <= end]
                for code, rows in self._histories.items()
            }
            self._limit_pools, self._broken_pools = _extract_pools(self._histories, self._names)
            raw = {
                "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                "collected_at": now.isoformat(), "names": self._names,
                "histories": self._histories, "concepts": self._concepts,
                "concept_sizes": self._concept_sizes, "industry_codes": self._industry_codes,
                "calendar": [value.isoformat() for value in self._calendar],
            }
            write_json(raw_path, raw)
        sessions = [session for session in self._calendar if start <= session <= end]
        if not sessions or not any(_bar_date(row) == end for rows in self._histories.values() for row in rows):
            # Weekends are accepted if the last market session is present.
            last = max((s for s in self._calendar if s <= end), default=None)
            if not sessions or not any(_bar_date(row) == last for rows in self._histories.values() for row in rows):
                raise RuntimeError("mootdx 未提供指定区间的最新完整日线")
        for index, session in enumerate(sessions):
            path = output / "sessions" / f"{session}.json"
            if path.exists():
                detail = json.loads(path.read_text())
                self._limit_pools[session] = detail["pool"]
                self.minute_prices.update(detail["minutes"])
            else:
                self._enrich_session(session)
                detail = {
                    "pool": self._limit_pools.get(session, []),
                    "minutes": {key: value for key, value in self.minute_prices.items()
                                if key.startswith(str(session) + ":")},
                }
                write_json(path, detail)
            print(f"历史输入 {index + 1}/{len(sessions)} · {session} · 涨停 {len(detail['pool'])}", flush=True)
        snapshot = {
            "schema_version": 1, "source": "mootdx",
            "requested_start": str(start), "requested_end": str(end),
            "collected_at": raw["collected_at"],
            "calendar": raw["calendar"], "names": self._names,
            "histories": self._histories,
            "pools": {str(key): value for key, value in self._limit_pools.items()},
            "broken": {str(key): value for key, value in self._broken_pools.items()},
            "concepts": self._concepts, "concept_sizes": self._concept_sizes,
            "industry_codes": self._industry_codes, "minutes": self.minute_prices,
            "quality": {
                "universe_count": len(self._names), "daily_history_count": len(self._histories),
                "missing_history_codes": sorted(set(self._names) - set(self._histories)),
                "historical_order_book": False,
                "point_in_time_finance": False, "point_in_time_classification": False,
                "limitations": [
                    "历史封单不可用，统一按原策略缺失封单规则扣分，未使用当前盘口。",
                    "流通股本、股票名称及题材分类来自采集时快照，存在时点偏差和幸存者偏差。",
                    "分时为分钟采样，不能确认秒级封稳、真实竞价撮合价、成交排队和滑点。",
                    "主指标是选股次日收盘封板命中率，不是实际成交率或收益率。",
                    "一个月不足以验证跨月稳定性，优化结果仅作研究候选。",
                ],
            },
        }
        write_json(output / "snapshot.json", snapshot)
        return snapshot


class DatedResearchProvider:
    source_name = "mootdx 历史重建"

    def __init__(self, snapshot, reference: date):
        self.snapshot = snapshot
        self.reference = reference
        # Fresh copies prevent a later run's theme assignment contaminating an earlier run.
        provider = MootdxProvider(client_factory=lambda _: None)
        provider._limit_pools = {
            date.fromisoformat(key): copy.deepcopy(value)
            for key, value in snapshot["pools"].items() if key <= str(reference)
        }
        provider._concepts = snapshot["concepts"]
        provider._concept_sizes = snapshot["concept_sizes"]
        provider._industry_codes = snapshot["industry_codes"]
        provider._assign_themes(reference)
        self.pools = provider._limit_pools

    def trading_dates(self):
        return [date.fromisoformat(value) for value in self.snapshot["calendar"]]

    def limit_up_pool(self, session):
        if session > self.reference:
            raise ValueError("replay cannot read a future pool")
        return copy.deepcopy(self.pools.get(session, []))

    def broken_board_pool(self, session):
        if session > self.reference:
            raise ValueError("replay cannot read future failures")
        return copy.deepcopy(self.snapshot["broken"].get(str(session), []))


def snapshot_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
