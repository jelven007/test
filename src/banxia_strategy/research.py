"""Auditable next-day labels and chronological strategy parameter experiments."""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .mootdx_provider import _at_price_limit, _limit_price, _minute_label
from .research_data import DatedResearchProvider, ResearchCollector, snapshot_hash, write_json
from .strategy import StrategyEngine
from .strategy_config import StrategyConfig, revision_for, version_for


METRIC = {
    "name": "次日收盘二板命中率",
    "formula": "次日收盘封住涨停的候选数 ÷ 有完整次日日线的候选数",
    "daily": "按计划执行日统计；无候选或无已验证候选时为 null，不记作 0% 或 100%。",
    "period": "周/月汇总采用总命中数 ÷ 总已验证数，并同时保存有样本日的平均命中率。",
    "auction": "用次日日线开盘价近似竞价价，单列合格后的命中率，不等同真实成交。",
    "execution": "缺少秒级封稳、完整板块分时和排队成交数据，实际入场准确率不可核验。",
}


def rate(numerator, denominator):
    return round(100 * numerator / denominator, 2) if denominator else None


def summarize(rows):
    observed = [row for row in rows if row["status"] == "observed"]
    qualified = [row for row in observed if row["auction_qualified"]]
    successes = sum(row["closed_limit_up"] for row in observed)
    n = len(observed)
    # Wilson interval exposes the uncertainty of very small candidate sets.
    interval = None
    if n:
        p, z = successes / n, 1.96
        center = (p + z*z/(2*n)) / (1 + z*z/n)
        radius = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
        interval = [round(100*(center-radius), 2), round(100*(center+radius), 2)]
    return {
        "candidate_count": len(rows), "observed_count": n,
        "pending_count": sum(row["status"] == "pending" for row in rows),
        "missing_count": sum(row["status"] == "missing" for row in rows),
        "hit_count": successes, "accuracy_pct": rate(successes, n),
        "touch_count": sum(row["touched_limit_up"] for row in observed),
        "touch_rate_pct": rate(sum(row["touched_limit_up"] for row in observed), n),
        "auction_count": len(qualified),
        "auction_hit_count": sum(row["closed_limit_up"] for row in qualified),
        "auction_accuracy_pct": rate(sum(row["closed_limit_up"] for row in qualified), len(qualified)),
        "coverage_pct": rate(n, len(rows)),
        "accuracy_interval_95_pct": interval,
        "execution_accuracy_pct": None,
    }


def label_candidate(candidate, next_session, snapshot):
    ref = candidate["latest_price"]
    row = {
        "symbol": candidate["code"], "name": candidate["name"],
        "score": candidate["score"], "industry": candidate["industry"],
        "reference_close": ref, "plan_date": next_session,
        "status": "pending", "closed_limit_up": None, "touched_limit_up": None,
        "auction_qualified": None, "entry_verification": "unverified",
    }
    if not next_session or next_session > snapshot["requested_end"]:
        row["reason"] = "次日完整行情待采集"
        return row
    bars = snapshot["histories"].get(candidate["code"], [])
    bar = next((bar for bar in bars if str(bar.get("datetime", ""))[:10] == next_session), None)
    if not bar or any(not isinstance(bar.get(key), (int, float)) or not math.isfinite(bar[key])
                      or bar[key] <= 0 for key in ("open", "high", "low", "close")) or not bar.get("vol", bar.get("volume")):
        row.update(status="missing", reason="次日日线缺失、停牌或价格无效")
        return row
    limit_price = _limit_price(ref, candidate["name"])
    open_pct = 100 * (bar["open"] / ref - 1)
    rules = candidate["plan"]
    auction = rules["open_min_pct"] <= round(open_pct, 6) <= rules["open_max_pct"]
    prices = snapshot.get("minutes", {}).get(f"{next_session}:{candidate['code']}", [])
    full_minutes = len(prices) == 240
    touch_time = next((_minute_label(i) for i, price in enumerate(prices)
                       if _at_price_limit(price, limit_price)), None) if full_minutes else None
    row.update(
        status="observed", reason="日线收盘验证完成；实际入场需额外盘口及板块证据",
        open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"],
        limit_price=limit_price, open_change_pct=round(open_pct, 3),
        close_change_pct=round(100*(bar["close"]/ref-1), 3),
        closed_limit_up=_at_price_limit(bar["close"], limit_price),
        touched_limit_up=_at_price_limit(bar["high"], limit_price),
        auction_qualified=auction, daily_below_reference=bar["low"] < ref,
        minute_data_complete=full_minutes, first_minute_touch=touch_time,
        early_touch_proxy=(touch_time < rules["entry_cutoff_time"] + ":00" if touch_time else False)
        if full_minutes else None,
    )
    if not auction:
        row["entry_verification"] = "auction_outside"
    return row


