import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .domain.intraday import PHASE_LABELS, SHANGHAI, phase_at
from .mootdx_provider import MootdxProvider
from .web_server import ReportStore
from .strategy_config import ConfigConflict, ConfigError, StrategyConfig, StrategyConfigStore, revision_for
from .strategy_history import HISTORY_RANGE_DAYS, history_window
from .adapters.postgres import STRATEGY_CODE
from .adapters.strategy_catalog import CatalogConfigStore


STRATEGY_LIST_PARAMETER_KEYS = (
    "minimum_score",
    "minimum_amount_cny",
    "maximum_amount_cny",
    "minimum_turnover_pct",
    "maximum_turnover_pct",
    "minimum_float_market_cap_cny",
    "maximum_float_market_cap_cny",
    "minimum_industry_limit_up_count",
    "entry_cutoff_time",
)


@dataclass
class ApiServices:
    reports: ReportStore
    repository: Any
    cache: Any
    object_store: Optional[Any] = None
    api_token: Optional[str] = None
    kafka_ready: Optional[Callable[[], bool]] = None
    clickhouse_ready: Optional[Callable[[], bool]] = None
    market_history: Optional[Any] = None
    strategy_version: str = "v1"
    config_store: Optional[StrategyConfigStore] = None
    history_provider: Optional[Any] = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _report_summary(item: Mapping[str, Any], strategy_version: str = "v1"):
    return {
        "trade_date": item["as_of"],
        "plan_date": item.get("next_session"),
        "generated_at": item.get("generated_at"),
        "candidate_count": item.get("candidate_count", 0),
        "market_regime": item.get("regime"),
        "market_score": item.get("market_score"),
        "strategy_version": item.get("strategy_version") or strategy_version,
    }


def _report_payload(report: Mapping[str, Any], strategy_version: str = "v1"):
    candidates = []
    for source in report.get("candidates", []):
        item = dict(source)
        item["symbol"] = item.pop("code")
        candidates.append(item)
    return {
        "trade_date": report["as_of"],
        "plan_date": report.get("next_session"),
        "generated_at": report["generated_at"],
        "candidate_count": len(candidates),
        "market_regime": report["market"].get("regime"),
        "market_score": report["market"].get("score"),
        "strategy_version": report.get("strategy_version") or strategy_version,
        "strategy_revision": (
            revision_for(report["strategy_config"])
            if report.get("strategy_config")
            else None
        ),
        "strategy_config": report.get("strategy_config", {}),
        "constraints": report.get("constraints", {}),
        "market": report["market"],
        "candidates": candidates,
        "data_source": report.get("data_source"),
        "data_sessions": report.get("data_sessions", []),
        "rejected_count": report.get("rejected_count", 0),
        "disclaimer": report.get("disclaimer", ""),
    }


def _data_state(max_age: Optional[float], missing: int) -> str:
    if missing:
        return "unavailable"
    if max_age is None:
        return "unavailable"
    if max_age <= 6:
        return "fresh"
    if max_age <= 180:
        return "delayed"
    return "stale"


