# 数据模型与多模存储设计

## 1. 设计目标

- 每类数据选择与访问模式匹配的存储，不使用一个数据库承载所有职责。
- 所有权威数据可恢复、可追溯、可重放。
- Redis 和进程内存均不是事实来源。
- 高频行情与事务状态隔离，避免相互影响。
- 数据生命周期明确，禁止无限增长的 JSONL 文件。

## 2. 存储职责

| 数据类型 | 主存储 | 辅助存储 | 说明 |
| --- | --- | --- | --- |
| 待发布采集事件 | RocksDB WAL | Kafka | Kafka 确认前的本地持久缓冲 |
| 原始盘口快照 | ClickHouse | Kafka、S3/MinIO | 高频写入、时间范围查询、长期归档 |
| 一分钟线 | ClickHouse | S3/MinIO | 按股票和分钟幂等更新 |
| 证券目录、交易日历和板块关系 | PostgreSQL | S3/MinIO | 当前主数据和时点版本，原始文件内容寻址归档 |
| 公司财务摘要和除权除息 | PostgreSQL | S3/MinIO | 按观察时间版本化，支持历史研究 |
| F10 公司资料 | S3/MinIO | PostgreSQL | 正文不可变保存，数据库保存栏目和版本索引 |
| 多周期历史行情和历史分笔 | ClickHouse | S3/MinIO | 股票、指数和频次使用无冲突 instrument_id |
| 历史财务报表 | ClickHouse | S3/MinIO、PostgreSQL | 报告期查询、原始财务包和文件清单 |
| Flink 衍生指标 | ClickHouse | Kafka、Redis | 历史分析与最新值查询 |
| 策略定义和版本 | PostgreSQL | S3/MinIO | 事务控制和配置快照 |
| 策略目录和血缘 | PostgreSQL | 无 | 可重命名展示信息、不可变参数、父子关系、唯一激活状态和永久删除 |
| 每日计划和候选 | PostgreSQL | S3/MinIO | 权威业务数据 |
| 每策略每日投影 | PostgreSQL | 无 | 次日计划、当日执行计划和实际行情汇总 |
| 当前策略状态 | PostgreSQL | Redis | PostgreSQL 权威，Redis 为查询投影 |
| 决策事件和审计 | PostgreSQL | Kafka、S3/MinIO | 可解释、可重建 |
| 最新行情和实时建议 | Redis | ClickHouse、PostgreSQL | 低延迟读取，可重建 |
| 报告文件 | S3/MinIO | PostgreSQL 元数据 | Markdown、JSON、CSV |
| 研究实验 | PostgreSQL | S3/MinIO | 实验索引、逐日统计、候选配置和完整归档 |
| 应用日志和追踪 | Loki、Tempo | S3/MinIO | 独立生命周期 |
| 运行指标 | Prometheus | 长期指标存储 | 告警和容量分析 |

## 3. Kafka Topic

| Topic | Key | 生产者 | 消费者 | 建议保留 |
| --- | --- | --- | --- | --- |
| `market.quote.snapshot.v1` | `symbol` | 采集器 | Flink、ClickHouse Sink、归档器、策略引擎 | 7 天 |
| `market.bar.1m.v1` | `symbol` | 采集器 | Flink、ClickHouse Sink | 14 天 |
| `market.feature.realtime.v1` | `symbol` | Flink | 策略引擎、ClickHouse Sink、Redis Sink | 7 天 |
| `strategy.plan.created.v1` | `plan_id` | 日报 Worker | 策略引擎、采集清单服务 | 30 天 |
| `strategy.decision.v1` | `plan_id:symbol` | Outbox Relay | Redis Sink、API 推送、审计归档 | 30 天 |
| `strategy.audit.v1` | `plan_id:symbol` | Outbox Relay | 审计归档器 | 90 天 |
| `report.generated.v1` | `report_id` | 报告 Worker | 通知和索引服务 | 30 天 |

每个业务 Topic 都有对应 `<topic>.dlq`。DLQ 消息必须保留原 Topic、分区、offset、错误类型、
失败时间和原始负载。

