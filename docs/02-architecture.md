# 系统架构设计

## 1. 架构原则

1. `mootdx` 是唯一行情源，但其多个通达信节点可作为连接故障切换目标。
2. Kafka 是所有实时事件的唯一入口，服务之间不通过共享内存传递权威数据。
3. Flink 只计算事实和衍生指标，最终策略状态由独立策略引擎维护。
4. PostgreSQL、ClickHouse 和对象存储是权威持久层，Redis 仅提供可重建投影。
5. 投递语义为“至少一次 + 幂等写入”，不承诺不可验证的端到端 exactly-once。
6. 数据异常时优先停止判断，不使用旧行情继续生成入场确认。
7. 系统不包含自动下单、券商账户和持仓资金操作。

## 2. CURRENT 架构

当前版本为单进程 Python 本地应用：

```mermaid
flowchart LR
    M[mootdx] --> P[IntradayMonitor]
    P --> MEM[进程内快照和事件]
    P --> JSONL[JSONL 日志]
    MEM --> HTTP[ThreadingHTTPServer]
    REPORT[本地报告文件] --> HTTP
    HTTP --> WEB[Web 看板]
```

当前能力：

- mootdx 长连接、批量盘口、60 秒分钟线和节点切换。
- 交易时段 1 秒轮询、非交易时段 60 秒轮询。
- 日报、盘中判断、历史报告和本地 Web 看板。
- 单元测试覆盖主要策略分支。

当前限制：

- 最新快照、最近事件和不可逆策略状态主要存在内存中。
- JSONL 每轮追加且缺少分区、压缩、索引和生命周期管理。
- HTTP、采集、策略计算和报告读取处于同一故障域。
- 无消息重放、分布式幂等、服务级监控和生产容灾。

## 3. TARGET 总体架构

```mermaid
flowchart TB
    subgraph SOURCE[行情接入]
        M[mootdx 节点池]
        C[采集服务集群]
        WAL[本地 WAL]
        M --> C
        C --> WAL
    end

    subgraph EVENT[事件平面]
        K[Kafka]
        DLQ[Dead Letter Topics]
        K --> DLQ
    end

    subgraph COMPUTE[计算与决策]
        F[Flink 实时计算]
        S[策略状态机]
        RPT[日报与报告 Worker]
    end

    subgraph DATA[多模持久化]
        CH[(ClickHouse)]
        PG[(PostgreSQL)]
        RD[(Redis)]
        OBJ[(S3 / MinIO)]
    end

    subgraph SERVING[查询与展示]
        API[API 服务]
        WEB[Web 看板]
    end

    C --> K
    K --> F
    K --> S
    K --> OBJ
    F --> K
    F --> CH
    F --> RD
    S --> PG
    S --> K
    S --> RD
    RPT --> PG
    RPT --> CH
    RPT --> OBJ
    CH --> API
    PG --> API
    RD --> API
    OBJ --> API
    API --> WEB
```

横切能力：

- Prometheus：指标与告警。
- Loki：结构化日志。
- Tempo：分布式追踪。
- OpenTelemetry：统一埋点。
- Kubernetes：编排、服务发现、滚动发布和租约。

## 4. 组件职责

### 4.1 采集服务

- 根据动态监控清单对股票分片。
- 每个分片由一个 Leader 采集，备用实例通过租约接管。
- 复用 mootdx 连接并维护节点健康分数。
- 盘口目标周期 1 秒，分钟线目标周期 60 秒。
- 事件发布前写本地 RocksDB WAL；Kafka 确认后推进 WAL Checkpoint。
- 生成稳定 `event_id`，记录 `source_time`、`collected_at` 和 `published_at`。

采集器不执行策略判断，避免行情接入和业务规则耦合。

### 4.2 Kafka

- 原始行情、衍生指标、策略决策和审计事件的统一总线。
- 按 `symbol` 作为分区键，保证单只股票事件有序。
- 生产者启用幂等和 `acks=all`。
- 消费者写入权威存储后提交 offset。
- Schema 不兼容或无法解析的数据进入对应 DLQ。

