# 多策略与交易日记录

`/strategy` 默认展示策略列表，横向对比最低评分、成交额、换手率、流通市值、题材涨停
门槛和入场截止时间。点击策略名称进入 `?strategy_id=...` 详情，查看交易日记录和参数。
仅修改名称可保存到原策略；修改任一参数后只能“保存为新策略”，系统要求输入新名称，
并记录 `parent_strategy_id` 与逐项 `config_changes`。原策略及历史计划不会被覆盖。
新策略默认处于“未激活”，也可在保存时立即激活。

策略只有“激活”和“未激活”两种可见状态，全库最多一条激活策略。激活新策略会在
同一事务内取消原激活策略。删除会永久移除目标策略及其历史交易日、计划、候选、盘中
决策、回测优化和报告资产；子策略保留，但解除对已删除父策略的引用。
数据库触发器禁止修改已保存策略的完整配置、父策略和差异，名称作为展示元数据允许修正；部分唯一索引
`strategy_definition_single_active` 保证未归档策略最多一条 `enabled=true`。

当前配置文件 `config/strategy.json` 仍用于兼容文件模式。完整策略目录模式以 PostgreSQL
为权威，`PUT /api/v1/strategy-config` 返回 `409 IMMUTABLE_STRATEGY`，页面通过
`POST /api/v1/strategies` 创建新版本。

## 每个交易日一条

`strategy_day` 主键为 `(strategy_id, trade_date)`，一条记录含：

- `next_plan`：该交易日收盘后生成、下个交易日执行的计划。
- `execution_plan`：此前生成、该交易日执行的计划。
- `actuals`：该日实盘行情与策略判断；收盘后附行情验证结果、覆盖率和收盘封板命中率。

空候选、尚无前日计划、未开盘及行情缺失分别显示；无样本准确率为 `null`。
实盘数据不是券商持仓或成交，不包含自动下单。
重复刷新 UPSERT 同一日记录；底层 `strategy_version/run/plan` 继续保存不同参数版本审计记录。
历史参数刷新完成后，也重新验证该计划的已结束执行日。
历史回填使用只补空字段的写法，因此不会覆盖已存在的 `next_plan`、`execution_plan`
或 `actuals`。

## 隔离与调度

仅当前激活策略按其保存的时间表运行，执行开始时才解析当前激活策略。页面刷新任务
不接受固定策略执行语义，即使请求带有历史 `strategy_id`，Worker 开始执行时仍读取
当时激活策略。每天从 mootdx 更新
交易日历，交易日预建每日记录，遗漏的收盘任务自动补跑。
采集端动态合并各策略观察股票、按代码去重，复用长连接，按最快配置采集。
策略引擎为每份有效计划维护独立处理器，拒绝非计划执行日的行情。
输入去重按 `plan_id + source_event_id` 与计划范围内的 Kafka offset 进行，保证
同一行情可以驱动多份计划，每份只落一次。

行情写入当天记录时同时校验策略、计划 ID 和交易日；旧计划的延迟消息不能覆盖新计划记录。
历史页面优先读取持久化收盘结果，异日 Redis 报价不会混入历史日。

## API

- `GET/POST /api/v1/strategies`
- `PATCH /api/v1/strategies/{strategy_id}`：单独修改名称或切换激活状态
- `DELETE /api/v1/strategies/{strategy_id}`：永久删除策略及关联业务数据
- `GET /api/v1/strategy-config?strategy_id=...`
- `GET /api/v1/strategies/{strategy_id}/days?limit=30&before=YYYY-MM-DD`
- `GET /api/v1/strategies/{strategy_id}/days/{trade_date}`
- reports、refresh、assets、monitor、monitor/events、monitor/stream 均支持 `strategy_id`。
- monitor 相关接口另支持 `trade_date`，报告日期表示生成日。

无 `strategy_id` 的报告和监控接口默认指向当前激活策略。刷新接口始终在执行时使用激活策略。
文件模式旧 Web 服务保留原参数编辑，
完整多策略功能通过 FastAPI 服务提供。

列表响应通过 `key_parameters` 返回横向对比所需的九项参数。`PATCH` 请求必须严格只包含
`name` 或 `enabled` 之一，不能在同一请求中混合修改。

创建子策略请求必须提供来源 `parent_strategy_id` 和来源 revision。revision 不匹配返回
`409 CONFIG_CONFLICT`，字段或参数无效返回 `422 VALIDATION_ERROR`。策略名称限制为
1 至 80 个字符。

## 迁移与验证

依次应用 `migrations/postgres/005_multi_strategy.sql`、
`migrations/postgres/006_immutable_active_strategy.sql`、
`migrations/postgres/007_mutable_strategy_name.sql` 和
`migrations/postgres/008_strategy_cascade_delete.sql`，再使用已配置数据库环境执行：

```sh
PYTHONPATH=src python -m banxia_strategy.catalog_bootstrap
```

导入默认策略已有报告及最近一次研究实验的两组历史记录，优化策略默认暂停。
迁移重复执行不会覆盖较新的日报；旧报告错误的下一交易日按已有 mootdx 日历纠正。
实验原始数据保持不变，日表仅作为管理和展示记录。

按当前目录中每条未归档策略的配置补齐历史日记录：

```sh
PYTHONPATH=src python -m banxia_strategy.catalog_backfill \
  --start 2025-09-25 --end 2026-09-24
```

命令只使用 mootdx，额外采集开始日前两周以衔接首日的执行计划；已存在的
`next_plan`、`execution_plan` 和 `actuals` 不会被覆盖。再次执行只校验并跳过已有数据。
可用 `--snapshot` 复用已保存的快照，避免重复下载。

2025-09-25 至 2026-09-24 已完成 242 个交易日的两策略历史回填。盈利约束优化另创建
未激活子策略 `9500ff4b-529c-4b08-ac24-0f0734fd7d60`，只把入场截止时间改为 `09:45`；
它不会自动替换激活策略。

随后更新 api、report-scheduler、report-worker、market-collector、strategy-engine 镜像。

```sh
PYTHONPATH=src:tests python -m unittest discover -s tests
```

设置 `BANXIA_TEST_POSTGRES_DSN` 后会额外运行真实 PostgreSQL 多策略隔离检查；
测试数据在事务中回滚，不保留测试策略。