采集器使用 RocksDB 保存尚未被 Kafka 确认的事件和发布 Checkpoint。RocksDB 放在持久卷，
按 `event_id` 排序；Kafka 返回成功确认后异步清理。WAL 设置容量上限，达到上限时停止采集、
触发告警，不能静默覆盖未发布事件。

## 4. 事件标识与时间

### 4.1 时间字段

所有事件使用 ISO 8601 或 UTC 毫秒时间戳，并同时保存：

- `source_time`：mootdx 返回的行情时间。
- `collected_at`：采集器完成读取的时间。
- `published_at`：Kafka 确认写入的时间。
- `processed_at`：下游完成处理的时间。

展示层统一转换为 `Asia/Shanghai`。不能使用服务接收时间冒充行情源时间。

### 4.2 幂等键

盘口快照没有交易所序列号时，使用规范化字段生成哈希：

```text
event_id = sha256(
  trade_date | symbol | source_time | price | cumulative_volume |
  cumulative_amount | bid1 | bid1_volume | ask1 | ask1_volume
)
```

其他唯一键：

- 分钟线：`trade_date + symbol + bar_time`
- 实时指标：`feature_version + symbol + window_end`
- 策略计划：`trade_date + strategy_version`
- 策略日记录：`strategy_id + trade_date`
- 激活策略：未归档且 `enabled=true` 的部分唯一索引
- 当前状态：`plan_id + symbol`
- 状态迁移：`plan_id + symbol + source_event_id + target_state`
- 报告：`strategy_run_id + format + content_hash`

## 5. PostgreSQL

### 5.1 核心表

| 表 | 主键/唯一键 | 说明 |
| --- | --- | --- |
| `strategy_definition` | `strategy_id`；`code` 唯一；激活状态部分唯一索引 | 可修改名称、不可变当前配置、父策略、差异、激活和永久删除 |
| `strategy_version` | `strategy_version_id` | 不可变规则、配置和代码版本 |
| `strategy_run` | `run_id`；唯一 `(trade_date, strategy_version_id)` | 日报任务状态 |
| `watchlist` | `watchlist_id` | 某计划的动态监控清单 |
| `watchlist_item` | 唯一 `(watchlist_id, symbol)` | 股票来源、顺序和静态门槛 |
| `candidate` | `candidate_id` | 评分、理由、入场和放弃规则 |
| `strategy_plan` | `plan_id` | 参考日、计划交易日和版本 |
| `decision_state` | 唯一 `(plan_id, symbol)` | 当前权威状态 |
| `decision_event` | `decision_event_id` | 追加式状态迁移历史 |
| `report_asset` | 唯一 `(run_id, format, content_hash)` | 对象地址和内容校验 |
| `inbox_event` | `event_id` | 消费幂等登记 |
| `outbox_event` | `outbox_id` | 可靠事件发布 |
| `job_execution` | `job_id` | 调度、重试和执行历史 |
| `audit_log` | `audit_id` | 配置和人工操作审计 |
| `trading_session` | `trade_date` | mootdx 校验后的交易日日历 |
| `strategy_day` | `(strategy_id, trade_date)` | 每策略每日的 `next_plan`、`execution_plan` 和 `actuals` |
| `research_run` | `run_id` | 已完成实验的区间、输入哈希、结果和归档索引 |
| `research_daily` | `(run_id, variant, reference_date)` | 基线/候选逐日准确率与完整结果 |
| `research_strategy` | `strategy_id`；`run_id` 唯一 | 独立研究配置，数据库约束 `active=false` |

`strategy_definition` 的 `current_config`、`parent_strategy_id` 和 `config_changes`
由触发器禁止更新；`name` 作为展示元数据允许单独修正。删除父策略时允许将子策略的
`parent_strategy_id` 清空，除此之外血缘仍不可修改。

策略删除会清理 PostgreSQL 中该策略的 `strategy_day`、版本、运行、计划、候选、监控决策、
报告索引及精确关联的 `research_*` 记录，并依据删除清单移除 MinIO、Redis 和本地报告。
ClickHouse 与 Kafka 中按证券和交易日保存的 mootdx 行情属于共享输入，不随单个策略删除。
激活流程使用事务级 advisory lock，并由部分唯一索引兜底。

