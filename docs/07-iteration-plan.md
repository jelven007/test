# 生产化迭代计划

## 1. 迭代原则

- 先建立持久化和可重放能力，再拆分服务和扩大实时计算。
- 每个阶段保持当前本地功能可用。
- 使用双写、对账和灰度读取迁移，不一次性替换全部链路。
- 每个阶段达到退出条件后才能进入下一阶段。
- 任何阶段都不增加自动下单能力。

## 2. 总体阶段

| 阶段 | 目标 | 主要交付 |
| --- | --- | --- |
| P0 | 冻结文档与当前行为 | 文档基线、回归测试、录制数据 |
| P1 | 建立生产基础设施和领域边界 | 容器、配置、数据模型、存储接口 |
| P2 | 引入 Kafka 和全量持久化 | 采集服务、WAL、Topic、双写和对账 |
| P3 | 持久化策略状态和报告 | PostgreSQL 状态机、Outbox、MinIO 报告 |
| P4 | 引入 Flink 实时指标 | 窗口计算、Checkpoint、板块指标 |
| P5 | 上线 `/api/v1` 与新版看板 | API、Redis 投影、SSE、兼容层 |
| P6 | 高可用、压测和生产发布 | 多副本、告警、备份、恢复和灰度 |

## 3. P0：文档与行为基线

### 工作项

- 评审 `docs/` 下的需求、架构、数据、接口、测试和运维文档。
- 固定当前策略配置和关键边界行为。
- 为典型交易日录制可重复的 mootdx 输入数据。
- 补齐当前单元测试的覆盖率报告。
- 将当前 JSONL 和报告格式纳入迁移样本。

### 退出条件

- 文档中的未决问题关闭。
- 当前测试全部通过。
- 核心策略场景具备固定回放数据。
- OpenAPI 和 AsyncAPI 通过 Schema 校验。

## 4. P1：基础工程和领域拆分

状态：`IN PROGRESS`

已完成：

- 盘中纯规则迁入 `banxia_strategy.domain`，旧导入路径保持兼容。
- 建立版本化事件信封、稳定幂等键和基础存储端口。
- 建立 PostgreSQL、ClickHouse、Redis、MinIO 和 Kafka 本地 Compose。
- 建立 PostgreSQL 和 ClickHouse 初始 Schema。
- 建立 Kafka Topic、DLQ 和 MinIO Bucket 初始化脚本。
- 实现 PostgreSQL、ClickHouse、Redis、MinIO Python 适配器和可选依赖。
- 日报接入本地文件、MinIO、PostgreSQL 双写，盘中接入 JSONL 与异步存储双写。
- 增加内容哈希、Redis TTL、SQL 参数、分钟线去重和部分失败行为单元测试。

待完成：

- 统一配置、结构化日志和 OpenTelemetry。
- 独立服务入口与录制行情回放器。
- 在具备 Docker 的环境执行完整组件测试。

### 工作项

- 将单体逻辑拆分为采集、领域策略、应用服务和存储端口。
- 引入统一配置、结构化日志、OpenTelemetry 和请求 ID。
- 建立本地 Docker Compose 开发环境。
- 创建 PostgreSQL、ClickHouse 和对象存储迁移。
- 定义共享事件 Schema 和代码生成流程。
- 保留当前 CLI 和 Web 作为兼容入口。

### 退出条件

- 领域策略测试不依赖数据库和网络。
- 所有基础设施可以一条命令启动。
- 数据库迁移可从空环境执行。
- 服务可以在无真实 mootdx 的录制数据下运行。

## 5. P2：Kafka 与行情持久化

### 工作项

- 实现独立 `market-collector`。
- 实现分片租约、连接复用、节点健康和本地 WAL。
- 发布盘口和一分钟线 Topic。
- 实现 ClickHouse Sink 和 S3 Parquet 归档。
- 当前 JSONL 与新链路临时双写。
- 建立事件数量、时间范围和哈希对账。

### 退出条件

- Kafka 重复投递不产生业务重复。
- 采集器重启后 WAL 可续传。
- ClickHouse 与 S3 对账通过。
- 连续五个交易日新链路数据完整率达到目标。
- JSONL 仍可作为回退，但不再是目标权威存储。

## 6. P3：策略状态与报告持久化

### 工作项