Topic 设计见 [数据与存储](03-data-storage.md)，消息契约见
[AsyncAPI](asyncapi.yaml)。

### 4.3 Flink

负责：

- 一分钟和多分钟滚动窗口。
- 分钟量比、价格区间、数据延迟和异常检测。
- 题材上涨家数比例、涨停家数和板块强度。
- Checkpoint 和从 Kafka 重放恢复。

不负责：

- 最终“入场/放弃”业务状态。
- 策略版本和人工配置管理。
- 报告文件生成。

Flink 输出进入新的 Kafka Topic，再由策略引擎和存储 Sink 消费。

### 4.4 策略状态机

- 同时消费原始行情、Flink 衍生指标和当日策略计划。
- 按 `plan_id + symbol` 维护状态。
- 将输入事件登记到 `inbox_event`，防止重复消费。
- 在一个 PostgreSQL 事务内更新当前状态、追加决策事件并写入 Outbox。
- Outbox Relay 将决策可靠发布到 Kafka。
- Redis Consumer 根据决策事件更新实时查询投影。

状态机必须保存不可逆规则，例如跌破昨收、开盘超限和 10:00 窗口关闭。

### 4.5 日报与报告 Worker

- Kubernetes CronJob 在交易日 16:00 生成初版，并于 23:30 发起覆盖更新。
- Worker 使用 mootdx 交易日历校验日期，非交易日正常跳过。
- PostgreSQL 唯一键 `(trade_date, strategy_version)` 防止重复运行。
- 从 ClickHouse 读取历史行情和统计结果。
- 将运行状态、候选和报告元数据写入 PostgreSQL。
- 将 Markdown、JSON 和 CSV 文件写入 S3/MinIO。

### 4.6 API 与 Web

- 对外统一使用 `/api/v1`。
- 最新快照优先读取 Redis；缓存缺失时回源 PostgreSQL 或 ClickHouse。
- 历史行情和指标读取 ClickHouse。
- 策略计划、状态和审计读取 PostgreSQL。
- 报告内容通过对象存储签名地址或 API 代理访问。
- SSE 仅用于降低页面轮询开销，断线后必须能通过普通查询恢复。

## 5. 数据流

### 5.1 盘口快照

1. 采集器从 mootdx 获取候选股票盘口快照。
2. 规范化字段并生成 `event_id`。
3. 写本地 WAL，再发布 `market.quote.snapshot.v1`。
4. ClickHouse Sink 保存历史快照。
5. Flink 计算窗口指标并发布 `market.feature.realtime.v1`。
6. 策略状态机生成状态迁移，写 PostgreSQL 和 Outbox。
7. Redis 投影更新，API 将结果推送到 Web。
8. 原始事件按批次归档为 Parquet 到 S3/MinIO。

### 5.2 一分钟线

一分钟线每 60 秒从 mootdx 刷新，不等于每 60 秒才采集一次盘口。分钟线按
`symbol + minute` 幂等更新，用于趋势、量能和行情时效校验。

### 5.3 每日策略

1. 16:00 调度任务创建或更新 `strategy_run`，23:30 以最新行情更新同一交易日报告。
2. Worker 获取交易日历、涨停池、炸板池、板块数据和历史行情。
3. 执行静态过滤、评分、行业限额和组合约束。
4. 保存候选和计划，发布 `strategy.plan.created.v1`。
5. 生成报告并写对象存储。
6. 次交易日采集器根据计划更新动态监控清单。

## 6. 一致性与可靠性

### 6.1 幂等

- 原始快照：稳定 `event_id`。
- 分钟线：`symbol + bar_time`。
- 策略状态：`plan_id + symbol`。
- 状态迁移：`decision_id`，由输入事件和目标状态确定。
- 日报运行：`trade_date + strategy_version`。

### 6.2 Inbox/Outbox

策略引擎不能在“数据库提交”和“Kafka offset 提交”之间依赖时间顺序碰运气：

