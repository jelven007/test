#!/usr/bin/env python3
"""Reproducible mootdx-only T+1 research. Raw responses stay outside Git."""
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
from datetime import date, datetime, timezone
from pathlib import Path

from banxia_strategy.limit_up_collector import daily_bars
from banxia_strategy.mootdx_provider import MootdxProvider, _bar_date, _json_value
from banxia_strategy.t1_research import (
    buy_proxy, cents, cluster_interval, matches, minute_quality, net_return,
    rules, sell_proxy, split_name, summary,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "research/limit-up-t1-2026"
LOG = logging.getLogger("t1-research")


def load(path):
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt") as stream:
            return json.load(stream)
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    if str(path).endswith(".gz"):
        with gzip.open(temp, "wt") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    else:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    temp.replace(path)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def events_and_calendar(output):
    snapshot = load(output / "postgres-snapshot.json")
    calendar = sorted(row["trade_date"] for row in snapshot["coverage"])
    positions = {day: i for i, day in enumerate(calendar)}
    events = []
    for row in snapshot["events"]:
        if row["raw"].get("limit_rule_basis") == "unverified_5pct_candidate":
            continue
        row = dict(row)
        pos = positions[row["trade_date"]]
        row["buy_date"] = calendar[pos + 1] if pos + 1 < len(calendar) else None
        row["sell_date"] = calendar[pos + 2] if pos + 2 < len(calendar) else None
        events.append(row)
    return events, calendar


def collect(output, legacy_cache=None):
    events, calendar = events_and_calendar(output)
    securities = {r["symbol"]: r for r in events if r["sell_date"]}
    provider = MootdxProvider()
    symbols = sorted(securities)
    if legacy_cache:
        legacy = load(legacy_cache)
        for key, minute in legacy.items():
            day, symbol = key.split(":")
            if day < "2026-01-01" or symbol not in securities:
                continue
            path = output / "minutes" / f"{day}_{symbol}.json.gz"
            if not path.exists() and len(minute.get("prices", [])) == 240:
                save(path, {**minute, "source": "mootdx_legacy_cache",
                            "symbol": symbol, "date": day})
        LOG.info("legacy cache imported")

    def worker(number, partition):
        client = None
        active_server = None
        counts = Counter()

        def request(action):
            nonlocal client, active_server
            errors = []
            for attempt in range(3):
                server = provider.servers[(number + attempt) % len(provider.servers)]
                try:
                    if client is None or active_server != server:
                        if client is not None:
                            provider._close(client)
                        client, active_server = provider._client(server), server
                    return action(client)
                except Exception as exc:
                    errors.append(str(exc))
                    if client is not None:
                        provider._close(client)
                    client = None
            raise RuntimeError("; ".join(errors))

        for pos, symbol in enumerate(partition):
            daily_path = output / "daily" / f"{symbol}.json.gz"
            try:
                if not daily_path.exists():
                    def get_daily(c):
                        bars = daily_bars(c, symbol, date(2026, 1, 1))
                        actions = c.client.get_xdxr_info(
                            1 if securities[symbol]["exchange"] == "sh" else 0, symbol)
                        if actions is None or not bars:
                            raise ValueError("empty daily or missing action response")
                        return {"symbol": symbol, "bars": _json_value(bars),
                                "actions": _json_value(actions), "source": "mootdx",
                                "collected_at": datetime.now(timezone.utc).isoformat()}
                    save(daily_path, request(get_daily))
                daily = load(daily_path)
                bar_days = {_bar_date(b).isoformat() for b in daily["bars"]}
                needed = sorted({r[field] for r in events if r["symbol"] == symbol
                                 and r["sell_date"] for field in ("buy_date", "sell_date")})
                for day in needed:
                    if day not in bar_days:
                        counts["no_daily_session"] += 1
                        continue
                    path = output / "minutes" / f"{day}_{symbol}.json.gz"
                    if path.exists():
                        counts["cached"] += 1
                        continue
                    try:
                        def get_minutes(c):
                            frame = c.minutes(symbol=symbol, date=day.replace("-", ""))
                            if frame is None or len(frame) != 240:
                                raise ValueError(f"expected 240 minute samples, got {None if frame is None else len(frame)}")
                            prices = [float(v) for v in frame["price"]]
                            volumes = [float(v) for v in frame["vol"]]
                            if not all(math.isfinite(v) for v in prices + volumes):
                                raise ValueError("non-finite minute sample")
                            return {"date": day, "symbol": symbol, "prices": prices,
                                    "volumes": volumes, "source": "mootdx",
                                    "collected_at": datetime.now(timezone.utc).isoformat()}
                        save(path, request(get_minutes))
                        counts["downloaded"] += 1
                    except Exception as exc:
                        counts["minute_errors"] += 1
                        LOG.warning("minute %s %s: %s", symbol, day, exc)
                counts["symbols"] += 1
            except Exception as exc:
                counts["daily_errors"] += 1
                LOG.warning("daily %s: %s", symbol, exc)
            if (pos + 1) % 20 == 0:
                LOG.info("worker=%s symbols=%s/%s counts=%s", number, pos + 1, len(partition), dict(counts))
        if client is not None:
            provider._close(client)
        return counts

    counts = Counter()
    workers = len(provider.servers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i, symbols[i::workers]) for i in range(workers)]
        for future in as_completed(futures):
            counts.update(future.result())
    manifest = {
        "extracted_at": load(output / "postgres-snapshot.json")["extracted_at"],
        "snapshot_sha256": sha256(output / "postgres-snapshot.json"),
        "protocol_sha256": sha256(ROOT / "docs/research/2026-limit-up-protocol.json"),
        "events": len(events), "completed_events": sum(bool(e["sell_date"]) for e in events),
        "calendar_end": calendar[-1], "symbols": len(symbols), "counts": dict(counts),
    }
    save(output / "collection-manifest.json", manifest)
    LOG.info("completed %s", manifest)


