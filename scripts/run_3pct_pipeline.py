"""Run the full monthly-target pipeline with D1 threshold lowered from 5% to 3%.

Reuses helper fns in src/banxia_strategy/monthly_target_research.py (daily data,
minute fetch, replay structure) but regenerates candidates with the 3% floor
and a wider per-day cap (top-15 by change / amount / score). Writes everything
under research/monthly-target-v1-3pct/, sharing the daily/minute caches with
the 5% run via symlinks so we only need to download the newly added D1/D2/D3
minute days.
"""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from banxia_strategy.monthly_target_research import (
    _action_dates,
    _request_with_retry,
    csv_save,
    load,
    save,
    sha256,
)
from banxia_strategy.mootdx_provider import MootdxProvider, _bar_date

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "research" / "monthly-target-v1-3pct"
PROTOCOL_SRC = ROOT / "docs" / "research" / "monthly-5pct-protocol.json"
PROTOCOL_DST = ROOT / "docs" / "research" / "monthly-3pct-protocol.json"

MIN_D1_CHANGE_PCT = 3.0
TOP_PER_DAY = {"change": 15, "amount": 15, "score": 15}


def build_candidates() -> list[dict]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    protocol_src = load(PROTOCOL_SRC)
    protocol = dict(protocol_src)
    protocol["universe"] = dict(protocol["universe"])
    protocol["universe"]["minimum_d1_change_pct"] = MIN_D1_CHANGE_PCT
    protocol["universe"]["candidate_collection_per_day"] = {
        "top_by_change": TOP_PER_DAY["change"],
        "top_by_amount": TOP_PER_DAY["amount"],
        "top_by_composite_score": TOP_PER_DAY["score"],
    }
    protocol["notes_vs_5pct"] = (
        "D1 change threshold lowered from 5% to 3%; per-day candidate cap "
        "expanded from 5 to 15 by change/amount/score."
    )
    PROTOCOL_DST.write_text(json.dumps(protocol, indent=2, ensure_ascii=False))

    manifest = load(OUTPUT / "daily-manifest.json")
    universe = load(OUTPUT / "universe.json")["securities"]
    calendar = sorted(v for v in manifest["calendar"] if "2024-12-01" <= v <= "2026-09-30")
    positions = {v: i for i, v in enumerate(calendar)}
    by_day: dict[str, list[dict]] = defaultdict(list)
    audit: Counter = Counter()

    for index, security in enumerate(universe, 1):
        name = str(security["name"])
        if "ST" in name.upper() or "退" in name:
            audit["excluded_current_st_or_delisting_name"] += 1
            continue
        path = OUTPUT / "daily" / f"{security['symbol']}.json.gz"
        if not path.exists():
            audit["missing_daily_file"] += 1
            continue
        daily = load(path)
        bars = {_bar_date(dict(item)).isoformat(): item for item in daily["bars"]}
        actions = _action_dates(daily.get("actions", []))
        for d1_date in sorted(bars):
            pos = positions.get(d1_date)
            if pos is None or pos < 5 or pos + 2 >= len(calendar) or not "2025-01-01" <= d1_date <= "2026-09-28":
                continue
            previous_date = calendar[pos - 1]
            d2_date = calendar[pos + 1]
            d3_date = calendar[pos + 2]
            previous = bars.get(previous_date)
            d1 = bars.get(d1_date)
            d2 = bars.get(d2_date)
            d3 = bars.get(d3_date)
            if not all((previous, d1, d2, d3)):
                audit["missing_consecutive_session"] += 1
                continue
            pc = float(previous.get("close") or 0)
            close = float(d1.get("close") or 0)
            if pc <= 0 or close <= 0:
                audit["invalid_close"] += 1
                continue
            change_pct = 100 * (close / pc - 1)
            max_change = 11.5 if security["board"] == "main" else 21.5
            if not MIN_D1_CHANGE_PCT <= change_pct <= max_change:
                continue
            amount = float(d1.get("amount") or 0)
            if amount < protocol["universe"]["minimum_d1_amount_cny"]:
                audit["below_minimum_amount"] += 1
                continue
            high = float(d1.get("high") or 0)
            low = float(d1.get("low") or 0)
            close_loc = (close - low) / (high - low) if high > low > 0 else 1.0
            five_day = bars.get(calendar[pos - 5])
            fdc = float(five_day.get("close") or 0) if five_day else 0
            ret5 = 100 * (close / fdc - 1) if fdc > 0 else None
            composite = change_pct + 5 * close_loc + math.log10(max(amount, 1) / 1e8) - 0.03 * max(ret5 or 0, 0)
            by_day[d1_date].append({
                "symbol": security["symbol"],
                "name": name,
                "market": security["market"],
                "board": security["board"],
                "d1_date": d1_date,
                "d2_date": d2_date,
                "d3_date": d3_date,
                "d1_close": close,
                "d1_change_pct": change_pct,
                "d1_amount_cny": amount,
                "d1_close_location": close_loc,
                "d1_return_5d_pct": ret5,
                "d1_composite_score": composite,
                "d2_action": d2_date in actions,
                "d3_action": d3_date in actions,
            })
        if index % 500 == 0:
            print(f"  candidate scan {index}/{len(universe)}  events={sum(len(v) for v in by_day.values())}")

    orderings = (
        ("change", "d1_change_pct", TOP_PER_DAY["change"]),
        ("amount", "d1_amount_cny", TOP_PER_DAY["amount"]),
        ("score", "d1_composite_score", TOP_PER_DAY["score"]),
    )
    candidates: list[dict] = []
    for d1_date, rows in sorted(by_day.items()):
        selected: dict[str, dict] = {}
        for basis, key, limit in orderings:
            ordered = sorted(rows, key=lambda r, k=key: (-float(r[k]), r["symbol"]))
            for rank, row in enumerate(ordered[:limit], 1):
                item = selected.setdefault(row["symbol"], dict(row))
                item[f"d1_{basis}_rank"] = rank
        candidates.extend(selected.values())
    candidates.sort(key=lambda r: (r["d2_date"], r["symbol"]))
    save(OUTPUT / "candidates.json.gz", candidates)
    csv_save(OUTPUT / "candidates.csv", candidates)
    save(OUTPUT / "candidate-audit.json", {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256(PROTOCOL_DST),
        "candidate_count": len(candidates),
        "candidate_days": len(by_day),
        "audit": dict(audit),
    })
    print(f"candidates: {len(candidates)} across {len(by_day)} days")
    return candidates


