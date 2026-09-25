"""Execution-aware comparison of materialized strategy plans."""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from .adapters.postgres import PostgresStorage
from .mootdx_provider import DEFAULT_SERVERS, MootdxProvider, _at_price_limit, _limit_price, _minute_label
from .research_data import write_json
from .storage_config import StorageSettings


METRIC = {
    "name": "严格可买成功率",
    "formula": "严格可买入候选数 / 分钟数据完整的计划候选数",
    "buyable_formula": "严格可买入且次日收盘封板数 / 严格可买入数",
    "strict_buyable": [
        "次日开盘涨幅位于计划竞价区间",
        "首次入场确认前未跌破昨收",
        "10:00 前先触及涨停确认",
        "确认后开板并出现有成交量的非涨停分钟，证明存在可买窗口",
        "10:00 前再次触及涨停，且炸板次数不超过计划上限",
    ],
    "limitation": (
        "mootdx 历史分钟线没有逐笔委托、买卖队列和板块分钟成分数据；"
        "结果是保守的分钟级可买入代理，不等同券商真实成交。"
    ),
}


def rate(numerator: int, denominator: int) -> Optional[float]:
    return round(100 * numerator / denominator, 2) if denominator else None


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def _daily_bar(snapshot: Mapping[str, Any], symbol: str, trade_date: str):
    for bar in snapshot.get("histories", {}).get(symbol, []):
        if str(bar.get("datetime", ""))[:10] == trade_date:
            return bar
    return None


def _daily_bar_from_minutes(minute: Optional[Mapping[str, Sequence[Any]]]):
    prices = list((minute or {}).get("prices") or [])
    if len(prices) != 240 or any(not _finite_positive(value) for value in prices):
        return None
    return {
        "open": float(prices[0]),
        "high": float(max(prices)),
        "low": float(min(prices)),
        "close": float(prices[-1]),
    }


def _cutoff_index(value: str) -> int:
    hour, minute = map(int, value.split(":")[:2])
    return max(0, min(240, hour * 60 + minute - (9 * 60 + 31)))