def create_api_app(services: ApiServices):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import (
            FileResponse,
            JSONResponse,
            RedirectResponse,
            Response,
            StreamingResponse,
        )
        from fastapi.staticfiles import StaticFiles
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            Counter,
            Histogram,
            generate_latest,
        )
        from starlette.exceptions import HTTPException as StarletteHTTPException
    except ImportError as exc:
        raise RuntimeError(
            "API service requires `pip install -e '.[production]'`"
        ) from exc

    app = FastAPI(
        title="Banxia Strategy Research API",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    metrics_registry = CollectorRegistry()
    request_total = Counter(
        "banxia_http_requests_total",
        "HTTP requests handled by the API service.",
        ("method", "path", "status"),
        registry=metrics_registry,
    )
    request_duration = Histogram(
        "banxia_http_request_duration_seconds",
        "HTTP request latency.",
        ("method", "path"),
        registry=metrics_registry,
    )

    def error_response(
        request: Request,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": getattr(
                        request.state,
                        "request_id",
                        str(uuid.uuid4()),
                    ),
                    "details": dict(details or {}),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        codes = {
            400: "BAD_REQUEST",
            401: "UNAUTHORIZED",
            404: "NOT_FOUND",
            409: "CONFLICT",
            422: "VALIDATION_ERROR",
            503: "SERVICE_UNAVAILABLE",
        }
        return error_response(
            request,
            status_code=exc.status_code,
            code=codes.get(exc.status_code, "HTTP_ERROR"),
            message=str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(
            request,
            status_code=422,
            code="VALIDATION_ERROR",
            message="request validation failed",
            details={"errors": exc.errors()},
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        started = time.perf_counter()
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        if (
            services.api_token
            and not request.url.path.startswith("/api/v1/health/")
            and request.url.path != "/metrics"
            and request.url.path.startswith("/api/")
        ):
            expected = f"Bearer {services.api_token}"
            if request.headers.get("Authorization") != expected:
                response = JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "UNAUTHORIZED",
                            "message": "Bearer token is missing or invalid",
                            "request_id": request_id,
                            "details": {},
                        }
                    },
                )
                response.headers["X-Request-ID"] = request_id
                return response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if (
            request.url.path in {
                "/",
                "/plan",
                "/monitor",
                "/strategy",
                "/research",
                "/stocks",
                "/api/v1/strategy-config",
            }
            or request.url.path.startswith("/stocks/")
            or request.url.path.startswith("/api/v1/stocks")
            or request.url.path.startswith("/api/v1/research")
            or request.url.path.endswith((".css", ".js"))
        ):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        request_total.labels(
            request.method,
            path,
            str(response.status_code),
        ).inc()
        request_duration.labels(request.method, path).observe(
            time.perf_counter() - started
        )
        return response

    def resolve_trade_date(trade_date: str):
        try:
            requested_date = date.fromisoformat(trade_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="日期无效") from exc
        is_trading_session = getattr(
            services.repository,
            "is_trading_session",
            None,
        )
        if is_trading_session is None or is_trading_session(requested_date):
            return trade_date, False
        previous_session = getattr(
            services.repository,
            "previous_trading_session",
            None,
        )
        resolved = (
            previous_session(requested_date)
            if previous_session is not None
            else None
        )
        if resolved is None:
            raise HTTPException(
                status_code=404,
                detail="所选日期之前没有可用交易日",
            )
        return resolved, True

    def active_plan(trade_date: Optional[str] = None, strategy_id: Optional[str] = None):
        active = active_strategy() if not strategy_id else None
        strategy_id = strategy_id or (active["strategy_id"] if active else None)
        if strategy_id:
            require_strategy(strategy_id)
        requested_trade_date = trade_date
        resolved_from_non_trading_day = False
        if trade_date:
            trade_date, resolved_from_non_trading_day = resolve_trade_date(
                trade_date
            )

        def with_requested_date(plan):
            if not resolved_from_non_trading_day:
                return plan
            return {
                **plan,
                "requested_date": requested_trade_date,
                "resolved_from_non_trading_day": True,
            }

        day = (
            services.repository.get_strategy_day(strategy_id, trade_date)
            if strategy_id and trade_date
            else None
        )
        execution = day.get("execution_plan") if day else None
        actuals = day.get("actuals", {}) if day else {}
        if execution and "outcomes" in actuals:
            return with_requested_date({
                "plan_id": execution.get("plan_id") or "",
                "reference_date": execution["as_of"],
                "trade_date": trade_date,
                "strategy_version": execution.get("strategy_version", ""),
                "candidates": [
                    {**candidate, "symbol": candidate["code"]}
                    for candidate in execution["candidates"]
                ],
            })
        plan = services.repository.get_active_plan(
            trade_date, **({"strategy_id": strategy_id} if strategy_id else {})
        )
        if plan is None and day:
            if execution:
                plan = {
                    "plan_id": execution.get("plan_id") or "",
                    "reference_date": execution["as_of"], "trade_date": trade_date,
                    "strategy_version": execution.get("strategy_version", ""),
                    "candidates": [{**c, "symbol": c["code"]} for c in execution["candidates"]],
                }
            elif day:
                plan = {"plan_id": "", "reference_date": None, "trade_date": trade_date,
                        "strategy_version": "", "candidates": []}
        if plan is None:
            raise HTTPException(status_code=503, detail="active plan is unavailable")
        return with_requested_date(plan)

    config_store = services.config_store or StrategyConfigStore(Path("config/strategy.json"))
    history_provider = services.history_provider or MootdxProvider()
    market_history = services.market_history
    reference_repository = (
        services.repository
        if all(
            callable(getattr(services.repository, name, None))
            for name in (
                "list_stock_blocks",
                "list_securities",
                "get_security",
                "latest_market_reference_snapshot",
            )
        )
        else None
    )

    def reference_metadata() -> dict[str, Any]:
        if reference_repository is None:
            return {
                "source": getattr(history_provider, "source_name", "mootdx"),
                "snapshot": None,
            }
        snapshot = reference_repository.latest_market_reference_snapshot()
        return {
            "source": "postgres",
            "snapshot": snapshot,
            **(
                {
                    key: snapshot.get(key)
                    for key in (
                        "source_as_of",
                        "fetched_at",
                        "snapshot_id",
                        "data_state",
                        "stale_after",
                    )
                }
                if snapshot
                else {}
            ),
        }

    def reference_security(symbol: str):
        MootdxProvider._validate_symbol(symbol)
        if reference_repository is not None:
            return reference_repository.get_security(symbol)
        return history_provider.security(symbol)

    def reference_quotes(symbols):
        if reference_repository is None:
            return history_provider.quote_snapshots(symbols)
        quotes = {}
        for symbol in symbols:
            try:
                cached = services.cache.get_latest_quote(symbol)
            except Exception:
                cached = None
            if cached:
                quotes[symbol] = dict(cached.get("payload") or cached)
        return quotes

    def history_items(
        symbol: str,
        period: str,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 800,
        refresh: bool = False,
    ):
        stored = []
        complete_required = period != "minute" and limit >= 20000
        if market_history is not None and not refresh:
            stored = market_history.get_history_bars(
                symbol,
                period,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
        sync = None
        get_sync = (
            getattr(market_history, "get_history_sync", None)
            if market_history is not None
            else None
        )
        if complete_required and get_sync is not None:
            sync = get_sync(symbol, period)
        if stored and (
            not complete_required
            or (sync is not None and sync.get("completed"))
        ):
            return stored, "clickhouse"

        if period == "minute":
            if start_date is None or end_date is None or start_date != end_date:
                raise ValueError("分时历史必须指定单个交易日")
            fetched = history_provider.historical_minutes(symbol, start_date)
        else:
            fetched = history_provider.history_bars(
                symbol,
                period,
                end_date=end_date,
                max_bars=max(limit, 800),
            )
            if start_date is not None:
                fetched = [
                    item
                    for item in fetched
                    if date.fromisoformat(item["date"]) >= start_date
                ]
        if market_history is not None and fetched:
            market_history.upsert_history_bars(symbol, period, fetched)
            mark_sync = getattr(
                market_history,
                "mark_history_sync",
                None,
            )
            if period != "minute" and mark_sync is not None:
                mark_sync(
                    symbol,
                    period,
                    row_count=len(fetched),
                    completed=max(limit, 800) >= 20000,
                )
        return fetched[-limit:], getattr(history_provider, "source_name", "mootdx")

    def stock_quote(raw: Mapping[str, Any]):
        previous_close = raw.get("last_close", raw.get("previous_close"))
        current = raw.get("price")
        change_pct = None
        if current is not None and previous_close:
            change_pct = round(
                (float(current) / float(previous_close) - 1) * 100,
                4,
            )
        return {
            **dict(raw),
            "previous_close": previous_close,
            "change_pct": change_pct,
        }

    def require_strategy(strategy_id):
        try:
            uuid.UUID(strategy_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="策略 ID 无效")
        item = services.repository.get_strategy(strategy_id)
        if item is None:
            raise HTTPException(status_code=404, detail="策略不存在")
        return item

    def active_strategy():
        getter = getattr(services.repository, "get_active_strategy", None)
        if getter is None:
            return None
        item = getter()
        if item is None:
            raise HTTPException(status_code=409, detail="当前没有激活策略，请先在策略管理中激活一条策略")
        return item

    def selected_store(strategy_id):
        item = require_strategy(strategy_id) if strategy_id else active_strategy()
        return CatalogConfigStore(services.repository, item["strategy_id"]) if item else config_store

    def catalog_response(item):
        result = dict(item)
        payload = selected_store(result["strategy_id"]).payload()
        is_initial = result["code"] == STRATEGY_CODE
        result["revision"] = payload["revision"]
        result["is_initial"] = is_initial
        result["permissions"] = {
            "edit_parameters": is_initial,
            "save_as": is_initial,
            "delete": not is_initial,
        }
        result["key_parameters"] = {
            key: payload["config"][key]
            for key in STRATEGY_LIST_PARAMETER_KEYS
        }
        get_history = getattr(
            services.repository,
            "get_latest_strategy_history",
            None,
        )
        result["history_generation"] = (
            get_history(result["strategy_id"]) if get_history else None
        )
        result.pop("config", None)
        return result

    @app.get("/api/v1/strategies")
    def strategies(
        q: Optional[str] = None,
        status: str = "all",
        limit: int = 100,
        offset: int = 0,
    ):
        if status not in {"all", "active", "inactive"}:
            raise HTTPException(status_code=400, detail="status 必须是 all、active 或 inactive")
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(status_code=400, detail="limit 必须为 1..200，offset 不能小于 0")
        items = services.repository.list_strategies()
        if q:
            keyword = q.strip().casefold()
            items = [
                item for item in items
                if keyword in item["name"].casefold()
                or keyword in item["code"].casefold()
            ]
        if status != "all":
            enabled = status == "active"
            items = [item for item in items if item["enabled"] is enabled]
        total = len(items)
        return {
            "items": [
                catalog_response(item)
                for item in items[offset:offset + limit]
            ],
            "total": total,
        }

    @app.post("/api/v1/strategies", status_code=201)
    async def create_strategy(request: Request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求必须为对象")
            if set(payload) - {
                "name",
                "parent_strategy_id",
                "config",
                "revision",
                "activate",
                "history_range",
            }:
                raise ValueError("请求字段无效")
            if "activate" in payload and not isinstance(payload["activate"], bool):
                raise ValueError("activate 必须为布尔值")
            parent_id = payload.get("parent_strategy_id")
            if not parent_id:
                raise ValueError("必须指定来源策略")
            parent = require_strategy(parent_id)
            if parent["code"] != STRATEGY_CODE:
                raise ValueError("只有初始策略支持修改参数并另存")
            parent_payload = selected_store(parent_id).payload()
            if payload.get("revision") != parent_payload["revision"]:
                raise ConfigConflict("来源策略已变化，请重新加载")
            if "config" not in payload:
                raise ValueError("另存策略必须提交修改后的完整参数")
            config = payload["config"]
            validated = asdict(StrategyConfig.from_mapping(config))
            changes = {
                key: {"from": parent_payload["config"].get(key), "to": value}
                for key, value in validated.items()
                if parent_payload["config"].get(key) != value
            }
            if not changes:
                raise ValueError("参数未变更，不能另存为新策略")
            history_range = payload.get("history_range", "1y")
            if history_range not in HISTORY_RANGE_DAYS:
                raise ValueError("历史范围必须是 1d、1w、1m 或 1y")
            start, end = history_window(history_range)
            item = services.repository.create_strategy(
                payload.get("name"), validated, parent_strategy_id=parent_id,
                config_changes=changes, enabled=payload.get("activate") is True,
                materialization={
                    "history_range": history_range,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "requested_by": request.state.request_id,
                    "datasets": ["next_plan", "intraday_monitor"],
                },
            )
            return catalog_response(item)
        except ConfigConflict as exc:
            return error_response(request, status_code=409, code="CONFIG_CONFLICT", message=str(exc))
        except (ConfigError, ValueError, TypeError) as exc:
            return error_response(request, status_code=422, code="VALIDATION_ERROR", message=str(exc))

    @app.patch("/api/v1/strategies/{strategy_id}")
    async def update_strategy(strategy_id: str, request: Request):
        require_strategy(strategy_id)
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) not in ({"enabled"}, {"name"}):
                raise ValueError("仅允许单独修改策略名称或激活状态")
            if "name" in payload:
                item = services.repository.rename_strategy(strategy_id, payload["name"])
            else:
                item = services.repository.update_strategy(
                    strategy_id, enabled=payload["enabled"], archived=False,
                )
            return catalog_response(item)
        except (ValueError, TypeError) as exc:
            return error_response(request, status_code=422, code="VALIDATION_ERROR", message=str(exc))

    @app.delete("/api/v1/strategies/{strategy_id}", status_code=204)
    def delete_strategy(strategy_id: str):
        strategy = require_strategy(strategy_id)
        if strategy["code"] == STRATEGY_CODE:
            raise HTTPException(status_code=409, detail="初始策略不可删除")

        def cleanup(manifest):
            remove_objects = getattr(services.object_store, "remove_objects", None)
            if remove_objects is not None:
                remove_objects(manifest["object_keys"])
            delete_cache = getattr(services.cache, "delete_strategy_data", None)
            if delete_cache is not None:
                delete_cache(
                    strategy_id,
                    manifest["plan_ids"],
                    manifest["trade_dates"],
                )
            delete_reports = getattr(services.reports, "delete_strategy", None)
            if delete_reports is not None:
                delete_reports(strategy_id)

        try:
            services.repository.delete_strategy(strategy_id, cleanup=cleanup)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"策略关联数据删除失败：{exc}",
            ) from exc
        return Response(status_code=204)

    @app.get("/api/v1/strategy-jobs/{job_id}")
    def strategy_job(job_id: str):
        try:
            uuid.UUID(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="job_id 必须是有效 UUID") from exc
        result = services.repository.get_job_execution(job_id)
        if result is None or result.get("job_type") != "strategy_history":
            raise HTTPException(status_code=404, detail="策略历史生成任务不存在")
        return result

    @app.get("/api/v1/strategies/{strategy_id}/days")
    def strategy_days(strategy_id: str, limit: int = 100, before: Optional[str] = None):
        require_strategy(strategy_id)
        if not 1 <= limit <= 200:
            raise HTTPException(status_code=400, detail="limit must be 1..200")
        if before:
            try:
                date.fromisoformat(before)
            except ValueError:
                raise HTTPException(status_code=400, detail="日期无效")
        return {"items": services.repository.list_strategy_days(strategy_id, limit, before)}

    @app.get("/api/v1/strategies/{strategy_id}/days/{trade_date}")
    def strategy_day(strategy_id: str, trade_date: str):
        require_strategy(strategy_id)
        try:
            result = services.repository.get_strategy_day(strategy_id, trade_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="日期无效")
        if result is None:
            raise HTTPException(status_code=404, detail="此交易日暂无记录")
        return result

    @app.get("/api/v1/strategy-config")
    def strategy_config(strategy_id: Optional[str] = None):
        try:
            return selected_store(strategy_id).payload()
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=f"无法读取策略参数：{exc}") from exc

    @app.put("/api/v1/strategy-config")
    async def save_strategy_config(request: Request, strategy_id: Optional[str] = None):
        if not hasattr(services.repository, "get_active_strategy"):
            try:
                payload = await request.json()
                return config_store.save(payload["config"], payload["revision"])
            except ConfigConflict as exc:
                return error_response(request, status_code=409, code="CONFIG_CONFLICT", message=str(exc))
            except ConfigError as exc:
                return error_response(request, status_code=422, code="VALIDATION_ERROR",
                                      message=str(exc), details={"fields": exc.errors})
        return error_response(
            request, status_code=409, code="IMMUTABLE_STRATEGY",
            message="已保存策略不可覆盖，请保存为新策略",
        )

    @app.get("/api/v1/health/live")
    def health_live():
        return {"status": "ok", "server_time": _now()}

    @app.get("/api/v1/health/ready")
    def health_ready():
        dependencies = {
            "postgres": "ok" if services.repository.ready() else "unavailable",
            "redis": "ok" if services.cache.ready() else "degraded",
        }
        if services.kafka_ready is not None:
            dependencies["kafka"] = (
                "ok" if services.kafka_ready() else "unavailable"
            )
        if services.clickhouse_ready is not None:
            dependencies["clickhouse"] = (
                "ok" if services.clickhouse_ready() else "degraded"
            )
        required_ready = all(
            dependencies.get(name) == "ok" for name in ("postgres", "kafka")
            if name in dependencies
        )
        status = "ready" if required_ready else "not_ready"
        return JSONResponse(
            status_code=200 if required_ready else 503,
            content={
                "status": status,
                "server_time": _now(),
                "dependencies": dependencies,
            },
        )

    @app.get("/api/v1/system/status")
    def system_status():
        ready = health_ready()
        payload = json.loads(ready.body)
        return {
            "server_time": _now(),
            "collection": {"mode": "kafka", "source": "mootdx"},
            "messaging": {"broker": payload["dependencies"].get("kafka", "unknown")},
            "storage": payload["dependencies"],
        }

    @app.get("/api/v1/trading-calendar")
    def trading_calendar():
        today = datetime.now(SHANGHAI).date()
        sessions = services.repository.list_trading_sessions(today)
        today_value = today.isoformat()
        previous = next(
            (session for session in reversed(sessions) if session < today_value),
            None,
        )
        current_is_trading_day = today_value in sessions
        return {
            "today": today_value,
            "current_is_trading_day": current_is_trading_day,
            "sessions": sessions,
            "defaults": {
                "next_plan": previous,
                "monitor": today_value if current_is_trading_day else previous,
            },
        }

    @app.get("/api/v1/stock-blocks")
    def stock_blocks():
        try:
            if reference_repository is not None:
                items = reference_repository.list_stock_blocks()
            else:
                items = history_provider.stock_blocks()
            metadata = reference_metadata()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"板块主数据暂不可用：{exc}",
            ) from exc
        return {
            **metadata,
            "items": items,
        }

    @app.get("/api/v1/stocks")
    def stocks(
        q: Optional[str] = None,
        board: str = "all",
        market: str = "all",
        block: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ):
        if board not in {"all", "main", "gem", "star"}:
            raise HTTPException(status_code=400, detail="股票板块无效")
        if market not in {"all", "sh", "sz"}:
            raise HTTPException(status_code=400, detail="交易市场无效")
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(
                status_code=400,
                detail="limit 必须为 1 至 100，offset 不能小于 0",
            )
        blockname = (block or "").strip()
        if len(blockname) > 40:
            raise HTTPException(status_code=400, detail="股票板块无效")
        try:
            keyword = (q or "").strip().lower()
            if reference_repository is not None:
                result = reference_repository.list_securities(
                    query=keyword or None,
                    board=board,
                    market=market,
                    block=blockname or None,
                    limit=limit,
                    offset=offset,
                )
                total = result["total"]
                page = result["items"]
            else:
                universe = history_provider.securities()
                block_symbols = (
                    set(history_provider.stock_block_symbols(blockname))
                    if blockname
                    else None
                )
                filtered = [
                    item
                    for item in universe
                    if (board == "all" or item["board"] == board)
                    and (market == "all" or item["market"] == market)
                    and (
                        block_symbols is None
                        or item["symbol"] in block_symbols
                    )
                    and (
                        not keyword
                        or keyword in item["symbol"]
                        or keyword in item["name"].lower()
                    )
                ]
                total = len(filtered)
                page = filtered[offset : offset + limit]
            quotes = reference_quotes(
                [item["symbol"] for item in page]
            )
            metadata = reference_metadata()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"股票主数据暂不可用：{exc}",
            ) from exc
        return {
            **metadata,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    **item,
                    "quote": stock_quote(quotes.get(item["symbol"], {})),
                }
                for item in page
            ],
        }

    @app.get("/api/v1/stocks/{symbol}")
    def stock_detail(symbol: str):
        try:
            security = reference_security(symbol)
            if security is None:
                raise HTTPException(status_code=404, detail="股票不存在")
            quotes = reference_quotes([symbol])
            profile = history_provider.company_profile(symbol)
            metadata = reference_metadata()
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"mootdx 股票资料暂不可用：{exc}",
            ) from exc
        return {
            **security,
            **metadata,
            "profile_source": getattr(
                history_provider,
                "source_name",
                "mootdx",
            ),
            "quote": stock_quote(quotes.get(symbol, {})),
            **profile,
        }

    @app.get("/api/v1/stocks/{symbol}/company")
    def stock_company_section(symbol: str, section: str):
        try:
            if reference_security(symbol) is None:
                raise HTTPException(status_code=404, detail="股票不存在")
            content = history_provider.company_section(symbol, section)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"mootdx 公司资料暂不可用：{exc}",
            ) from exc
        return {
            "symbol": symbol,
            "section": section,
            "source": getattr(history_provider, "source_name", "mootdx"),
            "content": content,
        }

    @app.get("/api/v1/stocks/{symbol}/history")
    def stock_history(
        symbol: str,
        period: str = "day",
        trade_date: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1200,
        refresh: bool = False,
    ):
        if period not in {"minute", "day", "week", "month", "year"}:
            raise HTTPException(status_code=400, detail="历史周期无效")
        if not 1 <= limit <= 20000:
            raise HTTPException(status_code=400, detail="limit 必须为 1 至 20000")
        try:
            security = reference_security(symbol)
            if security is None:
                raise HTTPException(status_code=404, detail="股票不存在")
            if period == "minute":
                if not trade_date:
                    raise ValueError("分时历史必须指定交易日")
                first = last = date.fromisoformat(trade_date)
            else:
                first = date.fromisoformat(start_date) if start_date else None
                last = (
                    date.fromisoformat(end_date)
                    if end_date
                    else datetime.now(SHANGHAI).date()
                )
            items, served_by = history_items(
                symbol,
                period,
                start_date=first,
                end_date=last,
                limit=limit,
                refresh=refresh,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"历史行情暂不可用：{exc}",
            ) from exc
        return {
            "symbol": symbol,
            "name": security["name"],
            "period": period,
            "source": getattr(history_provider, "source_name", "mootdx"),
            "served_by": served_by,
            "count": len(items),
            "items": items,
        }

    @app.get("/api/v1/monitor")
    def monitor(trade_date: Optional[str] = None, symbols: Optional[str] = None, strategy_id: Optional[str] = None):
        active = active_strategy() if not strategy_id else None
        strategy_id = strategy_id or (active["strategy_id"] if active else None)
        plan = active_plan(trade_date, strategy_id)
        resolved_from_non_trading_day = bool(
            plan.get("resolved_from_non_trading_day")
        )
        day = (
            services.repository.get_strategy_day(
                strategy_id,
                plan["trade_date"],
            )
            if strategy_id
            else None
        )
        actuals = day.get("actuals", {}) if day else {}
        outcomes = {item["symbol"]: item for item in actuals.get("outcomes", [])}
        selected = set(symbols.split(",")) if symbols else None
        stocks = []
        ages = []
        missing = 0
        now = datetime.now(timezone.utc)
        current_session = datetime.now(SHANGHAI).date()
        plan_session = date.fromisoformat(plan["trade_date"])
        live_market_data = (
            market_history is None
            or (
                plan_session == current_session
                and services.repository.is_trading_session(current_session)
            )
        )
        for candidate in plan["candidates"]:
            symbol = candidate["symbol"]
            if selected is not None and symbol not in selected:
                continue
            saved = actuals.get("stocks", {}).get(symbol)
            quote = {}
            feature = {}
            decision = {}
            bars = []
            if live_market_data:
                quote_event = services.cache.get_latest_quote(symbol)
                get_feature = getattr(
                    services.cache,
                    "get_latest_feature",
                    None,
                )
                feature_event = get_feature(symbol) if get_feature else None
                decision_event = services.cache.get_latest_decision(
                    plan["plan_id"],
                    symbol,
                )
                quote = quote_event["payload"] if quote_event else {}
                feature = feature_event["payload"] if feature_event else {}
                if (
                    quote.get("trade_date")
                    or str(quote.get("source_time", ""))[:10]
                ) != plan["trade_date"]:
                    quote = {}
                    feature = {}
                decision = (
                    decision_event["payload"] if decision_event else {}
                )
                get_bars = getattr(services.cache, "get_minute_bars", None)
                cached_bars = (
                    get_bars(symbol, limit=240) if get_bars else ()
                )
                bars = [
                    item["payload"]
                    for item in cached_bars
                    if item.get("payload", {}).get("trade_date")
                    == plan["trade_date"]
                ]
            else:
                try:
                    daily, _ = history_items(
                        symbol,
                        "day",
                        end_date=plan_session,
                        limit=2,
                    )
                    current_bar = next(
                        (
                            item
                            for item in reversed(daily)
                            if item["date"] == plan["trade_date"]
                        ),
                        None,
                    )
                    previous_bar = next(
                        (
                            item
                            for item in reversed(daily)
                            if item["date"] < plan["trade_date"]
                        ),
                        None,
                    )
                    if current_bar:
                        quote = {
                            "trade_date": plan["trade_date"],
                            "price": current_bar.get("close"),
                            "open": current_bar.get("open"),
                            "high": current_bar.get("high"),
                            "low": current_bar.get("low"),
                            "previous_close": (
                                previous_bar.get("close")
                                if previous_bar
                                else candidate.get("latest_price")
                            ),
                            "cumulative_volume": current_bar.get("volume"),
                            "cumulative_amount_cny": current_bar.get("amount"),
                            "source_time": current_bar.get("time"),
                            "collected_at": current_bar.get("fetched_at"),
                        }
                except Exception:
                    quote = {}
                try:
                    minute_items, _ = history_items(
                        symbol,
                        "minute",
                        start_date=plan_session,
                        end_date=plan_session,
                        limit=240,
                    )
                    bars = [
                        {
                            "trade_date": plan["trade_date"],
                            "bar_time": item["time"],
                            "open": item.get("open"),
                            "high": item.get("high"),
                            "low": item.get("low"),
                            "close": item.get("close"),
                            "volume": item.get("volume"),
                            "amount_cny": item.get("amount"),
                        }
                        for item in minute_items
                    ]
                except Exception:
                    bars = []
            if not decision and saved:
                decision = saved
            outcome = outcomes.get(symbol)
            if outcome and outcome.get("status") == "observed":
                decision = {"state": "expired", "label": "收盘封板" if outcome["closed_limit_up"] else "收盘未封板",
                            "reason": outcome["reason"], "updated_at": day["updated_at"]}
            source_time = quote.get("source_time")
            if source_time and live_market_data:
                age = (
                    now
                    - datetime.fromisoformat(str(source_time)).astimezone(timezone.utc)
                ).total_seconds()
                ages.append(max(0.0, age))
            elif not source_time:
                missing += 1
            stocks.append(
                {
                    "symbol": symbol,
                    "name": candidate["name"],
                    "industry": candidate.get("industry"),
                    "rank": candidate.get("rank"),
                    "score": candidate.get("score"),
                    "origin": candidate.get("origin"),
                    "eligible": candidate.get("eligible", True),
                    "eligibility_reason": candidate.get(
                        "eligibility_reason",
                    ),
                    "reference_date": plan["reference_date"],
                    "plan_date": plan["trade_date"],
                    "source_time": source_time or _now(),
                    "collected_at": quote.get("collected_at") or _now(),
                    "price": quote.get("price"),
                    "open": quote.get("open"),
                    "high": quote.get("high"),
                    "low": quote.get("low"),
                    "previous_close": quote.get("previous_close"),
                    "change_pct": (
                        round(
                            (
                                float(quote["price"])
                                / float(quote["previous_close"])
                                - 1
                            )
                            * 100,
                            4,
                        )
                        if quote.get("price") is not None
                        and quote.get("previous_close")
                        else None
                    ),
                    "open_change_pct": (
                        round(
                            (
                                float(quote["open"])
                                / float(quote["previous_close"])
                                - 1
                            )
                            * 100,
                            4,
                        )
                        if quote.get("open") is not None
                        and quote.get("previous_close")
                        else None
                    ),
                    "amount_cny": quote.get("cumulative_amount_cny"),
                    "volume": quote.get("cumulative_volume"),
                    "bid1": quote.get("bid1"),
                    "ask1": quote.get("ask1"),
                    "feature": {
                        "minute_volume_ratio": feature.get(
                            "minute_volume_ratio"
                        ),
                        "sector_rise_ratio": feature.get("sector_rise_ratio"),
                        "sector_sample_size": feature.get(
                            "attributes",
                            {},
                        ).get("sector_sample_size"),
                        "data_state": feature.get(
                            "data_state",
                            "unavailable",
                        ),
                        "computed_at": feature.get("computed_at"),
                    },
                    "candles": [
                        {
                            "time": bar.get("bar_time"),
                            "price": bar.get("close"),
                            "open": bar.get("open"),
                            "high": bar.get("high"),
                            "low": bar.get("low"),
                            "close": bar.get("close"),
                            "volume": bar.get("volume"),
                            "amount": bar.get("amount_cny"),
                        }
                        for bar in bars
                    ],
                    "plan": candidate.get("plan", {}),
                    "decision": {
                        "state": decision.get("state", "unavailable"),
                        "label": decision.get("label", "等待实时决策"),
                        "reason_code": decision.get(
                            "reason_code",
                            "PROJECTION_UNAVAILABLE",
                        ),
                        "reason": decision.get(
                            "reason",
                            "Redis 投影尚未收到该股票的策略决策。",
                        ),
                        "irreversible": bool(
                            decision.get("irreversible", False)
                        ),
                        "updated_at": decision.get("updated_at") or _now(),
                    },
                }
            )
        max_age = max(ages) if ages else None
        current_phase = phase_at(datetime.now(SHANGHAI))
        data_state = (
            _data_state(max_age, missing)
            if live_market_data
            else ("historical" if missing == 0 else "unavailable")
        )
        return {
            "trade_date": plan["trade_date"],
            "requested_date": plan.get(
                "requested_date",
                plan["trade_date"],
            ),
            "reference_date": plan["reference_date"],
            "plan_id": plan["plan_id"],
            "strategy_version": plan["strategy_version"],
            "server_time": _now(),
            "data_status": {
                "state": data_state,
                "max_quote_age_seconds": max_age,
                "consumer_lag": None,
                "reason": (
                    "non_trading_day_fallback"
                    if resolved_from_non_trading_day
                    else (
                        "historical_market_data"
                        if not live_market_data
                        else None
                    )
                ),
            },
            "phase": current_phase,
            "phase_label": PHASE_LABELS[current_phase],
            "stocks": stocks,
        }

    @app.get("/api/v1/monitor/kline/{symbol}")
    def monitor_kline(
        symbol: str,
        period: str = "day",
        trade_date: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ):
        if len(symbol) != 6 or not symbol.isdigit():
            raise HTTPException(status_code=400, detail="股票代码必须为六位数字")
        plan = active_plan(trade_date, strategy_id)
        if symbol not in {
            candidate["symbol"] for candidate in plan["candidates"]
        }:
            raise HTTPException(status_code=404, detail="股票不在所选当日实盘计划中")
        try:
            items = history_provider.kline_bars(
                symbol,
                period,
                date.fromisoformat(plan["trade_date"]),
                limit=120,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"mootdx K线暂不可用：{exc}",
            ) from exc
        return {
            "symbol": symbol,
            "period": period,
            "trade_date": plan["trade_date"],
            "source": getattr(history_provider, "source_name", "mootdx"),
            "items": items,
        }

    @app.get("/api/v1/monitor/events")
    def monitor_events(
        symbol: Optional[str] = None,
        state: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
        strategy_id: Optional[str] = None,
        trade_date: Optional[str] = None,
    ):
        plan = active_plan(trade_date, strategy_id)
        if not plan["plan_id"]:
            return {"items": [], "next_cursor": None}
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be 1..200")
        items = list(
            services.repository.list_decision_events(
                plan_id=plan["plan_id"],
                symbol=symbol,
                state=state,
                start_time=start_time,
                end_time=end_time,
                cursor=cursor,
                limit=limit + 1,
            )
        )
        has_more = len(items) > limit
        items = items[:limit]
        next_cursor = None
        if has_more and items:
            next_cursor = (
                f"{items[-1]['occurred_at']}|{items[-1]['event_id']}"
            )
        return {
            "items": items,
            "next_cursor": next_cursor,
        }

    @app.get("/api/v1/monitor/stream")
    async def monitor_stream(request: Request, strategy_id: Optional[str] = None, trade_date: Optional[str] = None):
        plan = active_plan(trade_date, strategy_id)
        last_id = request.headers.get("Last-Event-ID", "$")

        async def events():
            cursor = last_id
            while True:
                if await request.is_disconnected():
                    return
                try:
                    rows = await asyncio.to_thread(
                        services.cache.read_monitor_events,
                        plan["trade_date"],
                        cursor,
                        block_ms=15000,
                        count=100,
                    )
                except Exception:
                    yield (
                        "event: data.degraded\n"
                        'data: {"reason":"event_stream_unavailable"}\n\n'
                    )
                    await asyncio.sleep(2)
                    continue
                if not rows:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                for cursor, envelope in rows:
                    if (envelope.get("event_type") == "strategy.decision.v1"
                            and envelope.get("payload", {}).get("plan_id") != plan["plan_id"]):
                        continue
                    event_type = (
                        "decision.changed"
                        if envelope.get("event_type") == "strategy.decision.v1"
                        else "snapshot"
                    )
                    yield (
                        f"id: {cursor}\nevent: {event_type}\n"
                        f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
                    )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/monitor/{tradeDate}/refresh", status_code=202)
    def refresh_monitor(tradeDate: str, request: Request, strategy_id: Optional[str] = None):
        try:
            execution_date = date.fromisoformat(tradeDate)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="tradeDate must be a valid ISO date",
            ) from exc
        if execution_date > datetime.now(SHANGHAI).date():
            raise HTTPException(status_code=400, detail="所选交易日不能晚于今天")
        if not services.repository.is_trading_session(execution_date):
            raise HTTPException(status_code=400, detail="所选日期不是交易日")
        reference_date = services.repository.previous_trading_session(
            execution_date
        )
        if reference_date is None:
            raise HTTPException(
                status_code=400,
                detail="交易日历中缺少所选日期的前一交易日",
            )
        selected = require_strategy(strategy_id) if strategy_id else active_strategy()
        return services.repository.enqueue_report_refresh(
            reference_date,
            requested_by=request.state.request_id,
            execution_date=tradeDate,
            **({"strategy_id": selected["strategy_id"]} if selected else {}),
        )

    @app.get("/api/v1/reports")
    def reports(limit: int = 50, cursor: Optional[str] = None, strategy_id: Optional[str] = None):
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be 1..200")
        active = active_strategy() if not strategy_id else None
        strategy_id = strategy_id or (active["strategy_id"] if active else None)
        if not strategy_id:
            all_items = services.reports.list_reports()
            if cursor is not None:
                all_items = [item for item in all_items if str(item["as_of"]) < cursor]
            selected = all_items[:limit + 1]
            return {"items": [_report_summary(item, services.strategy_version) for item in selected[:limit]],
                    "next_cursor": str(selected[limit - 1]["as_of"]) if len(selected) > limit else None}
        require_strategy(strategy_id)
        rows = services.repository.list_strategy_days(strategy_id, limit, cursor)
        return {"items": [{"trade_date": row["trade_date"], "plan_date": row["plan_date"],
                          "candidate_count": row["candidate_count"], "strategy_version": row["strategy_version"]}
                         for row in rows if row["plan_status"] == "ready"],
                "next_cursor": rows[-1]["trade_date"] if len(rows) == limit else None}

    @app.get("/api/v1/reports/{tradeDate}")
    def report(tradeDate: str, strategy_id: Optional[str] = None):
        resolved_date, resolved_from_non_trading_day = resolve_trade_date(
            tradeDate
        )
        active = active_strategy() if not strategy_id else None
        strategy_id = strategy_id or (active["strategy_id"] if active else None)
        value = strategy_day(strategy_id, resolved_date)["next_plan"] if strategy_id else services.reports.get(resolved_date)
        if value is None:
            raise HTTPException(status_code=404, detail="report not found")
        payload = _report_payload(value, services.strategy_version)
        if resolved_from_non_trading_day:
            payload["requested_date"] = tradeDate
            payload["resolved_from_non_trading_day"] = True
        return payload

    @app.post("/api/v1/reports/{tradeDate}/refresh", status_code=202)
    def refresh_report(tradeDate: str, request: Request, strategy_id: Optional[str] = None):
        try:
            requested_date = date.fromisoformat(tradeDate)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="tradeDate must be a valid ISO date",
            ) from exc
        if not services.repository.is_trading_session(requested_date):
            raise HTTPException(status_code=400, detail="所选日期不是交易日")
        now = datetime.now(SHANGHAI)
        if requested_date > now.date() or (requested_date == now.date() and now.hour < 15):
            raise HTTPException(status_code=400, detail="该交易日尚未收盘，暂不能生成收盘计划")
        selected = require_strategy(strategy_id) if strategy_id else active_strategy()
        return services.repository.enqueue_report_refresh(
            tradeDate,
            requested_by=request.state.request_id,
            **({"strategy_id": selected["strategy_id"]} if selected else {}),
        )

    @app.get("/api/v1/report-jobs/{jobId}")
    def report_job(jobId: str):
        try:
            uuid.UUID(jobId)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="jobId must be a valid UUID",
            ) from exc
        result = services.repository.get_job_execution(jobId)
        if result is None or result.get("job_type") != "report_refresh":
            raise HTTPException(status_code=404, detail="report job not found")
        return result

    @app.get("/api/v1/reports/{tradeDate}/assets/{format}")
    def report_asset(tradeDate: str, format: str, strategy_id: Optional[str] = None):
        active = active_strategy() if not strategy_id else None
        strategy_id = strategy_id or (active["strategy_id"] if active else None)
        if strategy_id:
            require_strategy(strategy_id)
        filenames = {
            "markdown": "report.md",
            "json": "candidates.json",
            "csv": "candidates.csv",
        }
        filename = filenames.get(format)
        if filename is None:
            raise HTTPException(status_code=404, detail="asset format not found")
        get_asset = getattr(services.repository, "get_report_asset", None)
        if services.object_store is not None and get_asset is not None:
            asset = get_asset(tradeDate, format, **({"strategy_id": strategy_id} if strategy_id else {}))
            if asset is not None:
                return RedirectResponse(
                    services.object_store.presigned_get_url(
                        asset["object_key"],
                        expires_seconds=300,
                    ),
                    status_code=302,
                )
        for root in services.reports.roots:
            scoped = root / "strategies" / strategy_id if strategy_id else root
            path = scoped / tradeDate / filename
            if path.is_file():
                return FileResponse(path)
        raise HTTPException(status_code=404, detail="asset not found")

    @app.get("/api/v1/strategy-runs/{runId}")
    def strategy_run(runId: str):
        result = services.repository.get_strategy_run(runId)
        if result is None:
            raise HTTPException(status_code=404, detail="strategy run not found")
        return result

    @app.get("/api/health", deprecated=True)
    def legacy_health():
        return health_live()

    @app.get("/api/reports", deprecated=True)
    def legacy_reports():
        items = services.reports.list_reports()
        return {"reports": items, "count": len(items)}

    @app.get("/api/reports/latest", deprecated=True)
    def legacy_latest_report():
        value = services.reports.latest()
        if value is None:
            raise HTTPException(status_code=404, detail="report not found")
        return value

    @app.get("/api/reports/{trade_date}", deprecated=True)
    def legacy_report(trade_date: str):
        value = services.reports.get(trade_date)
        if value is None:
            raise HTTPException(status_code=404, detail="report not found")
        return value

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(
            generate_latest(metrics_registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    static_root = Path(__file__).with_name("web")

    @app.get("/api/v1/research")
    def research_runs():
        reader = getattr(services.repository, "list_research_runs", None)
        if reader is None:
            return {"items": []}
        return {"items": reader()}

    @app.get("/api/v1/research/comparison")
    def execution_comparison():
        reader = getattr(services.repository, "get_execution_comparison", None)
        result = reader() if reader else None
        if result is None:
            raise HTTPException(status_code=404, detail="尚无实盘成功率对比数据")
        def compact_summary(summary):
            return {
                key: value for key, value in summary.items()
                if key != "success_stocks"
            }
        return {
            **{key: result[key] for key in (
                "schema_version", "generated_at", "start", "end", "source", "metric"
            )},
            "strategies": [
                {
                    **{
                        key: strategy[key]
                        for key in (
                            "strategy_id", "strategy_code", "strategy_name",
                            "enabled",
                        )
                    },
                    "summary": compact_summary(strategy["summary"]),
                    "periods": {
                        frequency: [
                            compact_summary(item)
                            for item in rows
                        ]
                        for frequency, rows in strategy["periods"].items()
                    },
                    "days": [
                        {
                            **{key: day[key] for key in ("trade_date", "reference_date")},
                            "summary": compact_summary(day["summary"]),
                            "stocks": [
                                stock for stock in day["stocks"] if stock.get("buyable")
                            ],
                        }
                        for day in strategy["days"]
                    ],
                }
                for strategy in result["strategies"]
            ],
        }

    @app.get("/api/v1/research/{run_id}")
    def research_run(run_id: str):
        try:
            uuid.UUID(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid research run id") from exc
        reader = getattr(services.repository, "get_research_run", None)
        result = reader(run_id) if reader else None
        if result is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return result

    @app.get("/api/v1/research/{run_id}/strategy")
    def research_strategy(run_id: str):
        result = research_run(run_id)
        return Response(
            json.dumps(result["strategy"]["config"], ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="strategy-{run_id}.json"'},
        )

    @app.get("/api/v1/research/{run_id}/assets/{filename}")
    def research_asset(run_id: str, filename: str):
        result = research_run(run_id)
        asset = next((item for item in result["assets"] if item["filename"] == filename), None)
        if asset is None or services.object_store is None:
            raise HTTPException(status_code=404, detail="research asset not found")
        response = services.object_store.client.get_object(
            services.object_store.bucket, asset["object_key"],
        )
        def chunks():
            try:
                yield from response.stream(64 * 1024)
            finally:
                response.close()
                response.release_conn()
        return StreamingResponse(chunks(), media_type=asset["content_type"],
                                 headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/")
    def stocks_home():
        return FileResponse(static_root / "stocks.html")

    @app.get("/plan")
    def dashboard():
        return FileResponse(static_root / "index.html")

    @app.get("/monitor")
    def monitor_dashboard():
        return FileResponse(static_root / "monitor.html")

    @app.get("/strategy")
    def strategy_dashboard():
        return FileResponse(static_root / "strategy.html")

    @app.get("/research")
    def research_dashboard():
        return FileResponse(static_root / "research.html")

    @app.get("/stocks")
    def stocks_dashboard():
        return FileResponse(static_root / "stocks.html")

    @app.get("/stocks/{symbol}/history")
    def stock_history_dashboard(symbol: str):
        return FileResponse(static_root / "stock-history.html")

    @app.get("/stocks/{symbol}")
    def stock_dashboard(symbol: str):
        return FileResponse(static_root / "stock.html")

    app.mount("/", StaticFiles(directory=static_root), name="static")
    return app
