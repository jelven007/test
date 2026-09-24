# 接口规范

## 1. 范围与状态

当前仓库暴露无版本的本地接口 `/api/*`。生产化目标接口统一使用 `/api/v1/*`，契约见
[OpenAPI](openapi.yaml)。Kafka 事件契约见 [AsyncAPI](asyncapi.yaml)。

版本化接口已经由 FastAPI 实现；`docs/openapi.yaml` 是对外契约基线。

## 2. 通用约定

### 2.1 协议

- HTTPS。
- JSON 使用 UTF-8。
- 时间使用 ISO 8601，必须包含时区。
- 交易日使用 `YYYY-MM-DD`。
- 股票代码使用六位字符串，不转换为数字。
- 金额使用人民币元，字段名带 `_cny`。
- 百分比使用数值百分比，例如 `5.23` 表示 `5.23%`。

### 2.2 请求追踪

客户端可以传入：

```http
X-Request-ID: 5b60f996-3be1-4d53-9988-7a9825d187af
```

服务必须在响应中返回相同或新生成的 `X-Request-ID`。日志、Trace 和错误响应统一使用该值。

### 2.3 错误结构

```json
{
  "error": {
    "code": "REPORT_NOT_FOUND",
    "message": "未找到指定交易日的报告",
    "request_id": "5b60f996-3be1-4d53-9988-7a9825d187af",
    "details": {
      "trade_date": "2026-09-23"
    }
  }
}
```

常用状态码：

| 状态码 | 场景 |
| --- | --- |
| `200` | 查询成功 |
| `202` | 异步任务已受理 |
| `400` | 参数格式错误 |
| `401` | 未认证 |
| `403` | 无权限 |
| `404` | 资源不存在 |
| `409` | 幂等冲突或状态不允许 |
| `422` | 参数合法但违反业务规则 |
| `429` | 请求频率超限 |
| `503` | 关键依赖不可用或数据尚未就绪 |

### 2.4 分页

历史列表使用游标分页：

```text
?limit=50&cursor=<opaque-token>
```

响应：

```json
{
  "items": [],
  "next_cursor": null
}
```

`limit` 默认 50，最大 200。游标是不可解释的服务端令牌。

### 2.5 鉴权

- 看板查询：OIDC 登录后使用 Bearer Token。
- 运维和重放接口：独立管理员权限。
- Kubernetes 内部服务：mTLS 和 ServiceAccount。
- 健康检查可在集群内部匿名访问。
- 系统不提供下单类权限和接口。

## 3. CURRENT 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 进程健康和服务时间 |
| GET | `/api/reports` | 报告摘要列表 |
| GET | `/api/reports/latest` | 最新报告 |
| GET | `/api/reports/{date}` | 指定参考日报告 |
| GET | `/api/monitor` | 当前内存监控快照 |

当前接口特点：

- 无认证、无 API 版本。
- 返回本地文件和进程内状态。
- 无分页、标准错误码、请求 ID 和依赖就绪检查。
- 仅适用于本机原型。

迁移期保留这些路径，但只作为 `/api/v1` 的兼容代理。Web 前端迁移完成后再删除。

## 4. HTTP 接口

### 4.1 健康与状态

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health/live` | 进程是否存活，不检查外部依赖 |
| GET | `/api/v1/health/ready` | Kafka、PostgreSQL 等关键依赖是否就绪 |
| GET | `/api/v1/system/status` | 采集延迟、策略计划和依赖摘要 |

`ready` 仅在服务能够正确处理请求时返回 200。不能因为进程存活就忽略数据库不可用。

### 4.2 盘中监控

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/monitor` | 当前计划、股票快照和最新建议 |
| GET | `/api/v1/monitor/events` | 决策事件历史，支持股票、时间和状态过滤 |
| GET | `/api/v1/monitor/stream` | SSE 实时推送快照和状态变化 |

`GET /api/v1/monitor` 示例：

```json
{
  "trade_date": "2026-09-24",
  "plan_id": "plan_20260924_v1",
  "strategy_version": "1.0.0",
  "server_time": "2026-09-24T09:38:12+08:00",
  "data_status": {
    "state": "fresh",
    "max_quote_age_seconds": 1.4,
    "consumer_lag": 0
  },
  "stocks": [
    {
      "symbol": "002909",
      "name": "集泰股份",
      "source_time": "2026-09-24T09:38:11.520+08:00",
      "collected_at": "2026-09-24T09:38:11.781+08:00",
      "price": 8.29,
      "change_pct": 9.95,
      "decision": {
        "state": "sealed",
        "label": "封板快照 · 待核验",
        "reason_code": "LIMIT_SNAPSHOT_REQUIRES_SECTOR_CONFIRMATION",
        "reason": "个股封板成立，仍需核验板块助攻和回封过程。",
        "irreversible": false,
        "updated_at": "2026-09-24T09:38:11.900+08:00"
      }
    }
  ]
}
```