1. 在 PostgreSQL 事务内登记 `inbox_event`。
2. 更新 `decision_state`。
3. 追加 `decision_event`。
4. 写入 `outbox_event`。
5. 事务提交后再确认消费进度。
6. Outbox Relay 独立发布事件并记录确认时间。

重复消息会命中唯一键并幂等退出。

### 6.3 降级策略

| 故障 | 行为 |
| --- | --- |
| mootdx 单节点失败 | 切换备用节点，记录数据间隙 |
| mootdx 全部节点失败 | 保留最后报价，标记失效，暂停入场判断 |
| Kafka 不可用 | 采集器写 WAL，达到容量阈值后停止接收并告警 |
| Flink 不可用 | 原始行情继续落盘，依赖衍生指标的判断暂停 |
| PostgreSQL 不可用 | 不生成新策略决策，禁止只写 Redis |
| Redis 不可用 | API 回源权威存储，允许延迟升高 |
| ClickHouse 不可用 | 实时状态继续，历史查询和报告任务降级 |
| 对象存储不可用 | 报告任务重试，不将运行标记为完整成功 |

## 7. 部署拓扑

生产环境建议：

- Kubernetes 跨三个可用区。
- Kafka 3 Broker，副本数 3，`min.insync.replicas=2`。
- Flink 高可用 JobManager，Checkpoint 写 S3/MinIO。
- PostgreSQL 一主两副本或托管 Multi-AZ，启用 WAL 归档。
- ClickHouse 首期一分片两副本，三节点 Keeper。
- Redis 主从加 Sentinel，或使用托管高可用 Redis。
- API、采集器、策略引擎和 Worker 至少两个实例。

实时采集使用分片 Leader，不让多个副本重复请求同一批股票。Kafka、Flink 和数据库仍需
处理切换过程中可能出现的重复事件。

## 8. 性能与容量边界

近期生产范围：

- 日线分析覆盖沪深主板。
- 实时监控覆盖日报候选和人工补充清单，首期上限 200 只。
- 盘口快照目标 1 秒一次。
- 分钟线目标 60 秒刷新一次。

扩大到全市场 1 秒采样前，必须验证 mootdx 节点承载、网络带宽、Kafka 分区数、
ClickHouse 写入量和 Flink 状态大小，不得直接沿用候选池容量结论。

## 9. 技术选型结论

| 领域 | 选型 | 结论 |
| --- | --- | --- |
| 应用服务 | Python 3.12、FastAPI、Pydantic | 延续现有策略代码，API 使用 ASGI 多副本部署 |
| 数据访问 | SQLAlchemy、Alembic | PostgreSQL 事务和版本化迁移 |
| Kafka 客户端 | confluent-kafka-python | 使用 librdkafka 的幂等生产和消费能力 |
| 消息总线 | Kafka | 不同时维护 Pulsar |
| 流计算 | Flink SQL、PyFlink | 用于窗口和板块指标，不用于权威业务状态 |
| 采集 WAL | RocksDB | 保存尚未被 Kafka 确认的采集事件 |
| 事务数据 | PostgreSQL | 策略、计划、状态、审计和任务控制面 |
| 高频历史 | ClickHouse | 行情和指标的列式存储与聚合 |
| 实时缓存 | Redis | 最新快照和查询投影，可重建 |
| 对象存储 | S3/MinIO | 原始归档、报告和 Flink Checkpoint |
| 服务编排 | Kubernetes | 多副本、租约、调度和滚动发布 |
| 可观测性 | OpenTelemetry 栈 | Prometheus、Loki、Tempo |

## 10. 架构验收

- 任一服务重启后不可逆策略状态不丢失。
- Kafka 重放不产生重复状态迁移。
- Redis 清空后可以自动重建。
- Flink 故障不会阻止原始行情持久化。
- API 不直接依赖采集器进程内存。
- 报告可以由数据库元数据和对象存储重新定位。
- 数据过期或依赖异常时系统停止给出入场确认。