- 实现 PostgreSQL 策略版本、运行、计划和候选模型。
- 将 `evaluate` 规则迁移为显式状态机。
- 实现 Inbox、Decision、Outbox 原子事务。
- 实现 Outbox Relay。
- 将报告文件写入 MinIO，数据库保存元数据和哈希。
- 导入现有历史报告。

### 退出条件

- 不可逆状态在重启后保持。
- 提交 offset 前故障重放不产生重复决策。
- 同一交易日重复运行只生成一个成功任务。
- 现有报告和新报告的关键字段对账通过。

## 7. P4：Flink 实时计算

### 工作项

- 实现行情清洗、Watermark 和迟到数据处理。
- 实现一分钟、多分钟量能和价格窗口。
- 实现题材上涨比例、涨停家数和板块确认度。
- 发布版本化实时指标事件。
- Checkpoint 写入 MinIO，升级前创建 Savepoint。
- 策略状态机消费指标，但保留数据缺失时的降级路径。

### 退出条件

- 窗口指标与离线基准结果一致。
- 乱序和迟到数据测试通过。
- TaskManager 故障后可以从 Checkpoint 恢复。
- Flink 不可用时原始行情仍持续落库。

## 8. P5：API 与 Web 迁移

### 工作项

- 实现 `docs/openapi.yaml` 中的 `/api/v1`。
- 建立 Redis 最新行情、指标和决策投影。
- 实现缓存回源和投影重建。
- 实现 SSE 监控流。
- Web 看板切换到 `/api/v1`。
- 旧 `/api/*` 改为兼容适配器并输出弃用信息。

### 退出条件

- OpenAPI 契约测试全部通过。
- Redis 清空后 API 可以降级并完成重建。
- SSE 断线重连不造成状态缺失。
- 新旧接口关键字段语义对照通过。
- Web 不再直接依赖进程内监控对象。

## 9. P6：生产就绪

### 工作项

- Kubernetes 多副本和跨可用区部署。
- 配置 HPA、PDB、NetworkPolicy 和资源限制。
- 建立 Prometheus、Loki、Tempo 仪表盘和告警。
- 完成 200 股票实时监控容量压测。
- 执行 Kafka、Flink、PostgreSQL、Redis 和 ClickHouse 故障演练。
- 执行数据库和对象存储恢复演练。
- 灰度切换生产流量并观察完整交易日。

### 退出条件

- 所有 P0/P1 告警都有 Runbook。
- 性能指标满足需求文档。
- 备份恢复达到 RPO/RTO。
- 连续五个交易日无数据一致性差异。
- 生产回滚方案经过演练。

## 10. 建议代码结构

目标仓库可以逐步演进为：

```text
src/
  domain/
    strategy/
    market/
  services/
    collector/
    strategy_engine/
    report_worker/
    api/
    projection_worker/
  adapters/
    mootdx/
    kafka/
    postgres/
    clickhouse/
    redis/
    object_storage/
  contracts/
    events/
    api/
  shared/
    config/
    observability/
    time/
deploy/
  compose/
  kubernetes/
migrations/
  postgres/
  clickhouse/
tests/
  unit/
  contract/
  component/
  integration/
  e2e/
```

在领域代码稳定前，不强制拆分为多个独立仓库。优先使用单仓库中的多个可部署入口，
减少跨仓库版本协调。

## 11. 第一轮系统迭代范围

第一轮只进入 P1，不直接上 Flink：

1. 抽取领域状态机和存储端口。
2. 建立 Docker Compose。
3. 建立 PostgreSQL、ClickHouse、Redis、MinIO 的基础 Schema。
4. 实现四类存储适配器和迁移期双写。
5. 增加 OpenAPI/AsyncAPI 校验。
6. 保持当前 mootdx 采集和 Web 功能可运行。

当前已完成第 1 至 6 项。双写后台队列仍不是持久 WAL，真实组件测试也尚未在本机执行；
这两项分别进入 P2 和具备 Docker 的集成环境完成。

## 12. 变更管理

每个 Pull Request 必须说明：

- 对应需求编号。
- 是否修改 OpenAPI、AsyncAPI 或数据库 Schema。
- 是否影响幂等键、状态机或数据生命周期。
- 新增和更新的测试。
- 部署、迁移和回滚步骤。

策略规则变更必须创建新策略版本，不能直接覆盖历史配置。