SSE 事件类型：

- `snapshot`
- `decision.changed`
- `data.degraded`
- `heartbeat`

SSE 不是权威存储。客户端重连时先调用普通查询，再使用 `Last-Event-ID` 尝试补齐短期事件。

### 4.3 报告

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/reports` | 报告分页列表 |
| GET | `/api/v1/reports/{tradeDate}` | 指定参考日的完整报告 |
| GET | `/api/v1/reports/{tradeDate}/assets/{format}` | 获取报告文件或签名下载地址 |

`format` 支持 `markdown`、`json` 和 `csv`。

### 4.4 策略运行

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/strategy-runs/{runId}` | 查询运行状态、输入范围和产物 |

生产环境的 16:00 初版和 23:30 更新由调度系统发起。人工补跑属于受保护的运维接口，
不在公共 Web API 中开放。`GET /api/v1/reports/{tradeDate}` 按复盘交易日返回最后一次
成功生成的版本。

## 5. 数据状态约定

`data_status.state`：

| 值 | 含义 |
| --- | --- |
| `fresh` | 行情和依赖均在时效范围内 |
| `delayed` | 数据到达延迟，但仍在降级阈值内 |
| `stale` | 数据过期，策略判断暂停 |
| `unavailable` | 关键行情或存储不可用 |

策略状态建议使用稳定代码：

| 状态 | 说明 | 是否可能不可逆 |
| --- | --- | --- |
| `pre` | 等待竞价 | 否 |
| `auction` | 等待开盘确认 | 否 |
| `watch` | 合格区间内继续观察 | 否 |
| `sealed` | 封板快照，等待人工核验 | 否 |
| `at_limit` / `near_limit` | 触板或临近涨停，等待确认 | 否 |
| `reject_open` | 开盘超限 | 是 |
| `reject_low` | 当日跌破昨收 | 是 |
| `outside_open` | 竞价未通过 | 是 |
| `window_closed` | 超过 10:00 | 是 |
| `stale` / `unavailable` | 数据异常，暂停判断 | 否 |
| `ineligible` | 静态门槛未通过 | 是 |
| `expired` | 计划日期不匹配 | 是 |
| `no_quote` / `no_open` | 等待有效行情字段 | 否 |
| `reference_changed` | 昨收基准不一致 | 否 |
| `missing_rules` | 策略参数不完整 | 否 |

显示文案可以调整，状态代码不可随意改名。

## 6. Kafka 事件约定

公共事件信封：

```json
{
  "event_id": "sha256-value",
  "event_type": "market.quote.snapshot.v1",
  "schema_version": 1,
  "occurred_at": "2026-09-24T09:38:11.520+08:00",
  "published_at": "2026-09-24T09:38:11.810+08:00",
  "producer": "market-collector",
  "trace_id": "3f2f43d79c86488aa5f813cc6e6f48c7",
  "payload": {}
}
```

规则：

- `event_type` 包含主版本号。
- 同一主版本只允许增加可选字段，不允许改变字段语义。
- 删除字段、修改类型或改变单位必须发布新主版本 Topic。
- 消费者必须忽略未知可选字段。
- 金额、价格、时间和股票代码的格式与 HTTP API 保持一致。

## 7. API 兼容策略

1. 先实现 `/api/v1` 并完成契约测试。
2. 旧 `/api/*` 通过适配器调用新应用服务。
3. Web 看板切换到 `/api/v1`。
4. 至少保留一个发布周期的弃用提示。
5. 删除旧接口前检查访问日志，确认没有活跃客户端。

## 8. 接口验收

- OpenAPI 可以通过规范校验器。
- AsyncAPI 中所有 Topic 与生产代码配置一致。
- 请求和响应具备自动契约测试。
- 时间、金额、股票代码和状态枚举没有歧义。
- API 返回的每个策略状态都可关联到 PostgreSQL 决策事件。
- SSE 断开不会造成权威状态丢失。
- 无任何自动下单接口。