def csv_save(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def replay(output):
    protocol = load(ROOT / "docs/research/2026-limit-up-protocol.json")
    manifest = load(output / "collection-manifest.json")
    if manifest["snapshot_sha256"] != sha256(output / "postgres-snapshot.json"):
        raise ValueError("Source snapshot changed after collection")
    model = protocol["execution"]
    events, _ = events_and_calendar(output)
    by_symbol, results, audit = defaultdict(list), [], Counter()
    for event in events:
        by_symbol[event["symbol"]].append(event)
    for stock_index, (symbol, stock_events) in enumerate(sorted(by_symbol.items()), 1):
        path = output / "daily" / f"{symbol}.json.gz"
        daily = load(path) if path.exists() else {"bars": [], "actions": []}
        bars = {_bar_date(b).isoformat(): b for b in daily["bars"]}
        changes = {_bar_date(a).isoformat() for a in daily["actions"]
                   if int(a.get("category") or 0) in (1, 11, 12)}
        minute_cache = {}
        for event in stock_events:
            base = {k: v for k, v in event.items() if k not in ("raw",)}
            base["capital_basis"] = event["raw"].get("capital_basis")
            base["industry_basis"] = event["raw"].get("classification_basis")
            base["d2_open_gap_pct"] = None
            status = None
            d2, d3 = event["buy_date"], event["sell_date"]
            if not d3:
                status = "right_censored_no_d3"
            elif not daily["bars"]:
                status = "missing_daily_history"
            elif d2 not in bars:
                status = "no_d2_daily_bar"
            elif d2 in changes:
                status = "d2_corporate_action_unscorable"
            else:
                base["d2_open_gap_pct"] = (bars[d2]["open"] / event["close"] - 1) * 100
                if abs(bars[event["trade_date"]]["close"] - event["close"]) > 0.011:
                    status = "d1_snapshot_mismatch"
                for day in (d2, d3):
                    if day not in minute_cache:
                        minute_path = output / "minutes" / f"{day}_{symbol}.json.gz"
                        minute_cache[day] = load(minute_path) if minute_path.exists() else None
                    base[f"{'d2' if day == d2 else 'd3'}_minute_quality"] = (
                        minute_quality(minute_cache[day], bars[day]) if day in bars else "no_daily_bar")
                if base["d2_minute_quality"] != "ok":
                    status = "d2_data_" + base["d2_minute_quality"]
            audit["events"] += 1
            audit["base_" + (status or "ok")] += 1
            for field in ("d2_minute_quality", "d3_minute_quality"):
                if field in base:
                    audit[field + "_" + base[field]] += 1
            for mode in model["entry_modes"]:
                row = {**base, "entry_mode": mode}
                row["split"] = split_name(row, protocol) if d3 else "right_censored"
                if status:
                    row["buy_status"] = status
                else:
                    row.update(buy_proxy(event, bars[d2], minute_cache[d2], mode, model))
                if row["buy_status"] == "filled_proxy":
                    if d3 in bars and d3 not in changes:
                        open_fill = max(0.01, math.floor(
                            bars[d3]["open"] * (1 - model["slippage_each_side"]) * 100 + 1e-9) / 100)
                        row["open_benchmark_net_return_pct"] = net_return(
                            row["buy_price"], open_fill, row["shares"], model)
                        row["hindsight_d3_high_net_return_pct"] = net_return(
                            row["buy_price"], bars[d3]["high"] * (1 - model["slippage_each_side"]),
                            row["shares"], model)
                    for exit_mode in ("early", "target"):
                        if d3 in changes:
                            sale = {"sell_status": "d3_corporate_action_unscorable"}
                        elif base["d3_minute_quality"] != "ok":
                            sale = {"sell_status": "d3_data_" + base["d3_minute_quality"]}
                        else:
                            sale = sell_proxy(row, bars[d2], bars[d3], minute_cache[d3],
                                              row["board"], exit_mode, model)
                        row.update({f"{exit_mode}_{k}": v for k, v in sale.items()})
                results.append(row)
        if stock_index % 500 == 0:
            LOG.info("replay symbols=%s/%s rows=%s", stock_index, len(by_symbol), len(results))
    save(output / "replay.json.gz", results)
    csv_save(output / "all-events.csv", results)
    winners = [r for r in results if any(
        r.get(f"{mode}_net_return_pct", -math.inf) > 5 for mode in ("early", "target"))]
    csv_save(output / "winners.csv", winners)
    stocks = defaultdict(list)
    for row in results:
        if row["entry_mode"] == "early":
            stocks[row["symbol"]].append(row)
    csv_save(output / "stock-summary.csv", [
        {"symbol": symbol, "name": rows[0]["name"], "board": rows[0]["board"],
         "industry": rows[0]["industry"], **summary(rows, "target")}
        for symbol, rows in sorted(stocks.items())])
    save(output / "replay-audit.json", dict(audit))
    LOG.info("replayed rows=%s winners=%s audit=%s", len(results), len(winners), dict(audit))


def select(output):
    protocol = load(ROOT / "docs/research/2026-limit-up-protocol.json")
    rows = load(output / "replay.json.gz")
    # Selection cannot read September outcomes: discard those rows first.
    train = [r for r in rows if r["split"] == "train"]
    validation = [r for r in rows if r["split"] == "validation"]
    train_results = []
    for rule in rules(protocol):
        selected = [r for r in train if matches(r, rule)]
        train_results.append({"rule": rule, "train": summary(selected, rule["exit"])})
    train_results.sort(key=lambda r: (
        -r["train"]["wilson_lower"], -(r["train"]["mean_known_return_pct"] or -100),
        r["rule"]["id"]))
    eligible = [r for r in train_results
                if r["train"]["buys"] >= protocol["selection"]["minimum_train_buys"]]
    shortlist = eligible[:10]
    for result in shortlist:
        result["validation"] = summary(
            [r for r in validation if matches(r, result["rule"])], result["rule"]["exit"])
    candidates = [r for r in shortlist
                  if r["validation"]["buys"] >= protocol["selection"]["minimum_validation_buys"]]
    candidates.sort(key=lambda r: (
        -r["validation"]["wilson_lower"], -r["train"]["wilson_lower"], r["rule"]["id"]))
    selected = candidates[0] if candidates else (shortlist[0] if shortlist else None)
    decision = {
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256(ROOT / "docs/research/2026-limit-up-protocol.json"),
        "replay_sha256": sha256(output / "replay.json.gz"),
        "rules_tested": len(train_results), "eligible_train_rules": len(eligible),
        "qualified_validation_rules": len(candidates),
        "status": "selected" if candidates else "no_qualified_rule_exploratory_only",
        "selection": selected, "shortlist": shortlist,
    }
    save(output / "frozen-selection.json", decision)
    csv_save(output / "training-grid.csv", [
        {"rule_id": item["rule"]["id"], "rule": json.dumps(item["rule"]), **item["train"]}
        for item in train_results])
    LOG.info("selection frozen: %s", json.dumps(decision, ensure_ascii=False))


def analyze(output):
    protocol = load(ROOT / "docs/research/2026-limit-up-protocol.json")
    selection = load(output / "frozen-selection.json")
    if selection["replay_sha256"] != sha256(output / "replay.json.gz"):
        raise ValueError("Replay changed after rule selection")
    if selection["protocol_sha256"] != sha256(ROOT / "docs/research/2026-limit-up-protocol.json"):
        raise ValueError("Protocol changed after rule selection")
    rows = load(output / "replay.json.gz")
    result = {"selection": selection, "audit": load(output / "replay-audit.json")}
    baselines = []
    for board in ("main", "gem", "star"):
        for scope in ("all_limit_ups", "first_board_amount_200m"):
            for entry in protocol["execution"]["entry_modes"]:
                group = [r for r in rows if r["board"] == board and r["entry_mode"] == entry
                         and r["sell_date"] and (scope == "all_limit_ups" or
                         (r["consecutive_limit_days"] == 1 and (r["amount_cny"] or 0) >= 200000000))]
                for mode in ("open_benchmark", "early", "target", "hindsight_d3_high"):
                    baselines.append({"board": board, "scope": scope, "entry": entry,
                                      "exit": mode, **summary(group, mode)})
    result["baselines"] = baselines
    csv_save(output / "baselines.csv", baselines)
    monthly = []
    for month in sorted({r["trade_date"][:7] for r in rows}):
        group = [r for r in rows if r["board"] == "main" and r["entry_mode"] == "early"
                 and r["consecutive_limit_days"] == 1 and (r["amount_cny"] or 0) >= 200000000
                 and r["buy_date"] and r["buy_date"].startswith(month)]
        monthly.append({"month": month, **summary(group, "target")})
    result["monthly"] = monthly
    csv_save(output / "monthly.csv", monthly)
    chosen = selection["selection"]
    if chosen:
        rule = chosen["rule"]
        selected_rows = [r for r in rows if matches(r, rule)]
        result["selected_performance"] = []
        for stage in ("train", "validation", "holdout", "purged", "all"):
            group = [r for r in selected_rows if r["split"] == stage or stage == "all"]
            result["selected_performance"].append({
                "stage": stage, **summary(group, rule["exit"]),
                "date_cluster_95_interval": cluster_interval(group, rule["exit"]),
            })
        csv_save(output / "selected-trades.csv", selected_rows)
        stress = []
        for additional_round_trip_cost_pct in (0, 0.1, 0.2, 0.4):
            group = [r for r in selected_rows if r["buy_status"] == "filled_proxy"]
            wins = sum(r.get(f"{rule['exit']}_net_return_pct", -math.inf)
                       - additional_round_trip_cost_pct > 5 for r in group)
            stress.append({"extra_round_trip_cost_pct": additional_round_trip_cost_pct,
                           "buys": len(group), "successes": wins,
                           "success_rate": wins / len(group) if group else None})
        result["cost_stress_fixed_fills"] = stress
    features = []
    feature_rows = [r for r in rows if r["entry_mode"] == "early" and r["board"] == "main"
                    and r["consecutive_limit_days"] == 1 and (r["amount_cny"] or 0) >= 200000000
                    and r["split"] in ("train", "validation")]
    bins = {
        "d2_open_gap_pct": [-100, 0, 1, 3, 5, 9, 100],
        "turnover_pct": [0, 3, 5, 10, 15, 25, 1000],
        "float_market_cap_cny": [0, 2000000000, 5000000000, 10000000000, 20000000000, 1e15],
        "total_market_cap_cny": [0, 5000000000, 10000000000, 20000000000, 50000000000, 1e15],
        "amplitude_pct": [0, 3, 5, 8, 12, 100],
        "return_5d_pct": [-100, 0, 10, 20, 35, 1000],
        "open_change_pct": [-100, 0, 3, 5, 100],
        "change_pct": [0, 9.9, 10.1, 100],
    }
    for field, boundaries in bins.items():
        for low, high in zip(boundaries, boundaries[1:]):
            group = [r for r in feature_rows if r.get(field) is not None and low <= r[field] < high]
            features.append({"feature": field, "range": f"[{low}, {high})", **summary(group, "target")})
    for industry in sorted({r["industry"] for r in feature_rows}):
        features.append({"feature": "industry_current_classification", "range": industry,
                         **summary([r for r in feature_rows if r["industry"] == industry], "target")})
    result["features_train_validation"] = features
    csv_save(output / "feature-comparison.csv", features)
    result["buy_statuses_by_entry"] = {
        mode: dict(Counter(r["buy_status"] for r in rows if r["entry_mode"] == mode))
        for mode in protocol["execution"]["entry_modes"]
    }
    unique_winners = {(r["trade_date"], r["symbol"]) for r in rows if any(
        r.get(f"{mode}_net_return_pct", -math.inf) > 5 for mode in ("early", "target"))}
    result["retrospective_union_winning_events"] = len(unique_winners)
    result["retrospective_union_winning_symbols"] = len({s for _, s in unique_winners})
    save(output / "data-summary.json", result)
    LOG.info("analysis saved; frozen rule=%s", chosen["rule"] if chosen else None)


def evidence(output):
    """Independent historical transaction corroboration of illustrative events."""
    protocol = load(ROOT / "docs/research/2026-limit-up-protocol.json")
    rows = load(output / "replay.json.gz")
    base = sorted([r for r in rows if r["entry_mode"] == "early" and r["board"] == "main"
                   and r["consecutive_limit_days"] == 1 and (r["amount_cny"] or 0) >= 200000000
                   and r["buy_status"] == "filled_proxy"], key=lambda r: (r["buy_date"], r["symbol"]))
    examples = []
    for month in sorted({r["buy_date"][:7] for r in base}):
        winners = [r for r in base if r["buy_date"].startswith(month)
                   and r.get("target_net_return_pct", -math.inf) > 5]
        if winners:
            examples.append({**winners[0], "example_reason": "first_baseline_target_winner_of_month"})
    losers = [r for r in base if r.get("target_net_return_pct", 0) < -5]
    examples.extend({**r, "example_reason": "first_three_baseline_losses_below_minus5"}
                    for r in losers[:3])
    provider = MootdxProvider()
    proofs = []
    for row in examples:
        proof = dict(row)
        for side, day, clock in (
            ("buy", row["buy_date"], row["buy_time"]),
            ("sell", row["sell_date"], row.get("target_sell_time")),
        ):
            if not clock:
                continue
            path = output / "transactions" / f"{day}_{row['symbol']}.json.gz"
            if not path.exists():
                def fetch(c):
                    pages = []
                    for start in range(0, 25200, 1800):
                        frame = c.transactions(symbol=row["symbol"], date=day.replace("-", ""),
                                               start=start, offset=1800)
                        if frame is None:
                            raise ValueError("missing transaction response")
                        batch = frame.to_dict(orient="records")
                        pages.append({"start": start, "rows": _json_value(batch)})
                        if len(batch) < 1800:
                            return {"pages": pages, "source": "mootdx", "date": day,
                                    "symbol": row["symbol"]}
                    raise ValueError("transaction pagination cap reached")
                save(path, provider._first_result(fetch))
            pages = load(path)["pages"]
            ticks = [t for page in pages for t in page["rows"]
                     if (("09:30" <= t["time"] <= "11:30") or ("13:00" <= t["time"] <= "15:00"))
                     and t["vol"] > 0 and t.get("buyorsell") in (0, 1, 2)]
            hour, minute = (int(x) for x in clock.split(":"))
            bucket_value = hour * 60 + minute - 1
            bucket = f"{bucket_value // 60:02d}:{bucket_value % 60:02d}"
            same_minute = [t for t in ticks if t["time"] == bucket]
            eligible = [t for t in same_minute if (
                row["close"] - 0.001 <= t["price"] <= row["buy_price"] + 0.001
                if side == "buy" else t["price"] >= row["target_sell_price"] - 0.001)]
            total_hands = sum(t["vol"] for t in eligible)
            proof.update({
                f"{side}_transaction_bucket": bucket,
                f"{side}_transaction_records": len(same_minute),
                f"{side}_eligible_transaction_records": len(eligible),
                f"{side}_eligible_transaction_hands": total_hands,
                f"{side}_transaction_capacity_corroborated": (
                    total_hands * 100 * protocol["execution"]["maximum_participation_in_observed_minute_volume"]
                    >= row["shares"]),
            })
        proofs.append(proof)
        LOG.info("transaction evidence %s %s buy=%s sell=%s", row["symbol"], row["buy_date"],
                 proof.get("buy_transaction_capacity_corroborated"),
                 proof.get("sell_transaction_capacity_corroborated"))
    csv_save(output / "transaction-evidence-examples.csv", proofs)
    save(output / "transaction-evidence-examples.json", proofs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "replay", "select", "analyze", "evidence"])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--legacy-cache", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.command == "collect":
        collect(args.output, args.legacy_cache)
    else:
        {"replay": replay, "select": select, "analyze": analyze, "evidence": evidence}[args.command](args.output)


if __name__ == "__main__":
    main()
