"""Mootdx-only research for a 5% monthly T+1 portfolio target."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from .mootdx_provider import MootdxProvider, _bar_date, _json_value
from .t1_research import cash_paid, cents, minute_quality, sell_proxy


LOG = logging.getLogger("monthly-target-research")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "research/monthly-target-v1"
DEFAULT_PROTOCOL = ROOT / "docs/research/monthly-5pct-protocol.json"
DEFAULT_SNAPSHOTS = (
    ROOT / "research/catalog-backfill/20250101-20250924/snapshot.json",
    ROOT / "research/catalog-backfill/20250925-20260924/snapshot.json",
)


def load(path: Path) -> Any:
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt") as stream:
            return json.load(stream)
    return json.loads(path.read_text())


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if str(path).endswith(".gz"):
        with gzip.open(temporary, "wt") as stream:
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
    else:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        )
    temporary.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_save(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _request_with_retry(
    provider: MootdxProvider,
    worker: int,
    state: dict[str, Any],
    action,
):
    errors = []
    for attempt in range(3):
        server = provider.servers[(worker + attempt) % len(provider.servers)]
        try:
            if state.get("server") != server or state.get("client") is None:
                if state.get("client") is not None:
                    provider._close(state["client"])
                state["client"] = provider._client(server)
                state["server"] = server
            return action(state["client"])
        except Exception as exc:
            errors.append(str(exc))
            if state.get("client") is not None:
                provider._close(state["client"])
            state["client"] = None
            state["server"] = None
    raise RuntimeError("; ".join(errors))


def collect_daily(
    output: Path,
    snapshots: Sequence[Path] = DEFAULT_SNAPSHOTS,
) -> Mapping[str, Any]:
    provider = MootdxProvider()
    securities = provider.securities()
    by_symbol = {item["symbol"]: item for item in securities}
    save(
        output / "universe.json",
        {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source": "mootdx",
            "securities": securities,
        },
    )

    histories: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    calendars = set()
    for snapshot_path in snapshots:
        snapshot = load(snapshot_path)
        calendars.update(snapshot["calendar"])
        for symbol, rows in snapshot["histories"].items():
            if symbol not in by_symbol:
                continue
            target = histories[symbol]
            for row in rows:
                target[str(row["datetime"])[:10]] = row
        del snapshot

    counts = Counter()
    for symbol, rows in histories.items():
        path = output / "daily" / f"{symbol}.json.gz"
        if not path.exists():
            save(
                path,
                {
                    "symbol": symbol,
                    "bars": [rows[key] for key in sorted(rows)],
                    "actions": [],
                    "source": "mootdx_research_snapshot",
                },
            )
        counts["snapshot_symbols"] += 1
    del histories

    missing = [
        item for item in securities
        if not (output / "daily" / f"{item['symbol']}.json.gz").exists()
    ]

    def worker(number: int, partition: Sequence[Mapping[str, Any]]) -> Counter:
        result = Counter()
        state: dict[str, Any] = {"client": None, "server": None}
        try:
            for index, security in enumerate(partition, 1):
                symbol = security["symbol"]
                path = output / "daily" / f"{symbol}.json.gz"
                if path.exists():
                    result["cached"] += 1
                    continue
                try:
                    def fetch(client):
                        frame = client.bars(
                            symbol=symbol,
                            frequency=9,
                            offset=800,
                        )
                        if frame is None or frame.empty:
                            raise ValueError("empty daily history")
                        market = 1 if security["market"] == "sh" else 0
                        actions = client.client.get_xdxr_info(market, symbol)
                        if actions is None:
                            raise ValueError("missing corporate-action response")
                        if hasattr(actions, "to_dict"):
                            actions = actions.to_dict(orient="records")
                        return {
                            "symbol": symbol,
                            "bars": _json_value(frame.to_dict(orient="records")),
                            "actions": _json_value(actions),
                            "source": "mootdx",
                        }

                    save(
                        path,
                        _request_with_retry(
                            provider,
                            number,
                            state,
                            fetch,
                        ),
                    )
                    result["downloaded"] += 1
                except Exception as exc:
                    result["errors"] += 1
                    LOG.warning("daily %s: %s", symbol, exc)
                if index % 50 == 0:
                    LOG.info(
                        "daily worker=%s progress=%s/%s counts=%s",
                        number,
                        index,
                        len(partition),
                        dict(result),
                    )
        finally:
            if state.get("client") is not None:
                provider._close(state["client"])
        return result

    workers = min(len(provider.servers), max(1, len(missing)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(worker, index, missing[index::workers])
            for index in range(workers)
        ]
        for future in as_completed(futures):
            counts.update(future.result())

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "mootdx",
        "protocol_sha256": sha256(DEFAULT_PROTOCOL),
        "snapshot_sha256": {
            str(path.relative_to(ROOT)): sha256(path) for path in snapshots
        },
        "security_count": len(securities),
        "calendar": sorted(calendars),
        "counts": dict(counts),
    }
    save(output / "daily-manifest.json", manifest)
    return manifest


def _action_dates(actions: Iterable[Mapping[str, Any]]) -> set[str]:
    result = set()
    for item in actions:
        try:
            if int(item.get("category") or 0) in (1, 11, 12):
                result.add(_bar_date(dict(item)).isoformat())
        except (KeyError, TypeError, ValueError):
            continue
    return result


def monthly_buy_proxy(
    event: Mapping[str, Any],
    bar: Mapping[str, Any],
    minute: Mapping[str, Sequence[float]],
    mode: str,
    model: Mapping[str, Any],
    *,
    protect_d1_close: bool,
) -> dict[str, Any]:
    previous = float(event["close"])
    opening = float(bar["open"])
    ratio = 0.1 if event["board"] == "main" else 0.2
    upper = cents(previous * (1 + ratio))
    limit = min(
        cents(previous * model["buy_limit_vs_d1_close"]),
        upper - 0.01,
    )
    lot = model["lot_shares"]
    shares = math.floor(model["position_cny"] / limit / lot) * lot
    while shares > 0 and cash_paid(limit, shares, model) > model["position_cny"]:
        shares -= lot
    if shares < model["minimum_shares_by_board"][event["board"]]:
        return {"buy_status": "below_one_lot"}

    prices = minute["prices"]
    volumes = minute["volumes"]
    pending = False
    dipped = False
    signal_index = None
    for index in range(29):
        price = prices[index]
        if protect_d1_close and price < previous - 0.001:
            return {"buy_status": "observed_breach_before_fill"}
        if pending:
            fill = cents(
                price * (1 + model["slippage_each_side"]),
                up=True,
            )
            capacity = (
                volumes[index]
                * 100
                * model[
                    "maximum_participation_in_observed_minute_volume"
                ]
            )
            if (
                price < upper - 0.001
                and fill <= limit + 0.001
                and shares <= capacity
            ):
                return {
                    "buy_status": "filled_proxy",
                    "signal_index": signal_index,
                    "buy_index": index,
                    "buy_price": fill,
                    "shares": shares,
                }
        signal = (
            (mode == "early" and index == 0)
            or (
                mode == "breakout"
                and index >= 1
                and price >= max(opening, prices[0]) * 1.005
            )
            or (
                mode == "reclaim"
                and dipped
                and price >= opening + 0.009
            )
        )
        if signal and not pending:
            pending = True
            signal_index = index
        if price < opening - 0.001:
            dipped = True
    return {"buy_status": "no_pre10_fill_proxy"}


def build_candidates(output: Path) -> list[Mapping[str, Any]]:
    protocol = load(DEFAULT_PROTOCOL)
    manifest = load(output / "daily-manifest.json")
    universe = load(output / "universe.json")["securities"]
    calendar = sorted(
        value for value in manifest["calendar"]
        if "2024-12-01" <= value <= "2026-09-30"
    )
    positions = {value: index for index, value in enumerate(calendar)}
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    audit = Counter()

    for index, security in enumerate(universe, 1):
        name = str(security["name"])
        if "ST" in name.upper() or "退" in name:
            audit["excluded_current_st_or_delisting_name"] += 1
            continue
        path = output / "daily" / f"{security['symbol']}.json.gz"
        if not path.exists():
            audit["missing_daily_file"] += 1
            continue
        daily = load(path)
        bars = {
            _bar_date(dict(item)).isoformat(): item
            for item in daily["bars"]
        }
        actions = _action_dates(daily.get("actions", []))
        symbol_days = sorted(bars)
        for d1_date in symbol_days:
            position = positions.get(d1_date)
            if (
                position is None
                or position < 5
                or position + 2 >= len(calendar)
                or not "2025-01-01" <= d1_date <= "2026-09-28"
            ):
                continue
            previous_date = calendar[position - 1]
            d2_date = calendar[position + 1]
            d3_date = calendar[position + 2]
            previous = bars.get(previous_date)
            d1 = bars.get(d1_date)
            d2 = bars.get(d2_date)
            d3 = bars.get(d3_date)
            if not all((previous, d1, d2, d3)):
                audit["missing_consecutive_session"] += 1
                continue
            previous_close = float(previous.get("close") or 0)
            close = float(d1.get("close") or 0)
            if previous_close <= 0 or close <= 0:
                audit["invalid_close"] += 1
                continue
            change_pct = 100 * (close / previous_close - 1)
            maximum_change = 11.5 if security["board"] == "main" else 21.5
            if not 5 <= change_pct <= maximum_change:
                continue
            amount = float(d1.get("amount") or 0)
            if amount < protocol["universe"]["minimum_d1_amount_cny"]:
                audit["below_minimum_amount"] += 1
                continue
            high = float(d1.get("high") or 0)
            low = float(d1.get("low") or 0)
            close_location = (
                (close - low) / (high - low)
                if high > low > 0
                else 1.0
            )
            five_day_bar = bars.get(calendar[position - 5])
            five_day_close = float(
                five_day_bar.get("close") or 0
            ) if five_day_bar else 0
            return_5d_pct = (
                100 * (close / five_day_close - 1)
                if five_day_close > 0
                else None
            )
            composite_score = (
                change_pct
                + 5 * close_location
                + math.log10(max(amount, 1) / 100_000_000)
                - 0.03 * max(return_5d_pct or 0, 0)
            )
            by_day[d1_date].append(
                {
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
                    "d1_close_location": close_location,
                    "d1_return_5d_pct": return_5d_pct,
                    "d1_composite_score": composite_score,
                    "d2_action": d2_date in actions,
                    "d3_action": d3_date in actions,
                }
            )
        if index % 500 == 0:
            LOG.info(
                "candidate universe=%s/%s events=%s",
                index,
                len(universe),
                sum(map(len, by_day.values())),
            )

    limits = protocol["universe"]["candidate_collection_per_day"]
    candidates = []
    for d1_date, rows in sorted(by_day.items()):
        selected: dict[str, dict[str, Any]] = {}
        orderings = (
            ("change", "d1_change_pct", limits["top_by_change"]),
            ("amount", "d1_amount_cny", limits["top_by_amount"]),
            ("score", "d1_composite_score", limits["top_by_composite_score"]),
        )
        for basis, key, limit in orderings:
            ordered = sorted(
                rows,
                key=lambda row: (-float(row[key]), row["symbol"]),
            )
            for rank, row in enumerate(ordered[:limit], 1):
                item = selected.setdefault(row["symbol"], dict(row))
                item[f"d1_{basis}_rank"] = rank
        candidates.extend(selected.values())
    candidates.sort(key=lambda row: (row["d2_date"], row["symbol"]))
    save(output / "candidates.json.gz", candidates)
    csv_save(output / "candidates.csv", candidates)
    save(
        output / "candidate-audit.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "protocol_sha256": sha256(DEFAULT_PROTOCOL),
            "daily_manifest_sha256": sha256(output / "daily-manifest.json"),
            "candidate_count": len(candidates),
            "candidate_days": len(by_day),
            "audit": dict(audit),
        },
    )
    return candidates


def collect_minutes(
    output: Path,
    legacy_caches: Sequence[Path] = (),
) -> Mapping[str, Any]:
    candidates = load(output / "candidates.json.gz")
    universe = load(output / "universe.json")
    securities = {
        item["symbol"]: item for item in universe["securities"]
    }
    del universe
    needed: dict[str, set[str]] = defaultdict(set)
    for item in candidates:
        needed[item["symbol"]].update((item["d2_date"], item["d3_date"]))

    imported = 0
    needed_keys = {
        f"{day}:{symbol}" for symbol, days in needed.items() for day in days
    }
    for cache_path in legacy_caches:
        if not cache_path.exists():
            continue
        for key, minute in load(cache_path).items():
            if key not in needed_keys:
                continue
            day, symbol = key.split(":")
            target = output / "minutes" / f"{day}_{symbol}.json.gz"
            if target.exists() or len(minute.get("prices", [])) != 240:
                continue
            save(
                target,
                {
                    "date": day,
                    "symbol": symbol,
                    "prices": minute["prices"],
                    "volumes": minute["volumes"],
                    "source": "mootdx_legacy_cache",
                },
            )
            imported += 1

    provider = MootdxProvider()
    symbols = sorted(needed)

    def worker(number: int, partition: Sequence[str]) -> Counter:
        counts = Counter()
        state: dict[str, Any] = {"client": None, "server": None}
        try:
            for index, symbol in enumerate(partition, 1):
                daily_path = output / "daily" / f"{symbol}.json.gz"
                try:
                    daily = load(daily_path)
                    if not daily.get("actions"):
                        security = securities[symbol]

                        def fetch_actions(client):
                            market = 1 if security["market"] == "sh" else 0
                            result = client.client.get_xdxr_info(market, symbol)
                            if result is None:
                                raise ValueError("missing action response")
                            return (
                                result.to_dict(orient="records")
                                if hasattr(result, "to_dict")
                                else result
                            )

                        daily["actions"] = _json_value(
                            _request_with_retry(
                                provider,
                                number,
                                state,
                                fetch_actions,
                            )
                        )
                        save(daily_path, daily)
                        counts["action_downloaded"] += 1
                except Exception as exc:
                    counts["action_errors"] += 1
                    LOG.warning("actions %s: %s", symbol, exc)

                for day in sorted(needed[symbol]):
                    path = output / "minutes" / f"{day}_{symbol}.json.gz"
                    if path.exists():
                        counts["cached"] += 1
                        continue
                    try:
                        def fetch_minutes(client):
                            frame = client.minutes(
                                symbol=symbol,
                                date=day.replace("-", ""),
                            )
                            if frame is None or len(frame) != 240:
                                raise ValueError(
                                    "expected 240 minute samples, got "
                                    f"{None if frame is None else len(frame)}"
                                )
                            prices = [float(value) for value in frame["price"]]
                            volumes = [float(value) for value in frame["vol"]]
                            if not all(
                                math.isfinite(value)
                                for value in prices + volumes
                            ):
                                raise ValueError("non-finite minute sample")
                            return {
                                "date": day,
                                "symbol": symbol,
                                "prices": prices,
                                "volumes": volumes,
                                "source": "mootdx",
                            }

                        save(
                            path,
                            _request_with_retry(
                                provider,
                                number,
                                state,
                                fetch_minutes,
                            ),
                        )
                        counts["downloaded"] += 1
                    except Exception as exc:
                        counts["minute_errors"] += 1
                        LOG.warning("minute %s %s: %s", symbol, day, exc)
                if index % 25 == 0:
                    LOG.info(
                        "minute worker=%s progress=%s/%s counts=%s",
                        number,
                        index,
                        len(partition),
                        dict(counts),
                    )
        finally:
            if state.get("client") is not None:
                provider._close(state["client"])
        return counts

    counts = Counter(imported=imported)
    workers = min(len(provider.servers), max(1, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(worker, index, symbols[index::workers])
            for index in range(workers)
        ]
        for future in as_completed(futures):
            counts.update(future.result())

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256(DEFAULT_PROTOCOL),
        "candidate_sha256": sha256(output / "candidates.json.gz"),
        "symbols": len(symbols),
        "minute_keys": len(needed_keys),
        "counts": dict(counts),
    }
    save(output / "minute-manifest.json", manifest)
    return manifest


def replay(output: Path) -> list[Mapping[str, Any]]:
    protocol = load(DEFAULT_PROTOCOL)
    model = protocol["execution"]
    candidates = load(output / "candidates.json.gz")
    results = []
    audit = Counter()
    daily_cache: dict[str, Mapping[str, Any]] = {}

    for index, event in enumerate(candidates, 1):
        symbol = event["symbol"]
        if symbol not in daily_cache:
            daily = load(output / "daily" / f"{symbol}.json.gz")
            daily_cache[symbol] = {
                "bars": {
                    _bar_date(dict(item)).isoformat(): item
                    for item in daily["bars"]
                },
                "actions": _action_dates(daily.get("actions", [])),
            }
        daily = daily_cache[symbol]
        d2_bar = daily["bars"].get(event["d2_date"])
        d3_bar = daily["bars"].get(event["d3_date"])
        d2_path = (
            output / "minutes"
            / f"{event['d2_date']}_{symbol}.json.gz"
        )
        d3_path = (
            output / "minutes"
            / f"{event['d3_date']}_{symbol}.json.gz"
        )
        d2_minute = load(d2_path) if d2_path.exists() else None
        d3_minute = load(d3_path) if d3_path.exists() else None
        status = None
        if not d2_bar or not d3_bar:
            status = "missing_daily_bar"
        elif event["d2_date"] in daily["actions"]:
            status = "d2_corporate_action"
        elif event["d3_date"] in daily["actions"]:
            status = "d3_corporate_action"
        else:
            d2_quality = minute_quality(d2_minute, d2_bar)
            d3_quality = minute_quality(d3_minute, d3_bar)
            if d2_quality != "ok":
                status = "d2_" + d2_quality
            elif d3_quality != "ok":
                status = "d3_" + d3_quality
        audit[status or "ok"] += 1
        base = {
            **event,
            "d2_open_gap_pct": (
                100 * (float(d2_bar["open"]) / event["d1_close"] - 1)
                if d2_bar else None
            ),
            "d2_first_minute_change_pct": (
                100 * (
                    float(d2_minute["prices"][0])
                    / event["d1_close"]
                    - 1
                )
                if d2_minute and d2_minute.get("prices")
                else None
            ),
        }
        proxy_event = {
            "close": event["d1_close"],
            "board": event["board"],
        }
        for entry_mode in ("early", "breakout", "reclaim"):
            for protect_d1_close in (True, False):
                row = {
                    **base,
                    "entry_mode": entry_mode,
                    "protect_d1_close": protect_d1_close,
                }
                if status:
                    row["buy_status"] = status
                else:
                    row.update(
                        monthly_buy_proxy(
                            proxy_event,
                            d2_bar,
                            d2_minute,
                            entry_mode,
                            model,
                            protect_d1_close=protect_d1_close,
                        )
                    )
                if row["buy_status"] == "filled_proxy":
                    early = sell_proxy(
                        row,
                        d2_bar,
                        d3_bar,
                        d3_minute,
                        event["board"],
                        "early",
                        model,
                    )
                    row.update(
                        {
                            f"early_{key}": value
                            for key, value in early.items()
                        }
                    )
                    for target in (3.0, 5.1, 7.0):
                        target_model = {
                            **model,
                            "target_net_pct": target,
                        }
                        outcome = sell_proxy(
                            row,
                            d2_bar,
                            d3_bar,
                            d3_minute,
                            event["board"],
                            "target",
                            target_model,
                        )
                        prefix = (
                            f"target_{str(target).replace('.', '_')}"
                        )
                        row.update(
                            {
                                f"{prefix}_{key}": value
                                for key, value in outcome.items()
                            }
                        )
                results.append(row)
        if index % 500 == 0:
            LOG.info("replay=%s/%s rows=%s", index, len(candidates), len(results))
    save(output / "replay.json.gz", results)
    save(output / "replay-audit.json", dict(audit))
    return results


def _months(start: str, end: str) -> list[str]:
    start_year, start_month = map(int, start[:7].split("-"))
    end_year, end_month = map(int, end[:7].split("-"))
    result = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return result


def research_rules() -> Iterable[dict[str, Any]]:
    index = 0
    for values in product(
        (5.0, 7.0, 9.0),
        (100_000_000.0, 500_000_000.0),
        (0.5, 0.9),
        (20.0, None),
        ((-3.0, 3.0), (0.0, 5.0), (2.0, 7.0)),
        (0.0, 3.0, 6.0),
        ("early", "breakout", "reclaim"),
        (True, False),
        ("early", "target_3_0", "target_5_1", "target_7_0"),
        (1, 3, 5),
        ("change", "amount", "score"),
    ):
        index += 1
        yield {
            "rule_id": f"M{index:05d}",
            "minimum_d1_change_pct": values[0],
            "minimum_d1_amount_cny": values[1],
            "minimum_d1_close_location": values[2],
            "maximum_d1_return_5d_pct": values[3],
            "d2_open_gap_pct": list(values[4]),
            "minimum_d2_first_minute_change_pct": values[5],
            "entry_mode": values[6],
            "protect_d1_close": values[7],
            "exit_mode": values[8],
            "maximum_daily_entries": values[9],
            "rank_by": values[10],
        }


def _matches(row: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    if (
        row["entry_mode"] != rule["entry_mode"]
        or row["protect_d1_close"] != rule["protect_d1_close"]
        or row.get("buy_status") != "filled_proxy"
        or row["d1_change_pct"] < rule["minimum_d1_change_pct"]
        or row["d1_amount_cny"] < rule["minimum_d1_amount_cny"]
        or row["d1_close_location"]
        < rule["minimum_d1_close_location"]
        or row["d2_first_minute_change_pct"]
        < rule["minimum_d2_first_minute_change_pct"]
    ):
        return False
    maximum = rule["maximum_d1_return_5d_pct"]
    if maximum is not None and (
        row["d1_return_5d_pct"] is None
        or row["d1_return_5d_pct"] > maximum
    ):
        return False
    low, high = rule["d2_open_gap_pct"]
    return low <= row["d2_open_gap_pct"] <= high


def _rank_key(row: Mapping[str, Any], rank_by: str):
    field = {
        "change": "d1_change_pct",
        "amount": "d1_amount_cny",
        "score": "d1_composite_score",
    }[rank_by]
    return (-float(row[field]), row["symbol"])


def portfolio_summary(
    rows: Sequence[Mapping[str, Any]],
    rule: Mapping[str, Any],
    months: Sequence[str],
) -> dict[str, Any]:
    allowed_months = set(months)
    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            row["d3_date"][:7] in allowed_months
            and _matches(row, rule)
        ):
            by_day[row["d2_date"]].append(row)
    daily_returns: dict[str, list[float]] = defaultdict(list)
    trades = []
    exit_prefix = rule["exit_mode"]
    return_key = f"{exit_prefix}_net_return_pct"
    for day, candidates in by_day.items():
        ordered = sorted(
            candidates,
            key=lambda row: _rank_key(row, rule["rank_by"]),
        )
        for row in ordered[: rule["maximum_daily_entries"]]:
            value = row.get(return_key)
            if value is None:
                value = -10.0 if row["board"] == "main" else -20.0
            daily_returns[row["d3_date"]].append(float(value))
            trades.append(
                {
                    "d2_date": row["d2_date"],
                    "d3_date": row["d3_date"],
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "board": row["board"],
                    "return_pct": value,
                }
            )

    month_values = []
    overall_nav = 1.0
    peak = 1.0
    maximum_drawdown = 0.0
    for month in months:
        month_nav = 1.0
        for day in sorted(
            value for value in daily_returns if value.startswith(month)
        ):
            day_return = 0.2 * sum(daily_returns[day]) / 100
            month_nav *= max(0.0, 1 + day_return)
            overall_nav *= max(0.0, 1 + day_return)
            peak = max(peak, overall_nav)
            maximum_drawdown = min(
                maximum_drawdown,
                overall_nav / peak - 1 if peak else -1,
            )
        month_values.append(100 * (month_nav - 1))
    target = 5.0
    hits = sum(value >= target for value in month_values)
    return {
        "months": len(months),
        "hit_count": hits,
        "hit_rate": hits / len(months) if months else None,
        "mean_monthly_return_pct": mean(month_values) if month_values else None,
        "worst_month_pct": min(month_values) if month_values else None,
        "compounded_return_pct": 100 * (overall_nav - 1),
        "maximum_drawdown_pct": 100 * maximum_drawdown,
        "trade_count": len(trades),
        "monthly_returns_pct": month_values,
        "_trades": trades,
    }


def _public_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (
            round(item, 4)
            if isinstance(item, float)
            else item
        )
        for key, item in value.items()
        if not key.startswith("_")
    }


def search(output: Path) -> Mapping[str, Any]:
    protocol = load(DEFAULT_PROTOCOL)
    rows = load(output / "replay.json.gz")
    train_months = _months(*protocol["selection"]["train"])
    validation_months = _months(*protocol["selection"]["validation"])
    holdout_months = _months(*protocol["selection"]["holdout"])
    train_month_set = set(train_months)
    validation_month_set = set(validation_months)
    holdout_month_set = set(holdout_months)
    train_rows = [
        row for row in rows if row["d3_date"][:7] in train_month_set
    ]
    validation_rows = [
        row
        for row in rows
        if row["d3_date"][:7] in validation_month_set
    ]
    holdout_rows = [
        row for row in rows if row["d3_date"][:7] in holdout_month_set
    ]

    train_results = []
    for index, rule in enumerate(research_rules(), 1):
        performance = portfolio_summary(train_rows, rule, train_months)
        train_results.append(
            {
                "rule": rule,
                "train": _public_summary(performance),
            }
        )
        if index % 2500 == 0:
            LOG.info("search train rules=%s", index)
    train_results.sort(
        key=lambda item: (
            -item["train"]["hit_rate"],
            -item["train"]["mean_monthly_return_pct"],
            -item["train"]["worst_month_pct"],
            -item["train"]["maximum_drawdown_pct"],
            item["rule"]["rule_id"],
        )
    )
    shortlist = train_results[:100]
    for item in shortlist:
        item["validation"] = _public_summary(
            portfolio_summary(
                validation_rows,
                item["rule"],
                validation_months,
            )
        )
    shortlist.sort(
        key=lambda item: (
            -item["validation"]["hit_rate"],
            -item["train"]["hit_rate"],
            -item["validation"]["mean_monthly_return_pct"],
            -item["train"]["mean_monthly_return_pct"],
            item["rule"]["rule_id"],
        )
    )
    minimum_train = protocol["selection"]["minimum_train_month_hit_rate"]
    minimum_validation = protocol["selection"][
        "minimum_validation_month_hit_rate"
    ]
    qualified = [
        item for item in shortlist
        if item["train"]["hit_rate"] >= minimum_train
        and item["validation"]["hit_rate"] >= minimum_validation
    ]
    selected = qualified[0] if qualified else shortlist[0]
    holdout = portfolio_summary(
        holdout_rows,
        selected["rule"],
        holdout_months,
    )
    all_months = train_months + validation_months + holdout_months
    all_result = portfolio_summary(rows, selected["rule"], all_months)
    decision = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "qualified"
            if qualified
            else "no_rule_met_80pct_in_train_and_validation"
        ),
        "protocol_sha256": sha256(DEFAULT_PROTOCOL),
        "replay_sha256": sha256(output / "replay.json.gz"),
        "rules_tested": len(train_results),
        "qualified_rules": len(qualified),
        "selection": {
            **selected,
            "holdout": _public_summary(holdout),
            "all": _public_summary(all_result),
        },
        "shortlist": shortlist[:20],
    }
    save(output / "selection.json", decision)
    csv_save(output / "selected-trades.csv", all_result["_trades"])
    csv_save(
        output / "selected-months.csv",
        [
            {
                "month": month,
                "stage": (
                    "train"
                    if month in train_months
                    else "validation"
                    if month in validation_months
                    else "holdout"
                ),
                "return_pct": value,
                "target_met": value >= 5,
            }
            for month, value in zip(
                all_months,
                all_result["monthly_returns_pct"],
            )
        ],
    )
    return decision


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "collect-daily",
            "build-candidates",
            "collect-minutes",
            "replay",
            "search",
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--legacy-cache",
        action="append",
        type=Path,
        default=[],
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.command == "collect-daily":
        collect_daily(args.output)
    elif args.command == "build-candidates":
        build_candidates(args.output)
    elif args.command == "collect-minutes":
        collect_minutes(args.output, args.legacy_cache)
    elif args.command == "replay":
        replay(args.output)
    else:
        search(args.output)


if __name__ == "__main__":
    main()
