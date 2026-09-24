# 部署与运维方案

## 1. 环境划分

| 环境 | 目的 | 数据 |
| --- | --- | --- |
| Local | 开发和单元测试 | Mock、录制行情或独立测试数据 |
| Integration | 组件和契约测试 | 临时 Kafka、数据库和对象存储 |
| Staging | 发布前验证、压测和故障演练 | 脱敏或可重复回放数据 |
| Production | 正式研究监控 | mootdx 实时数据 |

不同环境必须使用独立的 Kafka Topic 前缀、数据库、Redis 命名空间和对象存储 Bucket。

## 2. 生产拓扑

### 2.1 应用服务

- `market-collector`：至少 2 副本，按分片租约选举 Leader。
- `strategy-engine`：至少 2 副本，Kafka Consumer Group 扩展。
- `report-worker`：按任务弹性运行。
- `api-service`：至少 2 副本，通过负载均衡提供服务。
- `outbox-relay`：至少 2 副本，使用数据库锁避免重复发布。
- `projection-worker`：消费决策和行情事件，更新 Redis。

### 2.2 基础设施

- Kafka：3 Broker，跨可用区，副本数 3。
- Flink：高可用 JobManager，按负载配置 TaskManager。
- PostgreSQL：一主两副本或托管 Multi-AZ。
- ClickHouse：首期一分片两副本，三节点 Keeper。
- Redis：主从加 Sentinel 或托管高可用实例。
- S3/MinIO：启用版本控制、服务端加密和生命周期。
- Prometheus、Loki、Tempo：独立于业务服务部署。

数据库、Kafka、Redis 和对象存储只允许私网访问。

## 3. 配置管理

配置分为：

### 3.1 非敏感配置

- 采集周期和行情时效阈值。
- Kafka Topic 名称。
- 监控股票上限。
- 数据生命周期。
- Flink 窗口参数。
- 日报调度时间。

非敏感配置通过版本化配置文件或 ConfigMap 发布。

### 3.2 策略配置

策略配置存入 PostgreSQL `strategy_version`，发布后不可原地修改。修改任何阈值都创建新版本，
并保存：

- 配置 JSON。
- Git Commit。
- 创建人和创建时间。
- 生效交易日。
- 变更说明。

### 3.3 密钥

- 数据库密码、Kafka 凭证、OIDC 密钥和对象存储密钥进入 Vault 或云 KMS。
- 不得写入 Git、镜像、ConfigMap 或日志。
- 密钥必须支持轮换。

### 3.4 P1 适配器开关

兼容入口通过 `BANXIA_STORAGE_MODE` 控制新存储：

| 值 | 行为 |
| --- | --- |
| `off` | 默认值，只运行本地报告和 JSONL 链路 |
| `best_effort` | 启用双写；远端失败可降级，但必须暴露错误 |
| `required` | 初始化、日报双写或队列接收失败时使命令失败 |

连接参数见 `deploy/compose/.env.example`。生产环境不得使用示例凭证。盘中
ClickHouse/PostgreSQL/Redis 写入在有界后台队列执行，`BANXIA_WRITER_QUEUE_SIZE`
和 `BANXIA_WRITER_BATCH_SIZE` 必须结合压测设置。该队列在 P1 不提供进程崩溃恢复，
生产数据完整性仍依赖 P2 的 Kafka 与本地 WAL。

## 4. 容器和发布

### 4.1 镜像

- 使用固定 Python 和基础镜像版本。
- 依赖必须锁定并生成 SBOM。
- 运行时使用非 root 用户。
- 镜像不得包含开发密钥、测试数据和本地报告。
- 镜像以 Git Commit 和语义版本双标签发布。

### 4.2 发布流程

1. 静态检查、单元测试和 Schema 校验。
2. 构建镜像并扫描依赖漏洞。
3. 运行组件测试和数据库迁移检查。
4. 部署 Staging。
5. 执行回放、端到端和性能冒烟测试。
6. 创建 Flink Savepoint。
7. 滚动发布 Production。
8. 验证核心指标、数据延迟和状态一致性。

数据库迁移优先使用“扩展、切换、收缩”方式，禁止在同一次发布中直接删除旧字段。

## 5. 调度

日报任务由 Kubernetes CronJob 在 `Asia/Shanghai` 每个工作日 `16:30` 和 `23:30`
触发。Worker 再通过 mootdx 交易日历确认日期；周末和节假日正常退出且不生成报告。
本地 Compose 使用常驻 `report-scheduler` 执行同样的两个时点。

幂等要求：

- 调度器只负责发起，不依赖其 exactly-once。
- PostgreSQL 唯一键 `(trade_date, strategy_version_id)` 决定是否创建任务。
- 节假日通过 mootdx 交易日历确认，不仅依赖星期判断。
- 失败任务按指数退避重试。
- 超过允许时间仍失败时告警，不生成不完整报告冒充成功。

## 6. 健康检查

### 6.1 Liveness

只检查进程主循环是否正常，避免外部依赖故障导致实例反复重启。

### 6.2 Readiness

按服务职责检查关键依赖：

| 服务 | 就绪依赖 |
| --- | --- |
| 采集器 | Kafka 可写、本地 WAL 可写、租约可续期 |
| 策略引擎 | Kafka 可读、PostgreSQL 可写 |
| API | PostgreSQL 可读；Redis 可降级；ClickHouse 按接口需要检查 |
| 报告 Worker | PostgreSQL、ClickHouse、对象存储 |
| Flink | Kafka、Checkpoint 存储 |

