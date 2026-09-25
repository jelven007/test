# 接口规范

## 1. 范围与状态

当前 FastAPI 服务以 `/api/v1/*` 为正式接口，契约见 [OpenAPI](openapi.yaml)。
Kafka 事件契约见 [AsyncAPI](asyncapi.yaml)。旧 `/api/*` 只保留只读兼容代理。

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

## 3. CURRENT 兼容接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 进程健康和服务时间 |
| GET | `/api/reports` | 报告摘要列表 |
| GET | `/api/reports/latest` | 最新报告 |
| GET | `/api/reports/{date}` | 指定参考日报告 |
| GET | `/api/monitor` | 已弃用；正式监控使用 `/api/v1/monitor` |

兼容接口不再扩展新功能。策略目录、任务刷新、研究和 SSE 只在 `/api/v1` 提供。

## 4. HTTP 接口

### 4.1 健康与状态

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health/live` | 进程是否存活，不检查外部依赖 |
| GET | `/api/v1/health/ready` | Kafka、PostgreSQL 等关键依赖是否就绪 |
| GET | `/api/v1/system/status` | 采集延迟、策略计划和依赖摘要 |

`ready` 仅在服务能够正确处理请求时返回 200。不能因为进程存活就忽略数据库不可用。

### 4.2 当日实盘

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/monitor` | 当前计划、股票快照和最新建议 |
| GET | `/api/v1/monitor/events` | 决策事件历史，支持股票、时间和状态过滤 |
| GET | `/api/v1/monitor/stream` | SSE 实时推送快照和状态变化 |

三个接口均支持可选 `strategy_id` 和 `trade_date`。省略 `strategy_id` 时解析当前唯一激活策略；
系统没有激活策略时返回 `409`。`monitor` 还支持逗号分隔的 `symbols`。

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
| POST | `/api/v1/reports/{tradeDate}/refresh` | 创建完整报告刷新任务 |
| GET | `/api/v1/report-jobs/{jobId}` | 查询刷新任务状态和实际执行版本 |
| GET | `/api/v1/reports/{tradeDate}/assets/{format}` | 获取报告文件或签名下载地址 |

`format` 支持 `markdown`、`json` 和 `csv`。
报告查询与下载支持可选 `strategy_id`；省略时使用激活策略。刷新请求即使携带
`strategy_id`，当前实现也只校验存在激活策略，Worker 开始时重新解析当时的激活策略。
每次刷新请求生成独立任务，前端应在任务成功后重新读取报告。

### 4.4 策略目录与配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/strategies` | 按关键词、状态列出未归档策略、权限、历史生成状态和关键参数 |
| POST | `/api/v1/strategies` | 仅将初始策略的参数变更另存为不可变策略，并排队生成指定范围历史数据 |
| PATCH | `/api/v1/strategies/{strategy_id}` | 单独修改 `name` 或 `enabled`；一次请求不得混合字段 |
| DELETE | `/api/v1/strategies/{strategy_id}` | 永久删除非初始策略及其计划、当日实盘、回测优化和报告资产；子策略保留并解除父引用 |
| GET | `/api/v1/strategy-config?strategy_id=...` | 读取配置、默认值、字段 Schema 和 revision |
| PUT | `/api/v1/strategy-config` | 仅文件兼容模式可覆盖；目录模式固定返回 `409` |
| GET | `/api/v1/strategies/{strategy_id}/days` | 按交易日倒序查询每日摘要 |
| GET | `/api/v1/strategies/{strategy_id}/days/{trade_date}` | 查询每日计划、执行计划和实际行情 |
| GET | `/api/v1/strategy-jobs/{job_id}` | 查询次日计划与当日实盘历史生成任务 |

创建策略请求：

```json
{
  "name": "一进二 09:45 研究候选",
  "parent_strategy_id": "来源策略 UUID",
  "revision": "来源配置 SHA-256",
  "config": {"entry_cutoff_time": "09:45"},
  "activate": false,
  "history_range": "1y"
}
```

该接口不支持普通新建或复制：来源必须是系统初始策略，且完整参数必须发生变化。
`history_range` 支持 `1d`、`1w`、`1m`、`1y`，默认 `1y`；生成内容固定包含次日计划和
当日实盘历史。服务使用完整 `StrategyConfig` 校验配置，并记录相对父策略的逐项差异。名称可通过
`{"name": "新名称"}` 原地修正；参数和血缘保存后不可修改。参数变化必须调用创建接口并提供
新策略名称。激活新策略会在同一事务停用旧策略。

### 4.5 策略运行

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/strategy-runs/{runId}` | 查询运行状态、输入范围和产物 |

生产环境的 16:30 初版和 23:30 更新由调度系统发起。人工补跑属于受保护的运维接口，
不在公共 Web API 中开放。`GET /api/v1/reports/{tradeDate}` 按复盘交易日返回最后一次
成功生成的版本。

### 4.6 研究

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/research` | 最近 50 个已持久化实验 |
| GET | `/api/v1/research/{run_id}` | 实验完整结果、策略和资产索引 |
| GET | `/api/v1/research/{run_id}/strategy` | 下载独立策略 JSON |
| GET | `/api/v1/research/{run_id}/assets/{filename}` | 流式下载白名单内的实验资产 |

研究接口只读取 `research_*` 表和 MinIO 归档，不会激活策略，也不会生成实时交易事件。

### 4.7 页面和机器文档

| 路径 | 说明 |
| --- | --- |
| `/strategy` | 策略管理 |
| `/` | 次日计划 |
| `/monitor` | 当日实盘 |
| `/research` | 回测优化 |
| `/api/docs` | FastAPI Swagger UI |
| `/api/openapi.json` | 运行时生成的 OpenAPI |
| `/metrics` | Prometheus 指标，不进入公开 OpenAPI |

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
6. `docs/openapi.yaml` 与运行时 `/api/openapi.json` 的路径和参数必须同步。

## 8. 接口验收

- OpenAPI 可以通过规范校验器。
- AsyncAPI 中所有 Topic 与生产代码配置一致。
- 请求和响应具备自动契约测试。
- 时间、金额、股票代码和状态枚举没有歧义。
- API 返回的每个策略状态都可关联到 PostgreSQL 决策事件。
- SSE 断开不会造成权威状态丢失。
- 策略目录模式禁止 `PUT /strategy-config` 原地覆盖配置。
- 策略更新接口仅接受单一 `name` 或单一 `enabled` 字段，禁止将重命名与状态变化合并提交。
- 刷新任务结果记录实际使用的 `strategy_version`、`strategy_revision`、`run_id` 和生成时间。
- 研究资产下载只允许数据库索引中的文件名，拒绝路径穿越。
- 无任何自动下单接口。
