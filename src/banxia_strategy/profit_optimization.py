"""Chronological, execution-aware optimization for T+1 opening returns."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from .adapters.postgres import PostgresStorage
from .execution_analysis import classify_candidate
from .research_data import write_json
from .storage_config import StorageSettings
from .strategy_config import StrategyConfig, revision_for


ROUND_TRIP_COST_BPS = 25
MIN_TRAIN_TRADES = 5
MIN_VALIDATION_TRADES = 2


@dataclass(frozen=True)
class TrialSpec:
    minimum_score: float
    entry_cutoff_time: str

    @property
    def trial_id(self) -> str:
        return f"score-{self.minimum_score:g}-cutoff-{self.entry_cutoff_time.replace(':', '')}"


def chronological_split(trade_dates: Sequence[str]) -> dict[str, list[str]]:
    dates = sorted(set(trade_dates))
    if len(dates) < 15:
        raise ValueError("至少需要 15 个交易日才能划分训练、验证和留出集")
    train_end = int(len(dates) * 0.6)
    validation_end = int(len(dates) * 0.8)
    return {
        "train": dates[:train_end],
        "validation": dates[train_end:validation_end],
        "holdout": dates[validation_end:],
    }


def trial_specs(base: StrategyConfig) -> list[TrialSpec]:
    scores = sorted({
        float(base.minimum_score),
        62.0,
        66.0,
        68.0,
        70.0,
        72.0,
    })
    return [
        TrialSpec(score, cutoff)
        for score in scores
        if score >= base.minimum_score
        for cutoff in ("09:45", base.entry_cutoff_time)
    ]


def summarize_returns(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    priced = [row for row in trades if row.get("net_return_pct") is not None]
    values = [float(row["net_return_pct"]) for row in priced]
    compounded = math.prod(1 + value / 100 for value in values) - 1 if values else None
    return {
        "trade_count": len(trades),
        "priced_trade_count": len(priced),
        "unsellable_proxy_count": len(trades) - len(priced),
        "mean_net_return_pct": round(statistics.fmean(values), 2) if values else None,
        "median_net_return_pct": round(statistics.median(values), 2) if values else None,
        "sum_net_return_pct": round(sum(values), 2) if values else None,
        "compounded_return_pct": round(compounded * 100, 2) if compounded is not None else None,
        "positive_count": sum(value > 0 for value in values),
        "positive_rate_pct": round(100 * sum(value > 0 for value in values) / len(values), 2)
        if values else None,
        "worst_trade_pct": round(min(values), 2) if values else None,
        "best_trade_pct": round(max(values), 2) if values else None,
    }


def _bar(
    snapshot: Mapping[str, Any],
    symbol: str,
    trade_date: str,
) -> Optional[Mapping[str, Any]]:
    return next(
        (
            row for row in snapshot.get("histories", {}).get(symbol, [])
            if str(row.get("datetime", ""))[:10] == trade_date
        ),
        None,
    )


def _minute_index(label: str) -> int:
    hour, minute = map(int, label.split(":")[:2])
    return hour * 60 + minute - (9 * 60 + 31)


def evaluate_trial(
    days: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    minutes: Mapping[str, Mapping[str, Sequence[Any]]],
    spec: TrialSpec,
    *,
    cost_bps: int = ROUND_TRIP_COST_BPS,
) -> list[dict[str, Any]]:
    calendar = sorted(str(value) for value in snapshot["calendar"])
    trades = []
    for day in days:
        trade_date = str(day["trade_date"])
        next_session = next((value for value in calendar if value > trade_date), None)
        if next_session is None:
            continue
        for stock in day["stocks"]:
            if float(stock["score"]) < spec.minimum_score:
                continue
            candidate = {
                "code": stock["symbol"],
                "name": stock["name"],
                "industry": stock.get("industry", ""),
                "rank": stock.get("rank"),
                "score": stock["score"],
                "latest_price": stock["reference_close"],
                "plan": {
                    "open_min_pct": 0.5,
                    "open_max_pct": 5.0,
                    "entry_cutoff_time": spec.entry_cutoff_time,
                    "manual_max_intraday_breaks": 1,
                    "reject_below_previous_close": True,
                },
            }
            daily = {
                key: stock.get(key)
                for key in ("open", "high", "low", "close")
            }
            minute = minutes.get(f"{trade_date}:{stock['symbol']}")
            result = classify_candidate(candidate, trade_date, daily, minute)
            if not result["buyable"]:
                continue
            buy_index = _minute_index(str(result["buy_window_time"]))
            buy_price = float(minute["prices"][buy_index])
            next_bar = _bar(snapshot, str(stock["symbol"]), next_session)
            if not next_bar or not isinstance(next_bar.get("open"), (int, float)):
                continue
            sell_price = float(next_bar["open"])
            limit_down = round(float(stock["close"]) * 0.9 + 1e-8, 2)
            sellable = sell_price > limit_down + 0.001
            gross_return = 100 * (sell_price / buy_price - 1)
            trades.append({
                "buy_date": trade_date,
                "symbol": stock["symbol"],
                "name": stock["name"],
                "industry": stock.get("industry", ""),
                "score": stock["score"],
                "buy_time": result["buy_window_time"],
                "buy_price": buy_price,
                "t1_date": next_session,
                "t1_open": sell_price,
                "gross_return_pct": round(gross_return, 2),
                "cost_bps": cost_bps,
                "net_return_pct": round(gross_return - cost_bps / 100, 2) if sellable else None,
                "t1_open_sellable_proxy": sellable,
            })
    return sorted(trades, key=lambda row: (row["buy_date"], row["symbol"]))


def _period_summary(
    trades: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
) -> dict[str, Any]:
    allowed = set(dates)
    return summarize_returns([row for row in trades if row["buy_date"] in allowed])


def _profitable(summary: Mapping[str, Any], minimum_trades: int) -> bool:
    return (
        summary["trade_count"] >= minimum_trades
        and summary["unsellable_proxy_count"] == 0
        and summary["mean_net_return_pct"] is not None
        and summary["mean_net_return_pct"] > 0
    )


def select_before_holdout(
    trials: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    eligible = [
        trial for trial in trials
        if trial["spec"] != baseline["spec"]
        and _profitable(trial["train"], MIN_TRAIN_TRADES)
        and _profitable(trial["validation"], MIN_VALIDATION_TRADES)
        and trial["train"]["mean_net_return_pct"] > baseline["train"]["mean_net_return_pct"]
        and trial["validation"]["mean_net_return_pct"] >= baseline["validation"]["mean_net_return_pct"]
    ]
    return max(
        eligible,
        key=lambda trial: (
            trial["validation"]["mean_net_return_pct"],
            trial["train"]["mean_net_return_pct"],
            trial["validation"]["trade_count"],
            -trial["spec"]["minimum_score"],
        ),
        default=None,
    )


def optimize(
    execution_report: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    minutes: Mapping[str, Mapping[str, Sequence[Any]]],
    *,
    strategy_id: str,
    base_config: StrategyConfig,
    cost_bps: int = ROUND_TRIP_COST_BPS,
) -> dict[str, Any]:
    source = next(
        (
            item for item in execution_report["strategies"]
            if item.get("strategy_id") == strategy_id
        ),
        None,
    )
    if source is None:
        raise ValueError("执行分析中不存在指定来源策略")
    split = chronological_split([day["trade_date"] for day in source["days"]])
    evaluated = []
    for spec in trial_specs(base_config):
        trades = evaluate_trial(source["days"], snapshot, minutes, spec, cost_bps=cost_bps)
        evaluated.append({
            "trial_id": spec.trial_id,
            "spec": asdict(spec),
            "train": _period_summary(trades, split["train"]),
            "validation": _period_summary(trades, split["validation"]),
            "_trades": trades,
        })
    baseline = next(
        trial for trial in evaluated
        if trial["spec"] == asdict(TrialSpec(base_config.minimum_score, base_config.entry_cutoff_time))
    )
    selected = select_before_holdout(evaluated, baseline)
    if selected is None:
        return {
            "schema_version": 1,
            "status": "no_profitable_candidate",
            "source_strategy_id": strategy_id,
            "split": split,
            "cost_bps": cost_bps,
            "trials": [{key: value for key, value in trial.items() if key != "_trades"} for trial in evaluated],
            "conclusion": "训练和验证阶段没有同时盈利且优于基线的参数，不生成新策略。",
        }

    baseline["holdout"] = _period_summary(baseline["_trades"], split["holdout"])
    baseline["overall"] = summarize_returns(baseline["_trades"])
    selected["holdout"] = _period_summary(selected["_trades"], split["holdout"])
    selected["overall"] = summarize_returns(selected["_trades"])
    all_periods_positive = all(
        _profitable(selected[key], 1)
        for key in ("train", "validation", "holdout")
    )
    holdout_not_worse = (
        selected["holdout"]["mean_net_return_pct"]
        >= baseline["holdout"]["mean_net_return_pct"]
    )
    qualified = all_periods_positive and holdout_not_worse
    config = asdict(base_config)
    config.update(selected["spec"])
    changes = {
        key: {"from": getattr(base_config, key), "to": value}
        for key, value in selected["spec"].items()
        if getattr(base_config, key) != value
    }
    selected_public = {key: value for key, value in selected.items() if key != "_trades"}
    baseline_public = {key: value for key, value in baseline.items() if key != "_trades"}
    return {
        "schema_version": 1,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "status": "historically_profitable_research_candidate" if qualified else "holdout_rejected",
        "source": "mootdx",
        "source_strategy_id": strategy_id,
        "source_strategy_name": source["strategy_name"],
        "protocol": {
            "selection": "仅使用训练和验证集选参；选定后只评估一次留出集。",
            "exit": "严格可买窗口成交，T+1 开盘卖出。",
            "cost": f"每笔往返固定扣除 {cost_bps}bp，包含佣金、印花税和滑点代理。",
            "sellability": "T+1 开盘价高于跌停价仅作为可卖代理，不证明真实成交。",
            "candidate_universe": "仅收紧来源策略历史候选，不扩张候选宇宙。",
        },
        "split": split,
        "baseline": baseline_public,
        "selected": selected_public,
        "selected_before_holdout": True,
        "all_periods_positive": all_periods_positive,
        "holdout_not_worse": holdout_not_worse,
        "changes": changes,
        "strategy": {
            "name": "一进二盈利约束优化（研究候选）",
            "active": False,
            "config": config,
            "revision": revision_for(config),
            "parent_strategy_id": strategy_id,
        },
        "trades": selected["_trades"],
        "trials": [{key: value for key, value in trial.items() if key != "_trades"} for trial in evaluated],
        "conclusion": (
            "训练、验证和留出集的成本后收益均为正，但严格可买样本仍少，"
            "仅保存为未激活研究候选，不代表未来盈利保证。"
            if qualified else
            "候选未通过留出集盈利约束，不生成可用策略。"
        ),
    }


def write_report(result: Mapping[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "result.json", result)
    if "strategy" in result:
        write_json(output / "optimized-strategy.json", result["strategy"]["config"])
        write_json(output / "strategy-metadata.json", {
            key: result[key] for key in ("status", "changes", "conclusion")
        } | result["strategy"])
    trades = result.get("trades", [])
    if trades:
        with (output / "trades.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)
    lines = [
        "# 一进二盈利约束优化",
        "",
        result["conclusion"],
        "",
        f"- 状态：`{result['status']}`",
        f"- 数据源：{result.get('source', 'mootdx')}",
        f"- 成本：{result.get('protocol', {}).get('cost', result.get('cost_bps'))}",
    ]
    if "selected" in result:
        lines.extend([
            f"- 参数变化：{json.dumps(result['changes'], ensure_ascii=False)}",
            "",
            "| 区间 | 日期 | 笔数 | 平均净收益 | 逐笔复合收益 | 胜率 | 最差单笔 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ])
        for key, title in (("train", "训练"), ("validation", "验证"), ("holdout", "留出")):
            dates = result["split"][key]
            summary = result["selected"][key]
            lines.append(
                f"| {title} | {dates[0]} 至 {dates[-1]} | {summary['trade_count']} | "
                f"{summary['mean_net_return_pct']}% | {summary['compounded_return_pct']}% | "
                f"{summary['positive_rate_pct']}% | {summary['worst_trade_pct']}% |"
            )
        old = result["baseline"]["overall"]
        new = result["selected"]["overall"]
        lines.extend([
            f"| 全区间 | {result['split']['train'][0]} 至 {result['split']['holdout'][-1]} | "
            f"{new['trade_count']} | {new['mean_net_return_pct']}% | "
            f"{new['compounded_return_pct']}% | {new['positive_rate_pct']}% | "
            f"{new['worst_trade_pct']}% |",
            "",
            "## 基线对比",
            "",
            "| 策略 | 笔数 | 平均净收益 | 逐笔复合收益 | 胜率 | 最差单笔 |",
            "|---|---:|---:|---:|---:|---:|",
            f"| 原策略 10:00 截止 | {old['trade_count']} | {old['mean_net_return_pct']}% | "
            f"{old['compounded_return_pct']}% | {old['positive_rate_pct']}% | "
            f"{old['worst_trade_pct']}% |",
            f"| 候选策略 09:45 截止 | {new['trade_count']} | {new['mean_net_return_pct']}% | "
            f"{new['compounded_return_pct']}% | {new['positive_rate_pct']}% | "
            f"{new['worst_trade_pct']}% |",
        ])
        lines.extend([
            "",
            "## 严格可买交易",
            "",
            "| 买入日 | 股票 | 买入时间 | T+1 | 毛收益 | 成本后净收益 |",
            "|---|---|---|---|---:|---:|",
        ])
        for row in trades:
            lines.append(
                f"| {row['buy_date']} | {row['name']}({row['symbol']}) | {row['buy_time']} | "
                f"{row['t1_date']} | {row['gross_return_pct']}% | {row['net_return_pct']}% |"
            )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def persist_candidate(result: dict[str, Any], repository: PostgresStorage) -> Mapping[str, Any]:
    if result["status"] != "historically_profitable_research_candidate":
        raise ValueError("未通过盈利约束，禁止保存候选策略")
    strategy = result["strategy"]
    for item in repository.list_strategies():
        if (
            item.get("parent_strategy_id") == strategy["parent_strategy_id"]
            and item.get("config") == strategy["config"]
        ):
            result["persisted_strategy_id"] = item["strategy_id"]
            return item
    item = repository.create_strategy(
        strategy["name"],
        strategy["config"],
        description=result["conclusion"],
        enabled=False,
        parent_strategy_id=strategy["parent_strategy_id"],
        config_changes=result["changes"],
    )
    result["persisted_strategy_id"] = item["strategy_id"]
    return item


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="严格可买口径下优化 T+1 成本后收益")
    parser.add_argument("--execution-report", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--minutes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--cost-bps", type=int, default=ROUND_TRIP_COST_BPS)
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args(argv)

    repository = None
    try:
        if args.persist:
            repository = PostgresStorage(StorageSettings.from_env().postgres_dsn)
            source = repository.get_strategy(args.strategy_id)
            if source is None:
                raise ValueError("来源策略不存在")
            base_config = StrategyConfig.from_mapping(source.get("config") or {})
        else:
            base_config = StrategyConfig()
        result = optimize(
            json.loads(args.execution_report.read_text()),
            json.loads(args.snapshot.read_text()),
            json.loads(args.minutes.read_text()),
            strategy_id=args.strategy_id,
            base_config=base_config,
            cost_bps=args.cost_bps,
        )
        if repository and result["status"] == "historically_profitable_research_candidate":
            persist_candidate(result, repository)
        write_report(result, args.output)
        print(json.dumps({
            "status": result["status"],
            "output": str(args.output.resolve()),
            "changes": result.get("changes"),
            "selected": result.get("selected"),
            "persisted_strategy_id": result.get("persisted_strategy_id"),
        }, ensure_ascii=False, indent=2), flush=True)
    finally:
        if repository:
            repository.close()


if __name__ == "__main__":
    main()