def aggregate_days(days):
    result = summarize([row for day in days for row in day["outcomes"]])
    daily = [day["summary"]["accuracy_pct"] for day in days if day["summary"]["accuracy_pct"] is not None]
    result.update(
        day_count=len(days), active_days=len(daily),
        mean_daily_accuracy_pct=round(sum(daily)/len(daily), 2) if daily else None,
        empty_days=sum(not day["outcomes"] for day in days),
    )
    return result


def group_periods(days, frequency):
    groups = defaultdict(list)
    for day in days:
        value = date.fromisoformat(day["plan_date"])
        year, week, _ = value.isocalendar()
        key = f"{year}-W{week:02d}" if frequency == "week" else value.strftime("%Y-%m")
        groups[key].append(day)
    return [{"period": key, **aggregate_days(value)} for key, value in sorted(groups.items())]


def evaluate_config(snapshot, references, config, *, commit="research"):
    days = []
    for reference in references:
        provider = DatedResearchProvider(snapshot, date.fromisoformat(reference))
        report = StrategyEngine(provider, config).run(date.fromisoformat(reference))
        if report.as_of != reference:
            raise RuntimeError(f"禁止将 {report.as_of} 的计划冒充 {reference}：当日行情池为空")
        report.strategy_version = version_for(asdict(config), "research", commit)
        report.code_commit = commit
        payload = report.to_dict()
        outcomes = [label_candidate(candidate, report.next_session, snapshot)
                    for candidate in payload["candidates"]]
        days.append({
            "reference_date": reference, "plan_date": report.next_session,
            "report": payload, "outcomes": outcomes, "summary": summarize(outcomes),
        })
    weeks = group_periods(days, "week")
    summary = aggregate_days(days)
    weekly_rates = [week["accuracy_pct"] for week in weeks if week["accuracy_pct"] is not None]
    summary["mean_weekly_accuracy_pct"] = round(sum(weekly_rates)/len(weekly_rates), 2) if weekly_rates else None
    return {"days": days, "summary": summary, "weeks": weeks, "months": group_periods(days, "month")}


def experiment_specs(base):
    """Declared grid; preserve timing/risk limits and never relax the amount floor."""
    baseline = asdict(base)
    specs = [{"trial_id": "baseline", "changes": {}, "config": baseline}]
    seen = {revision_for(baseline)}
    for minimum_score in sorted({base.minimum_score, min(1000, base.minimum_score + 4),
                                 min(1000, base.minimum_score + 8), 68, 72, 76}):
        for theme_count in sorted({base.minimum_industry_limit_up_count,
                                   max(3, base.minimum_industry_limit_up_count),
                                   max(4, base.minimum_industry_limit_up_count)}):
            for breaks in sorted({0, base.maximum_break_count}):
                values = {**baseline, "minimum_score": minimum_score,
                          "minimum_industry_limit_up_count": theme_count, "maximum_break_count": breaks}
                config = asdict(StrategyConfig.from_mapping(values))
                revision = revision_for(config)
                if revision in seen:
                    continue
                seen.add(revision)
                specs.append({
                    "trial_id": f"trial-{len(specs):02d}", "config": config,
                    "changes": {key: {"before": baseline[key], "after": value}
                                for key, value in config.items() if value != baseline[key]},
                })
    for changes in [
        {"max_per_industry": 1}, {"max_per_industry": 2},
        {"minimum_amount_cny": max(base.minimum_amount_cny, min(200_000_000, base.ideal_amount_min_cny))},
        {"max_candidates": 2, "max_per_industry": 1},
        {"early_weight": 18, "persistence_weight": 10, "theme_active_weight": 2, "theme_momentum_weight": 3},
        {"early_weight": 4, "theme_active_weight": 10, "theme_momentum_weight": 11},
    ]:
        config = asdict(StrategyConfig.from_mapping({**baseline, **changes}))
        revision = revision_for(config)
        if revision in seen:
            continue
        seen.add(revision)
        specs.append({
            "trial_id": f"trial-{len(specs):02d}", "config": config,
            "changes": {key: {"before": baseline[key], "after": value}
                        for key, value in config.items() if value != baseline[key]},
        })
    return specs