def classify_candidate(
    candidate: Mapping[str, Any],
    trade_date: str,
    bar: Optional[Mapping[str, Any]],
    minute: Optional[Mapping[str, Sequence[Any]]],
) -> dict[str, Any]:
    """Conservatively prove a post-confirmation buy window from minute closes."""
    symbol = str(candidate["code"])
    reference = float(candidate["latest_price"])
    plan = candidate["plan"]
    result = {
        "trade_date": trade_date,
        "symbol": symbol,
        "name": candidate.get("name", ""),
        "industry": candidate.get("industry", ""),
        "rank": candidate.get("rank"),
        "score": candidate.get("score"),
        "reference_close": reference,
        "status": "missing",
        "reason_code": "missing_daily",
        "reason": "次日日线缺失或无效",
        "auction_qualified": None,
        "minute_complete": False,
        "first_touch_time": None,
        "buy_window_time": None,
        "confirmation_time": None,
        "break_count_before_cutoff": None,
        "buyable": False,
        "closed_limit_up": None,
        "success": False,
    }
    if not bar or any(not _finite_positive(bar.get(key)) for key in ("open", "high", "low", "close")):
        return result

    limit_price = _limit_price(reference, str(candidate.get("name", "")))
    opening_pct = round(100 * (float(bar["open"]) / reference - 1), 6)
    closed_limit = _at_price_limit(bar["close"], limit_price)
    result.update(
        status="observed",
        reason_code="minute_missing",
        reason="分钟线不足 240 根，无法证明可买入",
        open=float(bar["open"]),
        high=float(bar["high"]),
        low=float(bar["low"]),
        close=float(bar["close"]),
        open_change_pct=round(opening_pct, 3),
        limit_price=limit_price,
        closed_limit_up=closed_limit,
    )
    prices = list((minute or {}).get("prices") or [])
    volumes = list((minute or {}).get("volumes") or [])
    if len(prices) != 240 or len(volumes) != 240:
        return result
    result["minute_complete"] = True

    if not plan["open_min_pct"] <= opening_pct <= plan["open_max_pct"]:
        result.update(
            reason_code="auction_outside",
            reason=f"开盘涨幅 {opening_pct:+.2f}% 不在竞价区间",
            auction_qualified=False,
        )
        return result
    result["auction_qualified"] = True

    cutoff = _cutoff_index(str(plan.get("entry_cutoff_time", "10:00")))
    window_prices = prices[:cutoff]
    window_volumes = volumes[:cutoff]
    touch_indices = [
        index for index, price in enumerate(window_prices)
        if _at_price_limit(price, limit_price)
    ]
    if not touch_indices:
        result.update(reason_code="no_early_touch", reason="10:00 前未触及涨停确认")
        return result
    first_touch = touch_indices[0]
    result["first_touch_time"] = _minute_label(first_touch)

    breach = next(
        (index for index, price in enumerate(window_prices[: first_touch + 1])
         if _finite_positive(price) and float(price) < reference - 0.001),
        None,
    )
    if breach is not None and plan.get("reject_below_previous_close", True):
        result.update(
            reason_code="breached_reference",
            reason=f"{_minute_label(breach)} 已跌破昨收，按计划永久放弃",
        )
        return result

    buy_index = next(
        (
            index for index in range(first_touch + 1, len(window_prices))
            if _finite_positive(window_prices[index])
            and float(window_prices[index]) < limit_price - 0.001
            and _finite_positive(window_volumes[index])
            and float(window_prices[index]) >= reference - 0.001
        ),
        None,
    )
    if buy_index is None:
        result.update(
            reason_code="no_proven_fill_window",
            reason="触板后 10:00 前无有量开板分钟，无法证明排队可成交",
        )
        return result
    result["buy_window_time"] = _minute_label(buy_index)

    confirmation = next(
        (
            index for index in range(buy_index + 1, len(window_prices))
            if _at_price_limit(window_prices[index], limit_price)
        ),
        None,
    )
    if confirmation is None:
        result.update(reason_code="no_reseal", reason="出现可买窗口后 10:00 前未回封")
        return result
    result["confirmation_time"] = _minute_label(confirmation)

    flags = [_at_price_limit(price, limit_price) for price in window_prices[: confirmation + 1]]
    breaks = sum(flags[index - 1] and not flags[index] for index in range(1, len(flags)))
    result["break_count_before_cutoff"] = breaks
    maximum_breaks = int(plan.get("manual_max_intraday_breaks", 1))
    if breaks > maximum_breaks:
        result.update(
            reason_code="too_many_breaks",
            reason=f"10:00 前炸板 {breaks} 次，超过计划上限 {maximum_breaks} 次",
        )
        return result

    result.update(
        reason_code="success" if closed_limit else "buyable_but_failed",
        reason="可买后收盘封板" if closed_limit else "已证明可买，但收盘未封板",
        buyable=True,
        success=bool(closed_limit),
    )
    return result