`strategy_day` 是面向页面和研究的物化聚合，不替代底层 `strategy_run`、`strategy_plan`、
`candidate` 和 `decision_event`。正常刷新可更新同一日投影；历史回填只填充空字段。

### 5.2 状态机字段

`decision_state` 至少包含：

- `plan_id`
- `symbol`
- `state`
- `reason_code`
- `reason_text`
- `source_event_id`
- `strategy_version_id`
- `first_entered_at`
- `updated_at`
- `version`
- `irreversible`

使用乐观锁 `version` 防止并发覆盖。不可逆状态只允许迁移到更严格的终态，不允许因后续
价格反弹恢复入场资格。

### 5.3 事务边界

一次策略事件处理必须在同一事务内：

1. 插入 `inbox_event`。
2. 读取并锁定 `decision_state`。
3. 验证合法状态迁移。
4. 更新当前状态。
5. 插入 `decision_event`。
6. 插入 `outbox_event`。

任一步失败则全部回滚。

## 6. ClickHouse

### 6.1 `market_quote_snapshot`

建议字段：

- `trade_date Date`
- `symbol LowCardinality(String)`
- `source_time DateTime64(3, 'Asia/Shanghai')`
- `collected_at DateTime64(3, 'Asia/Shanghai')`
- `event_id FixedString(64)`
- `price Decimal64(4)`
- `open/high/low/previous_close Decimal64(4)`
- `cumulative_volume UInt64`
- `cumulative_amount Decimal128(2)`
- `bid1/ask1 Decimal64(4)`
- `bid1_volume/ask1_volume UInt64`
- `source_node LowCardinality(String)`
- `ingest_version UInt64`

建议引擎：

```text
ReplicatedReplacingMergeTree(ingest_version)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (trade_date, symbol, source_time, event_id)
```

### 6.2 `market_bar_1m`

主排序键：

```text
(trade_date, symbol, bar_time)
```

保存 OHLC、成交量、成交额、源时间和修订版本。重复刷新同一分钟时保留最新修订。

### 6.3 `market_feature_realtime`

保存：

- 指标名称和版本。
- 窗口开始、窗口结束和计算时间。
- 量比、价格变化、题材上涨比例、涨停家数等值。
- 输入事件最大时间和数据质量状态。

### 6.4 查询规则

- 最新行情查询优先使用 Redis，不对 ClickHouse 执行高频逐请求 `FINAL`。
- 去重通过物化视图、`argMax` 或异步整理完成。
- 历史查询必须带交易日和股票过滤，禁止无界全表扫描。
- 大批量导出走异步任务和对象存储。

## 7. Redis

建议 Key：

| Key | 类型 | TTL | 内容 |
| --- | --- | --- | --- |
| `quote:latest:{symbol}` | Hash | 2 天 | 最新盘口和源时间 |
| `feature:latest:{symbol}` | Hash | 2 天 | 最新实时指标 |
| `decision:latest:{plan_id}:{symbol}` | Hash | 计划结束后 7 天 | 当前建议和原因 |
| `watchlist:active:{trade_date}` | Set/ZSet | 7 天 | 当日监控清单和顺序 |
| `lease:collector:{shard}` | String | 10 秒 | 采集分片租约 |
| `stream:monitor:{trade_date}` | Stream | 2 天 | SSE 短期推送缓冲 |

规则：

- Key 必须设置 TTL，租约 Key 除外但必须设置短过期。
- Redis 写入来自 Kafka Consumer，不由策略引擎在数据库事务外直接双写。
- Redis 故障不影响 PostgreSQL 状态提交。
- 重建任务从 Kafka、PostgreSQL 和 ClickHouse 恢复投影。

## 8. S3/MinIO

建议 Bucket 和路径：

```text
market-raw/
  quote/trade_date=YYYY-MM-DD/hour=HH/part-*.parquet
  bar_1m/trade_date=YYYY-MM-DD/part-*.parquet
  security-catalog/as_of_date=YYYY-MM-DD/<sha256>.json.gz
  reference/file=<filename>/as_of_date=YYYY-MM-DD/<sha256>.bin
  manifests/dataset=market_reference/snapshot_id=<uuid>/manifest.json

strategy-reports/
  strategy_version=<version>/trade_date=YYYY-MM-DD/sha256=<content-hash>/
    <report-file>

  research/<run-id>/<content-hash-prefix>/
    report.md
    result.json
    optimized-strategy.json
    strategy-metadata.json
    experiment.tar.gz

strategy-audit/
  year=YYYY/month=MM/day=DD/part-*.parquet

flink-state/
  checkpoints/<job-name>/
  savepoints/<job-name>/
```

