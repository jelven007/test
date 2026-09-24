import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .domain.intraday import PHASE_LABELS, SHANGHAI, phase_at
from .web_server import ReportStore


@dataclass
class ApiServices:
    reports: ReportStore
    repository: Any
    cache: Any
    object_store: Optional[Any] = None
    api_token: Optional[str] = None
    kafka_ready: Optional[Callable[[], bool]] = None
    clickhouse_ready: Optional[Callable[[], bool]] = None
    strategy_version: str = "v1"


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
        "strategy_version": strategy_version,
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
        "strategy_version": strategy_version,
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
            request.url.path in {"/", "/monitor"}
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

    def active_plan(trade_date: Optional[str] = None):
        plan = services.repository.get_active_plan(trade_date)
        if plan is None:
            raise HTTPException(status_code=503, detail="active plan is unavailable")
        return plan

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

    @app.get("/api/v1/monitor")
    def monitor(trade_date: Optional[str] = None, symbols: Optional[str] = None):
        plan = active_plan(trade_date)
        selected = set(symbols.split(",")) if symbols else None
        stocks = []
        ages = []
        missing = 0
        now = datetime.now(timezone.utc)
        for candidate in plan["candidates"]:
            symbol = candidate["symbol"]
            if selected is not None and symbol not in selected:
                continue
            quote_event = services.cache.get_latest_quote(symbol)
            get_feature = getattr(services.cache, "get_latest_feature", None)
            feature_event = get_feature(symbol) if get_feature else None
            decision_event = services.cache.get_latest_decision(
                plan["plan_id"],
                symbol,
            )
            quote = quote_event["payload"] if quote_event else {}
            feature = feature_event["payload"] if feature_event else {}
            decision = decision_event["payload"] if decision_event else {}
            get_bars = getattr(services.cache, "get_minute_bars", None)
            cached_bars = get_bars(symbol, limit=240) if get_bars else ()
            bars = [
                item["payload"]
                for item in cached_bars
                if item.get("payload", {}).get("trade_date")
                == plan["trade_date"]
            ]
            source_time = quote.get("source_time")
            if source_time:
                age = (
                    now
                    - datetime.fromisoformat(str(source_time)).astimezone(timezone.utc)
                ).total_seconds()
                ages.append(max(0.0, age))
            else:
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
        return {
            "trade_date": plan["trade_date"],
            "reference_date": plan["reference_date"],
            "plan_id": plan["plan_id"],
            "strategy_version": plan["strategy_version"],
            "server_time": _now(),
            "data_status": {
                "state": _data_state(max_age, missing),
                "max_quote_age_seconds": max_age,
                "consumer_lag": None,
            },
            "phase": current_phase,
            "phase_label": PHASE_LABELS[current_phase],
            "stocks": stocks,
        }

    @app.get("/api/v1/monitor/events")
    def monitor_events(
        symbol: Optional[str] = None,
        state: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ):
        plan = active_plan()
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
    async def monitor_stream(request: Request):
        plan = active_plan()
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

    @app.get("/api/v1/reports")
    def reports(limit: int = 50, cursor: Optional[str] = None):
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=400, detail="limit must be 1..200")
        all_items = services.reports.list_reports()
        if cursor is not None:
            all_items = [
                item for item in all_items if str(item["as_of"]) < cursor
            ]
        selected = all_items[: limit + 1]
        has_more = len(selected) > limit
        selected = selected[:limit]
        return {
            "items": [
                _report_summary(item, services.strategy_version)
                for item in selected
            ],
            "next_cursor": (
                str(selected[-1]["as_of"])
                if has_more and selected
                else None
            ),
        }

    @app.get("/api/v1/reports/{tradeDate}")
    def report(tradeDate: str):
        value = services.reports.get(tradeDate)
        if value is None:
            raise HTTPException(status_code=404, detail="report not found")
        return _report_payload(value, services.strategy_version)

    @app.post("/api/v1/reports/{tradeDate}/refresh", status_code=202)
    def refresh_report(tradeDate: str, request: Request):
        try:
            date.fromisoformat(tradeDate)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="tradeDate must be a valid ISO date",
            ) from exc
        return services.repository.enqueue_report_refresh(
            tradeDate,
            requested_by=request.state.request_id,
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
    def report_asset(tradeDate: str, format: str):
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
            asset = get_asset(tradeDate, format)
            if asset is not None:
                return RedirectResponse(
                    services.object_store.presigned_get_url(
                        asset["object_key"],
                        expires_seconds=300,
                    ),
                    status_code=302,
                )
        for root in services.reports.roots:
            path = root / tradeDate / filename
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

    @app.get("/")
    def dashboard():
        return FileResponse(static_root / "index.html")

    @app.get("/monitor")
    def monitor_dashboard():
        return FileResponse(static_root / "monitor.html")

    app.mount("/", StaticFiles(directory=static_root), name="static")
    return app