def collect_missing_minutes(candidates: list[dict]) -> dict:
    needed: set[tuple[str, str]] = set()
    for c in candidates:
        for d in (c["d1_date"], c["d2_date"], c["d3_date"]):
            needed.add((c["symbol"], d))
    missing = [(s, d) for s, d in needed if not (OUTPUT / "minutes" / f"{d}_{s}.json.gz").exists()]
    print(f"minutes needed: {len(needed)}  missing: {len(missing)}")
    if not missing:
        return {"cached": len(needed), "downloaded": 0, "errors": 0}

    provider = MootdxProvider()
    workers = min(len(provider.servers), max(1, len(missing)))

    def worker(number: int, partition: list[tuple[str, str]]):
        counts = {"downloaded": 0, "errors": 0}
        state: dict = {"client": None, "server": None}
        try:
            for i, (symbol, day) in enumerate(partition, 1):
                path = OUTPUT / "minutes" / f"{day}_{symbol}.json.gz"
                if path.exists():
                    continue
                try:
                    def fetch(client, symbol=symbol, day=day):
                        frame = client.minutes(symbol=symbol, date=day.replace("-", ""))
                        if frame is None or len(frame) != 240:
                            raise ValueError(f"expected 240 minute samples, got {None if frame is None else len(frame)}")
                        prices = [float(v) for v in frame["price"]]
                        volumes = [float(v) for v in frame["vol"]]
                        if not all(math.isfinite(v) for v in prices + volumes):
                            raise ValueError("non-finite minute sample")
                        return {"date": day, "symbol": symbol, "prices": prices, "volumes": volumes, "source": "mootdx"}

                    save(path, _request_with_retry(provider, number, state, fetch))
                    counts["downloaded"] += 1
                except Exception as exc:
                    counts["errors"] += 1
                    print(f"  err {symbol} {day}: {exc}")
                if i % 200 == 0:
                    print(f"  worker {number}: {i}/{len(partition)} dl={counts['downloaded']} err={counts['errors']}")
        finally:
            if state.get("client") is not None:
                provider._close(state["client"])
        return counts

    total = {"downloaded": 0, "errors": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, idx, missing[idx::workers]) for idx in range(workers)]
        for fut in as_completed(futures):
            for k, v in fut.result().items():
                total[k] = total.get(k, 0) + v
    total["cached"] = len(needed) - total["downloaded"] - total["errors"]
    return total


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("candidates", "all"):
        cands = build_candidates()
    else:
        cands = load(OUTPUT / "candidates.json.gz")
    if cmd in ("minutes", "all"):
        print("fetch stats:", collect_missing_minutes(cands))
    print("done.")