`manifest.json` 至少包含对象哈希、Schema 版本、生成时间、策略版本、输入时间范围和文件列表。

## 9. 生命周期

| 数据 | 热存 | 温存 | 冷存/删除 |
| --- | --- | --- | --- |
| Kafka 盘口 | 7 天 | 无 | 由 S3 归档承接 |
| ClickHouse 盘口 | 90 天 | 可扩展至 1 年 | S3 保存 3 年后按策略删除 |
| 一分钟线 | 1 年 | ClickHouse 5 年 | S3 可长期保存 |
| 实时指标 | 180 天 | ClickHouse 2 年 | 按研究需要归档 |
| PostgreSQL 策略与状态 | 在线长期保存 | 年度归档 | 审计建议 7 年 |
| Redis | 2 至 7 天 | 无 | 可随时重建 |
| 报告 | 在线索引长期保存 | 对象存储 | 建议 7 年 |
| 应用日志 | Loki 30 天 | S3 180 天 | 到期删除 |
| Trace | Tempo 7 天 | 必要时采样归档 | 到期删除 |

生命周期必须通过自动策略执行，不允许依赖人工删除。

## 10. 数据质量

每个交易日持续检查：

- 股票代码格式和交易所归属。
- `source_time <= collected_at` 的合理偏差。
- 同股票时间倒退、重复率和缺口。
- 昨收与计划参考价差异。
- 一分钟线 OHLC 合法关系。
- 累计成交量和成交额异常回退。
- Kafka 输入数、ClickHouse 落库数和 S3 归档数对账。
- 策略决策引用的事件是否可定位。

## 11. 已实现链路与兼容模式

当前分布式运行链路已经接入 Kafka、SQLite WAL、ClickHouse、PostgreSQL、Redis 和 MinIO。
旧 CLI/本地服务仍保留以下兼容模式：

- 日报先写本地 Markdown、JSON、CSV，再上传 MinIO 内容寻址对象，最后由 PostgreSQL
  事务登记策略版本、运行、计划、候选和已成功上传的对象元数据。
- 盘中快照先追加本地 JSONL，再进入进程内有界队列。后台线程批量写 ClickHouse，
  原子更新 PostgreSQL 决策状态/历史/Outbox，并写 Redis 最新监控投影。
- 同一分钟线按事件 ID 在当前进程内去重；ClickHouse 仍使用 `ReplacingMergeTree`
  承担跨重启幂等。
- `best_effort` 模式允许远端失败时保留本地链路，但错误必须出现在 CLI stderr 或
  `/api/monitor` 的 `storage.last_error`，不能静默成功。

该兼容队列不是持久 WAL。生产服务使用 `market-collector` 的 SQLite WAL、Kafka 确认、
真实 Topic 坐标幂等和独立 `projection-worker`，不能把兼容双写模式视为生产数据完整性边界。

严重异常必须将策略状态降级为“行情异常”，不得仅记录日志后继续判断。

## 12. mootdx 非实时数据

证券目录、板块原始文件、行业配置、F10、财务、除权除息、股票和指数多周期 K 线、
历史分时、历史分笔及历史财务包的表级设计、同步水位和读取改造见
[mootdx 非实时数据持久化设计](11-mootdx-persistence.md)。

## 13. 备份与恢复

- PostgreSQL：持续 WAL 归档，每日全量备份，支持时间点恢复。
- ClickHouse：每日增量备份到对象存储，定期验证恢复。
- S3/MinIO：版本控制、跨区域复制和生命周期保护。
- Kafka：不是长期备份；依赖多副本和对象归档。
- Redis：可启用 AOF，但恢复权威来源仍是 Kafka 和数据库。
- Flink：Checkpoint 用于故障恢复，Savepoint 用于版本升级。

恢复演练必须验证数据数量、内容哈希、状态机终态和报告可访问性。