## 7. 监控指标

### 7.1 行情采集

- `market_collect_duration_seconds`
- `market_collect_success_total`
- `market_collect_failure_total`
- `market_quote_age_seconds`
- `market_source_switch_total`
- `market_wal_pending_events`

### 7.2 Kafka

- Producer 错误率。
- Consumer Lag。
- ISR 数量。
- Under-replicated Partition。
- DLQ 写入速率。

### 7.3 Flink

- Checkpoint 成功率和耗时。
- Restart 次数。
- Watermark 延迟。
- Backpressure。
- 迟到事件数。

### 7.4 策略

- 每分钟处理事件数。
- 重复 Inbox 命中数。
- 状态迁移数。
- 不可逆放弃数。
- Outbox 未发布数量和最大等待时间。

### 7.5 API

- 请求量、状态码和 P50/P95/P99 延迟。
- Redis 命中率。
- PostgreSQL 和 ClickHouse 查询延迟。
- SSE 连接数和重连数。

## 8. 告警

| 级别 | 条件示例 |
| --- | --- |
| P0 | 交易时段所有 mootdx 节点不可用超过 30 秒 |
| P0 | PostgreSQL 不可写或策略状态无法提交 |
| P0 | 交易时段行情 P95 延迟超过 10 秒持续 2 分钟 |
| P1 | Kafka Consumer Lag 超过 30 秒 |
| P1 | Flink 连续 3 次 Checkpoint 失败 |
| P1 | Outbox 最老未发布事件超过 60 秒 |
| P1 | ClickHouse 或 S3 对账差异超过阈值 |
| P2 | Redis 命中率持续低于 80% |
| P2 | 单实例 CPU 或内存持续超过 80% |

所有告警必须包含环境、服务、分片、开始时间、最近错误和 Runbook 链接。

## 9. 备份

### 9.1 PostgreSQL

- 每日全量备份。
- 持续 WAL 归档。
- 生产备份保留至少 35 天。
- 每月执行隔离恢复验证。

### 9.2 ClickHouse

- 每日增量备份到对象存储。
- 每周验证随机交易日分区恢复。
- 恢复后执行记录数、最大时间和内容哈希对账。

### 9.3 S3/MinIO

- 启用对象版本控制。
- 报告和审计 Bucket 启用对象锁或等价防篡改能力。
- 跨区域复制。
- 删除由生命周期策略执行。

### 9.4 Flink

- Checkpoint 用于自动故障恢复。
- 版本升级前创建 Savepoint。
- Savepoint 与应用版本建立映射。

## 10. 容灾目标

| 场景 | RPO | RTO |
| --- | --- | --- |
| 单应用 Pod 故障 | 0 | 小于 60 秒 |
| 单 Kafka Broker 故障 | 0 | 自动恢复 |
| PostgreSQL 主节点故障 | 0 或同步复制窗口内 | 小于 5 分钟 |
| 单可用区故障 | 0 | 小于 10 分钟 |
| 区域故障 | 不超过 5 分钟 | 不超过 30 分钟 |

mootdx 在 Kafka 确认前产生但尚未被系统接收的行情无法保证零丢失。系统应记录缺口，
不得伪造缺失快照。

## 11. 安全

- OIDC 用户认证和基于角色的授权。
- 管理、重放和配置操作使用独立权限。
- 服务间 mTLS。
- Kubernetes NetworkPolicy 限制横向访问。
- 数据库账号按服务最小权限拆分。
- 所有审计日志包含操作者、请求 ID、变更前后值和时间。
- 日志禁止记录 Token、密码和完整凭证。
- 对象下载使用短时签名 URL。
- Web API 设置速率限制和请求体大小限制。

## 12. Runbook

### 12.1 mootdx 节点全部失败

1. 确认不是本地网络或 DNS 故障。
2. 查看节点健康分数和最近异常。
3. 保持最后行情只读展示，策略状态标记 `data_stale`。
4. 恢复后记录数据缺口，不补造不存在的 1 秒快照。
5. 对缺口期间所有决策执行审计。

### 12.2 Kafka 积压

1. 确认 Broker、网络和 Consumer Group 状态。
2. 检查 ClickHouse、PostgreSQL 或 Flink 是否背压。
3. 必要时扩展消费者，但保持同股票有序。
4. 恢复后对比事件时间和处理时间。

### 12.3 Redis 丢失

1. API 自动回源并标记缓存降级。
2. 暂停非必要批量查询。
3. 从 PostgreSQL 恢复策略状态投影。
4. 从 ClickHouse 恢复最新行情和指标。
5. 对比重建结果后恢复正常模式。

### 12.4 PostgreSQL 故障

1. 策略引擎停止提交新状态。
2. 保持行情进入 Kafka、ClickHouse 和 S3。
3. 执行主备切换或时间点恢复。
4. 从 Kafka 重放未确认策略事件。
5. 校验 Inbox、状态、决策和 Outbox 一致性。

## 13. 上线检查

- 所有镜像和 Schema 版本固定。
- 数据库迁移已在 Staging 演练。
- Flink Savepoint 可用。
- 关键告警和通知渠道生效。
- 备份处于成功状态。
- 看板显示正确交易日、计划版本和数据时效。
- 当前生产计划不包含自动下单能力。