def summarize_stocks(stocks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    verified = [row for row in stocks if row.get("minute_complete")]
    buyable = [row for row in verified if row.get("buyable")]
    successes = [row for row in buyable if row.get("success")]
    return {
        "candidate_count": len(stocks),
        "verified_count": len(verified),
        "missing_count": len(stocks) - len(verified),
        "buyable_count": len(buyable),
        "execution_success_count": len(buyable),
        "execution_success_rate_pct": rate(len(buyable), len(verified)),
        "success_count": len(successes),
        "success_rate_pct": rate(len(successes), len(verified)),
        "buyable_rate_pct": rate(len(buyable), len(verified)),
        "buyable_close_rate_pct": rate(len(successes), len(buyable)),
        "success_stocks": [
            {
                key: row.get(key)
                for key in ("trade_date", "symbol", "name", "industry", "score",
                            "buy_window_time", "confirmation_time")
            }
            for row in successes
        ],
    }


def aggregate_periods(days: Sequence[Mapping[str, Any]], frequency: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for day in days:
        value = date.fromisoformat(str(day["trade_date"]))
        if frequency == "day":
            key = value.isoformat()
        elif frequency == "week":
            year, week, _ = value.isocalendar()
            key = f"{year}-W{week:02d}"
        elif frequency == "month":
            key = value.strftime("%Y-%m")
        elif frequency == "year":
            key = value.strftime("%Y")
        else:
            raise ValueError(f"unsupported frequency: {frequency}")
        groups[key].extend(day["stocks"])
    return [{"period": key, **summarize_stocks(rows)} for key, rows in sorted(groups.items())]


def build_strategy_result(
    strategy: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    minutes: Mapping[str, Mapping[str, Sequence[Any]]],
    start: date,
    end: date,
) -> dict[str, Any]:
    days = []
    for record in sorted(records, key=lambda row: row["trade_date"]):
        trade_date = str(record["trade_date"])
        if not start.isoformat() <= trade_date <= end.isoformat():
            continue
        plan = record.get("execution_plan")
        if not plan:
            continue
        config = plan.get("strategy_config") or {}
        default_rules = {
            "open_min_pct": config.get("entry_open_min_pct", 0.5),
            "open_max_pct": config.get("entry_open_max_pct", 5.0),
            "entry_cutoff_time": config.get("entry_cutoff_time", "10:00"),
            "manual_max_intraday_breaks": config.get("manual_max_intraday_breaks", 1),
            "reject_below_previous_close": config.get("reject_below_previous_close", True),
        }
        stocks = [
            classify_candidate(
                candidate if candidate.get("plan") else {**candidate, "plan": default_rules},
                trade_date,
                _daily_bar(snapshot, str(candidate["code"]), trade_date)
                or _daily_bar_from_minutes(minutes.get(f"{trade_date}:{candidate['code']}")),
                minutes.get(f"{trade_date}:{candidate['code']}"),
            )
            for candidate in plan.get("candidates", [])
        ]
        days.append({
            "trade_date": trade_date,
            "reference_date": plan.get("as_of"),
            "stocks": stocks,
            "summary": summarize_stocks(stocks),
        })
    all_stocks = [stock for day in days for stock in day["stocks"]]
    return {
        "strategy_id": strategy["strategy_id"],
        "strategy_code": strategy["code"],
        "strategy_name": strategy["name"],
        "enabled": strategy["enabled"],
        "summary": summarize_stocks(all_stocks),
        "periods": {
            frequency: aggregate_periods(days, frequency)
            for frequency in ("day", "week", "month", "year")
        },
        "days": days,
    }


def required_pairs(
    records_by_strategy: Mapping[str, Sequence[Mapping[str, Any]]],
    start: date,
    end: date,
) -> list[tuple[str, str]]:
    pairs = set()
    for records in records_by_strategy.values():
        for record in records:
            trade_date = str(record["trade_date"])
            if not start.isoformat() <= trade_date <= end.isoformat():
                continue
            for candidate in (record.get("execution_plan") or {}).get("candidates", []):
                pairs.add((trade_date, str(candidate["code"])))
    return sorted(pairs)


def load_daily_snapshot(
    paths: Sequence[Path],
    pairs: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    required = defaultdict(set)
    for trade_date, symbol in pairs:
        required[symbol].add(trade_date)
    histories: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for path in paths:
        snapshot = json.loads(path.read_text())
        for symbol, dates in required.items():
            for bar in snapshot.get("histories", {}).get(symbol, []):
                trade_date = str(bar.get("datetime", ""))[:10]
                key = (symbol, trade_date)
                if trade_date in dates and key not in seen:
                    histories[symbol].append(bar)
                    seen.add(key)
    return {"histories": dict(histories)}


def load_strategy_records(repository: PostgresStorage, strategy_id: str) -> list[dict[str, Any]]:
    summaries = repository.list_strategy_days(strategy_id, 1000)
    return [
        detail
        for row in summaries
        if (detail := repository.get_strategy_day(strategy_id, row["trade_date"])) is not None
    ]


def _fetch_partition(
    server: tuple[str, int],
    pairs: Sequence[tuple[str, str]],
) -> tuple[dict[str, dict[str, list[Any]]], list[tuple[str, str]]]:
    provider = MootdxProvider(servers=(server,))
    client = None
    results: dict[str, dict[str, list[Any]]] = {}
    failed = []
    try:
        client = provider._client(server)
        for trade_date, symbol in pairs:
            try:
                frame = client.minutes(symbol=symbol, date=trade_date.replace("-", ""))
                if frame is None or frame.empty or len(frame) != 240:
                    failed.append((trade_date, symbol))
                    continue
                prices = [float(value) for value in frame["price"].tolist()]
                volumes = [float(value) for value in frame["vol"].tolist()]
                results[f"{trade_date}:{symbol}"] = {"prices": prices, "volumes": volumes}
            except Exception:
                failed.append((trade_date, symbol))
    finally:
        if client is not None:
            provider._close(client)
    return results, failed


def collect_minutes(
    pairs: Iterable[tuple[str, str]],
    cache_path: Path,
    *,
    workers: int = 7,
    batch_size: int = 140,
    servers: Sequence[tuple[str, int]] = DEFAULT_SERVERS,
) -> dict[str, dict[str, list[Any]]]:
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    missing = [pair for pair in pairs if len(cache.get(f"{pair[0]}:{pair[1]}", {}).get("prices", [])) != 240]
    for offset in range(0, len(missing), batch_size):
        pending = missing[offset : offset + batch_size]
        for attempt in range(3):
            if not pending:
                break
            count = min(workers, len(servers), len(pending))
            selected = [servers[(index + attempt) % len(servers)] for index in range(count)]
            partitions = [pending[index::count] for index in range(count)]
            failures = []
            with ThreadPoolExecutor(max_workers=count) as executor:
                futures = [
                    executor.submit(_fetch_partition, server, partition)
                    for server, partition in zip(selected, partitions)
                ]
                for future in futures:
                    values, failed = future.result()
                    cache.update(values)
                    failures.extend(failed)
            pending = failures
        write_json(cache_path, cache)
        completed = min(offset + batch_size, len(missing))
        print(f"分钟线 {completed}/{len(missing)}，当前完整 {len(cache)}，本批失败 {len(pending)}", flush=True)
    return cache


def _period_markdown(strategy: Mapping[str, Any], frequency: str, title: str) -> list[str]:
    lines = [f"### {title}", "", "| 周期 | 候选 | 可买成功 | 收盘封板 | 可买成功率 | 可买后封板率 | 封板股票 |",
             "|---|---:|---:|---:|---:|---:|---|"]
    for row in strategy["periods"][frequency]:
        stocks = "、".join(
            f"{item['name']}({item['symbol']})" for item in row["success_stocks"]
        ) or "-"
        success_rate = (
            f"{row['execution_success_rate_pct']}%"
            if row["execution_success_rate_pct"] is not None else "-"
        )
        close_rate = (
            f"{row['buyable_close_rate_pct']}%"
            if row["buyable_close_rate_pct"] is not None else "-"
        )
        lines.append(
            f"| {row['period']} | {row['verified_count']} | {row['buyable_count']} | "
            f"{row['success_count']} | {success_rate} | {close_rate} | {stocks} |"
        )
    return lines + [""]


def write_reports(result: Mapping[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "report.json", result)
    fields = [
        "strategy_name", "trade_date", "symbol", "name", "industry", "rank", "score",
        "open_change_pct", "first_touch_time", "buy_window_time", "confirmation_time",
        "break_count_before_cutoff", "buyable", "closed_limit_up", "success",
        "reason_code", "reason",
    ]
    with (output / "stocks.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for strategy in result["strategies"]:
            for day in strategy["days"]:
                for stock in day["stocks"]:
                    writer.writerow({**stock, "strategy_name": strategy["strategy_name"]})

    lines = [
        "# 双策略实盘可执行性对比",
        "",
        f"- 区间：{result['start']} 至 {result['end']}",
        "- 数据源：mootdx",
        f"- 主指标：{METRIC['formula']}",
        f"- 可买后封板率：{METRIC['buyable_formula']}",
        f"- 限制：{METRIC['limitation']}",
        "",
        "## 总览",
        "",
        "| 策略 | 候选 | 完整分钟 | 可买成功 | 收盘封板 | 可买成功率 | 可买后封板率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in result["strategies"]:
        summary = strategy["summary"]
        lines.append(
            f"| {strategy['strategy_name']} | {summary['candidate_count']} | {summary['verified_count']} | "
            f"{summary['buyable_count']} | {summary['success_count']} | "
            f"{summary['execution_success_rate_pct'] if summary['execution_success_rate_pct'] is not None else '-'}% | "
            f"{summary['buyable_close_rate_pct'] if summary['buyable_close_rate_pct'] is not None else '-'}% |"
        )
    lines.append("")
    for strategy in result["strategies"]:
        lines.extend([f"## {strategy['strategy_name']}", ""])
        lines.extend([
            "### 严格可买股票明细",
            "",
            "| 日期 | 股票 | 题材 | 评分 | 可买窗口 | 回封确认 | 收盘结果 |",
            "|---|---|---|---:|---|---|---|",
        ])
        buyable = [
            stock for day in strategy["days"] for stock in day["stocks"] if stock["buyable"]
        ]
        for stock in buyable:
            lines.append(
                f"| {stock['trade_date']} | {stock['name']}({stock['symbol']}) | "
                f"{stock['industry']} | {stock['score']} | {stock['buy_window_time']} | "
                f"{stock['confirmation_time']} | {'成功封板' if stock['success'] else '收盘未封板'} |"
            )
        if not buyable:
            lines.append("| - | - | - | - | - | - | 无严格可买样本 |")
        lines.append("")
        for frequency, title in (("year", "按年"), ("month", "按月"), ("week", "按周"), ("day", "按日")):
            lines.extend(_period_markdown(strategy, frequency, title))
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def analyze(
    repository: PostgresStorage,
    snapshot: Mapping[str, Any],
    minutes: Mapping[str, Mapping[str, Sequence[Any]]],
    start: date,
    end: date,
) -> dict[str, Any]:
    strategies = repository.list_strategies()
    records = {
        strategy["strategy_id"]: load_strategy_records(repository, strategy["strategy_id"])
        for strategy in strategies
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source": "mootdx",
        "metric": METRIC,
        "strategies": [
            build_strategy_result(
                strategy, records[strategy["strategy_id"]], snapshot, minutes, start, end,
            )
            for strategy in strategies
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="按日、周、月、年比较策略的严格可执行成功率")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--snapshot", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=7)
    args = parser.parse_args(argv)
    if args.start > args.end:
        raise ValueError("start must be on or before end")

    settings = StorageSettings.from_env()
    repository = PostgresStorage(settings.postgres_dsn)
    try:
        strategies = repository.list_strategies()
        records = {
            item["strategy_id"]: load_strategy_records(repository, item["strategy_id"])
            for item in strategies
        }
        pairs = required_pairs(records, args.start, args.end)
        snapshot = load_daily_snapshot(args.snapshot, pairs)
        minutes = collect_minutes(pairs, args.output / "minute-cache.json", workers=args.workers)
        result = analyze(repository, snapshot, minutes, args.start, args.end)
    finally:
        repository.close()
    write_reports(result, args.output)
    repository = PostgresStorage(settings.postgres_dsn)
    try:
        comparison_id = repository.replace_execution_comparison(result)
    finally:
        repository.close()
    print(json.dumps({
        "output": str(args.output),
        "comparison_id": comparison_id,
        "required_pairs": len(pairs),
        "minute_pairs": len(minutes),
        "strategies": [
            {
                "name": item["strategy_name"],
                **{
                    key: value
                    for key, value in item["summary"].items()
                    if key != "success_stocks"
                },
            }
            for item in result["strategies"]
        ],
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
