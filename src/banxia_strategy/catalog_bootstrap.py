"""One-time, repeatable import of existing reports and latest research into the catalog."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters.postgres import PostgresStorage, STRATEGY_CODE, _uuid
from .strategy_config import StrategyConfigStore
from .web_server import ReportStore


def bootstrap(repository, roots, config_path):
    default_id = _uuid(f"strategy:{STRATEGY_CODE}")
    current = StrategyConfigStore(config_path).payload()["config"]
    # Catalog exists before a report has ever succeeded.
    with repository.connection_factory() as connection:
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO banxia.strategy_definition
                (strategy_id,code,name,current_config) VALUES (%s,%s,%s,%s::jsonb)
                ON CONFLICT(code) DO NOTHING""",
                (default_id, STRATEGY_CODE, "首板晋级二板策略", json.dumps(current)))
    research = repository.list_research_runs()
    imported = 0
    if research:
        result = repository.get_research_run(research[0]["run_id"])
        optimized_id = _uuid(f"catalog-research:{result['strategy']['strategy_id']}")
        if not repository.get_strategy(optimized_id):
            repository.create_strategy(
                "题材分散优化策略", result["strategy"]["config"],
                description=f"研究候选；来源实验 {result['run_id']}，未自动替换默认策略",
                enabled=False, strategy_id=optimized_id,
            )
        for variant, sid in (("baseline", default_id), ("optimized", optimized_id)):
            item = repository.get_strategy(sid)
            days = result[variant]["days"]
            repository.save_trading_sessions(sorted({day["reference_date"] for day in days} |
                                                      {day["plan_date"] for day in days if day["plan_date"]}))
            for day in days:
                existing = repository.get_strategy_day(sid, day["reference_date"])
                if not existing or not existing["next_plan"]:
                    report = {**day["report"], "strategy_id": sid, "strategy_code": item["code"],
                              "strategy_name": item["name"], "research_run_id": result["run_id"]}
                    repository.save_daily_report(sid, report)
                    imported += 1
            for day in days:
                record = repository.get_strategy_day(sid, day["plan_date"])
                execution = record["execution_plan"] if record else None
                if (execution and execution.get("research_run_id") == result["run_id"]
                        and not record["actuals"]):
                    summary = day["summary"]
                    status = ("pending" if summary["pending_count"] else
                              "missing" if summary["missing_count"] else "complete")
                    repository.save_day_actuals(sid, day["plan_date"], {
                        "status": status, "outcomes": day["outcomes"], "summary": summary,
                        "kind": "historical_market_validation", "source": "mootdx",
                    })
    # Local live reports take precedence over research, but never replace an
    # already newer daily record. Persist existing identity; keep audit versions.
    store = ReportStore(roots)
    with repository.connection_factory() as connection:
        calendar = [str(row[0]) for row in connection.execute("SELECT trade_date FROM banxia.trading_session ORDER BY 1").fetchall()]
    for entry in reversed(store.list_reports()):
        report = store.get(entry["as_of"])
        known_next = next((day for day in calendar if day > report["as_of"]), None)
        if known_next and report.get("next_session") != known_next:
            report = {**report, "next_session": known_next}
        existing = repository.get_strategy_day(default_id, report["as_of"])
        if (existing and existing["next_plan"] and not existing["next_plan"].get("research_run_id")
                and existing["next_plan"].get("next_session") == report.get("next_session")
                and existing["next_plan"].get("generated_at", "") >= report["generated_at"]):
            continue
        config = report.get("strategy_config") or current
        from .strategy_config import version_for
        version = report.get("strategy_version") or version_for(config, "v2", report.get("code_commit") or "local")
        repository.persist_report(
            report, strategy_version=version, strategy_config=config,
            code_commit=report.get("code_commit") or "local", enqueue_events=True,
        )
        imported += 1
    with repository.connection_factory() as connection:
        connection.execute("""DELETE FROM banxia.strategy_day d WHERE next_plan IS NULL AND actuals='{}'::jsonb
            AND NOT EXISTS(SELECT 1 FROM banxia.trading_session t WHERE t.trade_date=d.trade_date)
            AND EXISTS(SELECT 1 FROM banxia.strategy_plan p WHERE p.plan_id::text=d.execution_plan->>'plan_id'
                       AND p.trade_date<>d.trade_date)""")
    # Validate remaining imported execution plans using the same immutable
    # mootdx snapshot as the research, including locally generated live plans.
    if research:
        from .research import label_candidate, summarize
        from .intraday import plan_for
        snapshot_paths = sorted(Path("research").glob("*/inputs/snapshot.json"))
        if snapshot_paths:
            snapshot = json.loads(snapshot_paths[-1].read_text())
            for item in repository.list_strategies():
                for row in repository.list_strategy_days(item["strategy_id"], 200):
                    if row["trade_date"] > snapshot["requested_end"]:
                        continue
                    day = repository.get_strategy_day(item["strategy_id"], row["trade_date"])
                    execution = day["execution_plan"]
                    if not execution or day["actuals"].get("status") == "complete":
                        continue
                    outcomes = [label_candidate({**c, "plan": plan_for(c)}, row["trade_date"], snapshot)
                                for c in execution["candidates"]]
                    repository.save_day_actuals(item["strategy_id"], row["trade_date"], {
                        "status": "complete" if all(o["status"] == "observed" for o in outcomes) else "missing",
                        "outcomes": outcomes, "summary": summarize(outcomes),
                        "source": "mootdx", "kind": "historical_market_validation",
                    }, execution.get("plan_id"))
    repository.ensure_strategy_days(datetime.now(ZoneInfo("Asia/Shanghai")).date())
    return {"imported_reports": imported, "strategies": len(repository.list_strategies())}


if __name__ == "__main__":
    from .config import RuntimeSettings
    settings = RuntimeSettings.from_env()
    repository = PostgresStorage(settings.storage.postgres_dsn)
    try:
        print(json.dumps(bootstrap(repository, settings.report_dirs, settings.strategy_config_path), ensure_ascii=False))
    finally:
        repository.close()