def objective(summary):
    if not summary["observed_count"]:
        return -1.0
    return round(0.4 * summary["accuracy_pct"]
                 + 0.3 * summary["mean_daily_accuracy_pct"]
                 + 0.3 * summary["mean_weekly_accuracy_pct"], 4)


def enough_samples(summary, baseline):
    return (
        summary["observed_count"] >= max(5, math.ceil(baseline["observed_count"] * .6))
        and summary["active_days"] >= max(3, math.ceil(baseline["active_days"] * .6))
        and summary["missing_count"] == 0
    )


def capture_source_snapshot(output: Path, *, stage="before_experiment"):
    source_root = Path(__file__).parent
    manifest = {}
    for path in sorted(source_root.rglob("*.py")):
        target = output / "source" / "banxia_strategy" / path.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        manifest[str(path.relative_to(source_root))] = snapshot_hash(target)
    write_json(output / "source-manifest.json", {
        "stage": stage, "captured_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "files": manifest,
        "note": "每轮实际搜索范围以 protocol.json 为准；source 保存代码及其哈希。",
    })


def optimize(snapshot, base, output: Path, *, commit="research", prior_run_id=None):
    references = sorted(value for value in snapshot["calendar"]
                        if snapshot["requested_start"] <= value <= snapshot["requested_end"])
    eligible = [value for value in references
                if next((d for d in snapshot["calendar"] if d > value), "9999") <= snapshot["requested_end"]]
    if len(eligible) < 15:
        raise ValueError("至少需要15个有完整次日行情的计划日，才能划分训练/验证/留出集")
    holdout_count = max(5, len(eligible)//5)
    validation_count = max(4, len(eligible)//5)
    split = {
        "train": eligible[:-holdout_count-validation_count],
        "validation": eligible[-holdout_count-validation_count:-holdout_count],
        "holdout": eligible[-holdout_count:],
        "pending": [value for value in references if value not in eligible],
    }
    run_id = str(uuid.uuid4())
    output = output / run_id
    output.mkdir(parents=True, exist_ok=False)
    capture_source_snapshot(output)
    specs = experiment_specs(base)
    protocol = {
        "run_id": run_id, "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "split": split, "metric": METRIC, "search_space": specs,
        "objective": "40%候选加权命中率 + 30%日均命中率 + 30%周均命中率",
        "selection": "先按训练集目标分选前三名，再仅用验证集选定一个候选；最后一次评估留出集。",
        "sample_constraint": "训练集至少5个验证候选、3个有效日，且候选数和有效日均不少于基线60%，不得有缺失标签。",
        "holdout_rule": "只检验预先选定的策略，不据此继续调参；不保证每日、每周或后续月份提升。",
        "prior_run_id": prior_run_id,
        "holdout_reused": bool(prior_run_id),
    }
    write_json(output / "protocol.json", protocol)
    trials = []
    for spec in specs:
        result = evaluate_config(snapshot, split["train"], StrategyConfig.from_mapping(spec["config"]), commit=commit)
        baseline_summary = trials[0]["training"]["summary"] if trials else result["summary"]
        trial = {
            **spec, "training": result, "objective": objective(result["summary"]),
            "eligible": enough_samples(result["summary"], baseline_summary),
        }
        trials.append(trial)
        write_json(output / "trials" / f"{spec['trial_id']}.json", trial)
        print(f"实验 {spec['trial_id']} · 训练命中率 {result['summary']['accuracy_pct']}% · "
              f"样本 {result['summary']['observed_count']} · 合格 {trial['eligible']}", flush=True)
    shortlist = sorted(
        [trial for trial in trials if trial["eligible"] and trial["changes"]],
        key=lambda trial: (-trial["objective"], -trial["training"]["summary"]["observed_count"], trial["trial_id"]),
    )[:3]
    finalists = [trials[0], *shortlist]
    for trial in finalists:
        trial["validation"] = evaluate_config(
            snapshot, split["validation"], StrategyConfig.from_mapping(trial["config"]), commit=commit,
        )
        trial["validation_objective"] = objective(trial["validation"]["summary"])
        write_json(output / "trials" / f"{trial['trial_id']}.json", trial)
    baseline_validation = trials[0]["validation"]["summary"]
    valid = [trial for trial in shortlist
             if enough_samples(trial["validation"]["summary"], baseline_validation)]
    selected = max(valid, key=lambda trial: (trial["validation_objective"], trial["objective"]), default=None)
    # Save the best explored variant even if no variant earns promotion.
    if selected is None:
        variants = [trial for trial in trials if trial["changes"]]
        selected = max(variants, key=lambda trial: trial["objective"])
        if "validation" not in selected:
            selected["validation"] = evaluate_config(
                snapshot, split["validation"], StrategyConfig.from_mapping(selected["config"]), commit=commit,
            )
            selected["validation_objective"] = objective(selected["validation"]["summary"])
            write_json(output / "trials" / f"{selected['trial_id']}.json", selected)
    selection = {
        "trial_id": selected["trial_id"], "config": selected["config"],
        "selected_before_holdout": True, "validation_eligible": selected in valid,
        "validation_improved": selected["validation_objective"] > trials[0]["validation_objective"],
    }
    write_json(output / "selection-before-holdout.json", selection)
    baseline = evaluate_config(snapshot, references, base, commit=commit)
    optimized = evaluate_config(snapshot, references, StrategyConfig.from_mapping(selected["config"]), commit=commit)
    for result in (baseline, optimized):
        result["holdout"] = evaluate_config(
            snapshot, split["holdout"],
            StrategyConfig.from_mapping(result["days"][0]["report"]["strategy_config"]), commit=commit,
        )["summary"]
    deltas = {
        key: round(optimized["holdout"][key] - baseline["holdout"][key], 2)
        if optimized["holdout"][key] is not None and baseline["holdout"][key] is not None else None
        for key in ("accuracy_pct", "mean_daily_accuracy_pct", "mean_weekly_accuracy_pct")
    }
    validated = (
        selection["validation_eligible"] and selection["validation_improved"]
        and enough_samples(optimized["holdout"], baseline["holdout"])
        and all(value is not None and value >= 0 for value in deltas.values())
        and any(value is not None and value > 0 for value in deltas.values())
    )
    strategy = {
        "strategy_id": f"research-{run_id}", "name": "一进二 · 月度优化研究",
        "revision": revision_for(selected["config"]), "config": selected["config"],
        "changes": selected["changes"], "selected_trial": selected["trial_id"],
        "status": "holdout_improved" if validated and not prior_run_id else "research_only",
        "active": False, "holdout_deltas_pp": deltas,
        "conclusion": (
            "扩展实验复用了首轮的留出日期，结果仅作探索性比较；需用新增交易日独立验证，暂不替换当前策略。"
            if prior_run_id else
            "留出集日均、周均及候选加权命中率均未下降，至少一项提高；仍需新增月份验证。"
            if validated else "本次未通过全部留出提升和样本约束；保存为研究候选，不替换当前策略。"
        ),
    }
    payload = {
        "run_id": run_id, "created_at": protocol["created_at"],
        "start_date": snapshot["requested_start"], "end_date": snapshot["requested_end"],
        "status": "completed", "protocol": protocol, "quality": snapshot["quality"],
        "input_collected_at": snapshot["collected_at"],
        "baseline_revision": revision_for(asdict(base)),
        "baseline": baseline, "optimized": optimized, "strategy": strategy,
        "trials": [{key: value for key, value in trial.items() if key not in {"training", "validation"}}
                   | {"training_summary": trial["training"]["summary"],
                      "validation_summary": trial.get("validation", {}).get("summary")}
                   for trial in trials],
    }
    write_json(output / "result.json", payload)
    write_json(output / "optimized-strategy.json", selected["config"])
    write_json(output / "strategy-metadata.json", strategy)
    for label, result in (("baseline", baseline), ("optimized", optimized)):
        for day in result["days"]:
            write_json(output / label / day["reference_date"] / "plan.json", day["report"])
            write_json(output / label / day["reference_date"] / "review.json", day)
        with (output / f"{label}-daily.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            fields = ["reference_date", "plan_date", *result["days"][0]["summary"].keys()]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({"reference_date": day["reference_date"], "plan_date": day["plan_date"],
                             **day["summary"]} for day in result["days"])
    lines = [
        "# 近一个月次日计划与策略优化", "", f"区间：{payload['start_date']} ～ {payload['end_date']}",
        "", METRIC["formula"], "", strategy["conclusion"], "",
        "| 范围 | 原策略 | 优化候选 |", "|---|---:|---:|",
        f"| 全区间（含训练，描述性） | {baseline['summary']['accuracy_pct']}% | {optimized['summary']['accuracy_pct']}% |",
        f"| 留出集 | {baseline['holdout']['accuracy_pct']}% | {optimized['holdout']['accuracy_pct']}% |", "",
        "## 每日结果", "", "| 计划日 | 原策略命中/样本 | 原准确率 | 新策略命中/样本 | 新准确率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for old, new in zip(baseline["days"], optimized["days"]):
        a, b = old["summary"], new["summary"]
        lines.append(f"| {old['plan_date']} | {a['hit_count']}/{a['observed_count']} | "
                     f"{a['accuracy_pct'] if a['accuracy_pct'] is not None else '无样本/待验证'} | "
                     f"{b['hit_count']}/{b['observed_count']} | "
                     f"{b['accuracy_pct'] if b['accuracy_pct'] is not None else '无样本/待验证'} |")
    lines.extend(["", "## 数据边界", "", *["- " + value for value in snapshot["quality"]["limitations"]]])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload, output


def main(argv=None):
    parser = argparse.ArgumentParser(description="mootdx 历史计划重建、复盘和时间留出优化")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    previous_month = (today.replace(day=1) - timedelta(days=1))
    default_start = previous_month.replace(day=min(today.day, calendar.monthrange(previous_month.year, previous_month.month)[1]))
    parser.add_argument("--start", type=date.fromisoformat, default=default_start)
    parser.add_argument("--end", type=date.fromisoformat, default=today - timedelta(days=1))
    parser.add_argument("--config", type=Path, default=Path("config/strategy.json"))
    parser.add_argument("--output", type=Path, default=Path("research"))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--prior-run-id", help="扩展既有实验时记录前序实验，并标记留出集复用")
    args = parser.parse_args(argv)
    config = StrategyConfig.from_file(args.config)
    directory = args.output / f"{args.start:%Y%m%d}-{args.end:%Y%m%d}"
    input_path = args.snapshot or directory / "inputs" / "snapshot.json"
    if input_path.exists():
        snapshot = json.loads(input_path.read_text())
    else:
        snapshot = ResearchCollector(history_sessions=max(100, config.lookback_sessions + 40)).collect(
            args.start, args.end, input_path.parent,
        )
    if snapshot["requested_start"] != str(args.start) or snapshot["requested_end"] != str(args.end):
        raise ValueError("输入快照日期与请求区间不一致")
    from .storage_config import StorageSettings
    settings = StorageSettings.from_env()
    payload, output = optimize(snapshot, config, directory / "runs",
                               commit=settings.code_commit, prior_run_id=args.prior_run_id)
    payload["input_sha256"] = snapshot_hash(input_path)
    write_json(output / "result.json", payload)
    if args.persist:
        from .research_storage import persist_research
        persist_research(payload, output, input_path, settings)
    print(json.dumps({
        "run_id": payload["run_id"], "output": str(output.resolve()),
        "baseline": payload["baseline"]["summary"], "optimized": payload["optimized"]["summary"],
        "strategy": payload["strategy"]["conclusion"],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
