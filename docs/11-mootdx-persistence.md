# mootdx 非实时数据持久化设计

## 1. 目标与边界

本文定义沪深市场中 `mootdx 0.11.x` 可提供的全部非实时数据如何持久化。目标是：

- API 和策略任务不再同步依赖 mootdx 才能读取股票目录、板块和公司资料。
- 每次成功采集都保留来源、采集时间、原始内容哈希和可重放对象。
- 股票名称、板块归属、财务和 F10 内容按观察时点版本化，支持历史研究。
- 大规模时间序列进入 ClickHouse，关系型主数据进入 PostgreSQL，原始内容进入 MinIO。
- Redis 只保存可重建的最新投影，不作为任何非实时数据的权威来源。

本设计覆盖沪深证券目录中的原始记录，并重点规范 A 股和指数。扩展市场 `ExtQuotes`
不在当前范围；`quotes()`、当日 `minute()` 和当日 `transaction()` 属于实时链路，不在本文
重复设计。

## 2. mootdx 数据能力清单

| 数据域 | mootdx 接口或文件 | 主要原始字段 | 权威存储 | 同步方式 |
| --- | --- | --- | --- | --- |
| 沪深证券目录 | `stock_count()`、`stocks()`、`get_security_list()` | `code`、`name`、`volunit`、`decimal_point`、`pre_close`、`market` | PostgreSQL + MinIO | 每交易日完整快照 |
| 交易日历 | 指数日线 `index()` + mootdx 休市日历 | 交易日、是否已有指数行情、推测/确认状态 | PostgreSQL | 每日校验 |
| 通达信板块 | `block.dat`、`block_gn.dat`、`block_fg.dat`、`block_zs.dat` | `blockname`、`code` 及文件原始字段 | PostgreSQL + MinIO | 每交易日完整快照 |
| 通达信行业 | `tdxhy.cfg` | 市场、证券代码、行业代码 | PostgreSQL + MinIO | 每交易日完整快照 |
| 当前财务摘要 | `finance()` | 股本、资产负债、收入、利润、现金流、股东人数、更新日期等 | PostgreSQL + MinIO | 夜间分片扫描 |
| 除权除息与股本事件 | `xdxr()` | 日期、类别、分红、配股、送转、缩股、变动前后股本等 | PostgreSQL + MinIO | 每日增量 |
| F10 栏目目录 | `F10C()` | `name`、`filename`、`start`、`length` | PostgreSQL | 每周全量、按需刷新 |
| F10 栏目正文 | `F10()` | GBK 文本内容 | MinIO + PostgreSQL 元数据 | 内容哈希去重 |
| 股票 K 线 | `bars()` | OHLC、成交量、成交额、时间 | ClickHouse + MinIO | 增量水位 |
| 指数 K 线 | `index()` / `index_bars()` | OHLC、成交量、成交额、时间 | ClickHouse + MinIO | 增量水位 |
| 历史分时 | `minutes()` | 240 个分钟位置的价格、成交量 | ClickHouse + MinIO | 按股票和交易日 |
| 历史分笔 | `transactions()` | 分钟时间、价格、成交量、买卖方向 | ClickHouse + MinIO | 按股票、交易日和分页位置 |
| 历史财务包目录 | `Affair.files()` | 文件名、MD5、文件大小 | PostgreSQL | 每日检查 |
| 历史财务报表 | `Affair.fetch()`、`Affair.parse()` | 报告期、约 264 个财务指标 | ClickHouse + MinIO | 仅下载新增或哈希变化文件 |

以下接口不形成新的持久化副本：

- `k()`、`ohlc()` 是日线查询包装，使用同一份 K 线数据。
- 前复权和后复权行情由未复权 K 线与 `xdxr()` 计算，保存复权因子版本，不重复采集。
- `Reader` 读取本地通达信文件，是另一种导入通道，不是新的数据事实来源。
- `stock_count()` 只用于完整性校验，计数写入采集快照，不单独建立业务表。

## 3. 总体存储归属

### 3.1 PostgreSQL

保存低频、强关系、需要事务和时点版本的数据：

- 证券主数据和名称变更历史。
- 交易日历及确认状态。
- 板块定义、行业定义和成员有效期。
- 当前财务摘要的历次版本。
- 除权除息、股本变化和复权因子来源。
- F10 栏目目录、正文版本元数据和对象地址。
- 数据采集批次、游标、覆盖率、错误和当前已发布快照。

### 3.2 ClickHouse

保存按时间增长、需要区间扫描的数据：

- 股票和指数的全部原生 K 线周期。
- 历史分时。
- 历史分笔。
- 历史财务包解析后的报告期数据。

### 3.3 MinIO

保存不可变原始副本：

- 原始证券目录、板块文件和行业配置。
- F10 原文。
- `finance()`、`xdxr()` 原始响应批次。
- K 线、分时、分笔回填批次的压缩 Parquet 或 NDJSON。
- `gpcw*.zip` 历史财务包及其解析清单。

### 3.4 Redis

只保存以下最新投影，并设置 TTL：

- 当前证券名称和状态。
- 当前板块成员集合。
- 当前财务摘要。
- 热门 F10 栏目短期缓存。

Redis 清空后必须能由 PostgreSQL、ClickHouse 和 MinIO 重建。

## 4. 统一采集批次与来源追踪

### 4.1 `source_snapshot`

每个完整或分片采集动作先创建批次记录：

| 字段 | 说明 |
| --- | --- |
| `snapshot_id UUID` | 批次主键 |
| `dataset TEXT` | `security_catalog`、`block_gn`、`finance_current` 等 |
| `scope_key TEXT` | `all`、`sh`、`600000`、`600000:公司概况` 等 |
| `as_of_date DATE` | 数据业务日期，无明确日期时使用观察日期 |
| `status TEXT` | `running`、`validating`、`published`、`failed` |
| `source_node TEXT` | 实际响应的 mootdx 节点 |
| `source_version TEXT` | mootdx 和 tdxpy 版本 |
| `schema_version INTEGER` | 规范化 Schema 版本 |
| `content_sha256 CHAR(64)` | 规范化原始内容哈希 |
| `raw_object_key TEXT` | MinIO 原始对象地址 |
| `row_count BIGINT` | 解析记录数 |
| `expected_count BIGINT` | 可获得时记录预期数量 |
| `started_at/finished_at TIMESTAMPTZ` | 采集时间 |
| `error_summary JSONB` | 失败分类和样本 |

唯一约束为 `(dataset, scope_key, as_of_date, content_sha256)`。相同内容可以在不同日期重复观察，
但内容寻址的原始对象只保存一份。

### 4.2 `source_sync_state`

按 `(dataset, scope_key)` 保存：

- 最后已发布 `snapshot_id`。
- 时间水位、分页 offset 或最后财务文件名。
- 最后完整成功时间。
- 连续失败次数和下次重试时间。
- 数据新鲜度状态。

大规模全市场任务另建 `source_sync_item`，按股票记录子任务状态和游标。调度可复用现有
`job_execution`，但业务水位不能只放在任务 JSON 中。

### 4.3 发布协议

1. 创建 `running` 批次。
2. 获取数据、计算哈希并写入 MinIO 内容寻址正式对象。
3. 校验数量、字段、日期、重复率和内容哈希。
4. 写 PostgreSQL 暂存表或 ClickHouse 新 revision。
5. PostgreSQL 事务内关闭旧版本、发布新版本并更新水位。
6. 标记批次 `published`。

对象写入和哈希校验必须先于数据库发布，避免当前快照指向不存在的对象。任一步失败都不能切换
“当前快照”指针；查询继续读取上一个已发布版本，并返回过期状态。

## 5. PostgreSQL 数据模型

### 5.1 证券主数据

`security_master`

- `instrument_id TEXT PRIMARY KEY`，格式为 `sh:stock:600000`、`sz:stock:000001`、
  `sh:index:000001`。
- `market SMALLINT`、`exchange TEXT`、`instrument_type TEXT`、`symbol VARCHAR(6)`。
- `board TEXT`、`name TEXT`、`volume_unit INTEGER`、`decimal_point SMALLINT`。
- `listing_status TEXT`、`first_seen_at`、`last_seen_at`、`delisted_at`。
- `current_snapshot_id UUID`、`raw JSONB`。
- 唯一 `(market, instrument_type, symbol)`。

`security_master_version`

- 主键 `(instrument_id, valid_from)`。
- 保存名称、板块类型、交易单位、小数位、昨收参考值和完整 `raw JSONB`。
- `valid_from/valid_to TIMESTAMPTZ` 采用半开区间；内容哈希不变时只更新 `last_seen_at`。

完整目录批次缺少单只股票时不能立即标记退市。只有批次通过完整性校验且连续多个交易日缺失，
才将 `listing_status` 改为 `inactive`。

### 5.2 交易日历

沿用 `trading_session` 主键，并增加：

- `session_status`：`projected` 或 `confirmed`。
- `confirmation_source`：`holiday_calendar`、`index_bar`。
- `first_observed_at`、`confirmed_at`、`snapshot_id`。

历史指数日线存在时才是 `confirmed`。未来工作日只能是 `projected`，不得在历史研究中冒充
已确认交易日。

### 5.3 板块和行业

`market_block`

- `block_id UUID PRIMARY KEY`。
- `block_type`：`default`、`concept`、`style`、`index`、`industry`。
- `source_code`、`block_name`、`active`、`first_seen_at`、`last_seen_at`。
- 唯一 `(block_type, source_code, block_name)`。

`market_block_membership_version`

- 主键 `(block_id, instrument_id, valid_from)`。
- `valid_from/valid_to TIMESTAMPTZ`。
- `snapshot_id`、`source_filename`、`raw JSONB`。

只有完整板块快照发布后才能关闭消失的成员关系。这样历史研究可以按参考日读取当时的题材归属，
消除当前实现中“使用今天板块解释过去行情”的时点偏差。

### 5.4 当前财务摘要

`company_finance_snapshot`

- 主键 `finance_snapshot_id UUID`。
- 唯一 `(instrument_id, source_updated_date, content_sha256)`。
- `observed_at`、`source_updated_date`、`ipo_date`、`snapshot_id`。
- 股本、资产、负债、收入、利润、现金流和股东人数使用 `NUMERIC` 类型。
- `raw JSONB NOT NULL` 保存 mootdx 返回的全部字段。
- `is_current BOOLEAN` 通过部分唯一索引保证每只股票最多一条当前版本。

`finance()` 返回的是通达信当前财务摘要，不等同于完整历史财务报表。即使
`source_updated_date` 相同，只要内容哈希变化也保留修订版本。

### 5.5 除权除息与股本事件

`corporate_action`

- 主键 `corporate_action_id UUID`。
- 唯一 `(instrument_id, action_date, category, content_sha256)`。
- 保存 `fenhong`、`peigujia`、`songzhuangu`、`peigu`、`suogu`、
  `panqianliutong`、`panhouliutong`、`qianzongguben`、`houzongguben`、
  `fenshu`、`xingquanjia`。
- `first_observed_at`、`last_observed_at`、`snapshot_id`、`raw JSONB`。

同一日期和类别可能被修订，不能只按日期覆盖。复权因子由该表按明确算法和版本计算，算法结果
可进入 ClickHouse 派生表，但源事件仍以 PostgreSQL 为准。

### 5.6 F10 公司资料

`company_document`

- 主键 `document_id UUID`。
- 唯一 `(instrument_id, section_name)`。
- 保存当前版本、最后观察时间和可用状态。

`company_document_version`

- 主键 `document_version_id UUID`。
- 唯一 `(document_id, valid_from)`，`content_sha256` 用于复用内容寻址对象。
- 保存 `source_filename`、`source_start`、`source_length`、`encoding`、
  `raw_object_key`、`content_sha256`、`observed_at`、`valid_from/valid_to`。

正文保存到 MinIO，PostgreSQL 只保存索引和短摘要。F10 栏目目录即使正文暂未下载，也要保存
为待采集版本。栏目删除只有在完整 `F10C()` 目录刷新成功后才能生效。

### 5.7 历史财务包清单

`financial_package`

- 主键 `package_id UUID`。
- 唯一 `(filename, source_md5)`。
- 保存 `report_date`、`source_size`、`source_md5`、`content_sha256`、
  `raw_object_key`、`parse_status`、`row_count`、`metric_schema_hash`。

MD5 用于匹配 mootdx 目录，MinIO 对象和内部幂等仍使用 SHA-256。

## 6. ClickHouse 数据模型

### 6.1 `market_kline_v2`

统一保存股票和指数的原生 K 线：

- `instrument_id LowCardinality(String)`。
- `market LowCardinality(String)`、`instrument_type LowCardinality(String)`。
- `symbol LowCardinality(String)`。
- `frequency LowCardinality(String)`：`1m`、`5m`、`15m`、`30m`、`1h`、
  `day`、`week`、`month`、`quarter`、`year`。
- `source_frequency UInt8`，保留 mootdx 原始周期编号，区分语义相同的日线类别。
- `bar_time DateTime64(0, 'Asia/Shanghai')`、`trade_date Date`。
- OHLC、成交量、成交额、`raw_json`、`snapshot_id`、`fetched_at`、`revision`。

排序键：

```text
(instrument_id, frequency, bar_time)
```

使用 `instrument_id` 是必要条件。仅使用六位代码会让上证指数 `sh:index:000001` 与平安银行
`sz:stock:000001` 冲突。现有 `market_history_bar` 可在迁移期继续服务股票历史接口，完成回填
和查询切换后再停止写入。

### 6.2 `market_minute_timeline`

历史 `minutes()` 只有分钟位置、价格和成交量，不是真正的 OHLC K 线：

- 主键语义 `(instrument_id, trade_date, minute_index)`。
- 保存 `minute_time`、`price`、`volume`、`raw_json`、`revision`。
- `minute_index` 为 0 至 239，并映射为上午和下午交易时段。

不能再将每个分时价格伪装成开高低收相同的 K 线。需要 OHLC 时使用原生 `bars(frequency=8)`。

### 6.3 `market_transaction_history`

- `instrument_id`、`trade_date`、`source_position`。
- `trade_time`、`price`、`volume`、`buy_or_sell`。
- `content_hash`、`snapshot_id`、`fetched_at`、`revision`。
- 排序键 `(instrument_id, trade_date, source_position)`。

mootdx 历史分笔只返回分钟级时间，且没有交易所成交序号。`source_position` 是分页位置，不得
对外表述为交易所序列或 Level-2 逐笔。相同日期重新采集时按 revision 保留最新完整分页。

### 6.4 `company_financial_report`

每只股票每个报告期一行：

- `instrument_id`、`report_date`、`source_file`、`source_hash`。
- `metric_schema_hash`、`metrics_json`。
- 常用指标可增加稳定类型列；未投影指标仍完整保存在 `metrics_json`。
- `snapshot_id`、`fetched_at`、`revision`。
- 排序键 `(instrument_id, report_date, source_hash)`。

历史财务包约有 264 个指标，字段会随版本扩展。第一阶段不使用 264 行 EAV 展开，避免把单个
报告期膨胀为数百行；研究需要的指标通过物化视图逐步投影。

## 7. MinIO 对象布局

```text
market-raw/
  security-catalog/as_of_date=YYYY-MM-DD/<sha256>.json.gz
  reference/file=block.dat/as_of_date=YYYY-MM-DD/<sha256>.bin
  reference/file=block_gn.dat/as_of_date=YYYY-MM-DD/<sha256>.bin
  reference/file=block_fg.dat/as_of_date=YYYY-MM-DD/<sha256>.bin
  reference/file=block_zs.dat/as_of_date=YYYY-MM-DD/<sha256>.bin
  reference/file=tdxhy.cfg/as_of_date=YYYY-MM-DD/<sha256>.bin
  company-finance/as_of_date=YYYY-MM-DD/part-*.parquet
  corporate-action/as_of_date=YYYY-MM-DD/part-*.parquet
  f10/symbol=600000/section=<escaped-name>/<sha256>.txt.gz
  kline/instrument_type=stock/frequency=day/year=YYYY/part-*.parquet
  minute-timeline/trade_date=YYYY-MM-DD/part-*.parquet
  transaction/trade_date=YYYY-MM-DD/part-*.parquet
  financial-package/report_date=YYYY-MM-DD/gpcwYYYYMMDD-<sha256>.zip
  manifests/dataset=<dataset>/snapshot_id=<uuid>/manifest.json
```

所有正式对象使用内容寻址，不覆盖旧对象。Manifest 保存 mootdx/tdxpy 版本、节点、请求参数、
行数、字段 Schema、哈希、开始结束时间和关联 `snapshot_id`。

## 8. 增量同步策略

| 数据域 | 建议频率 | 增量或完整性规则 |
| --- | --- | --- |
| 证券目录 | 每交易日收盘后 | 沪深两个市场必须都成功，数量异常时不发布 |
| 交易日历 | 每日及报告前 | 指数日线确认历史，休市表只投影未来 |
| 板块与行业 | 每交易日收盘后 | 五个原始文件独立哈希，整文件成功后切换成员版本 |
| 日 K 线 | 每交易日收盘后 | 从最后完整交易日前回看 5 日，吸收修订 |
| 其他 K 线周期 | 每日或按需 | 从最后 bar 回看两个周期 |
| 当前财务摘要 | 每晚全市场分片 | 内容哈希不变只更新时间，新内容追加版本 |
| 除权除息 | 每晚全市场分片 | 回看全部小体量事件，按内容哈希幂等 |
| F10 目录与正文 | 每周全量、访问时校验 | 目录指纹变化或 TTL 到期后读取正文 |
| 历史财务包 | 每日检查目录 | 只下载新文件或 MD5 变化文件 |
| 历史分时 | 候选优先、后台补全 | 按 `(股票, 交易日)` 保存覆盖状态 |
| 历史分笔 | 明确研究任务触发 | 分页回填，必须记录服务端可用范围和缺口 |

全市场 F10、历史分时和历史分笔不能放进同步 HTTP 请求。它们由限速后台任务执行，按股票分片，
支持断点续传、节点切换和指数退避。mootdx 服务端没有承诺无限历史，无法取得的区间记录为
`source_unavailable`，不能伪造为空数据。

## 9. 查询路径

改造后的读取路径：

- `/api/v1/stock-blocks`：PostgreSQL 当前已发布板块快照。
- `/api/v1/stocks`：PostgreSQL 证券主数据和板块关系，实时价格仍取 Redis。
- `/api/v1/stocks/{symbol}`：PostgreSQL 当前财务摘要、除权除息和 F10 目录。
- `/api/v1/stocks/{symbol}/company`：PostgreSQL 定位版本，MinIO 返回正文。
- `/api/v1/stocks/{symbol}/history`：ClickHouse。
- 研究任务：按参考时点读取证券、板块和财务版本，按区间读取 ClickHouse 行情。

API 不再因 mootdx 暂时不可用而返回整个股票目录或公司资料不可用。响应统一增加：

- `source_as_of`
- `fetched_at`
- `snapshot_id`
- `data_state`
- `stale_after`

只有显式管理接口可以触发后台刷新；普通 GET 不直接访问 mootdx。

## 10. 时点一致性与研究约束

研究输入必须以 `reference_date` 为上界：

- 股票名称和上市状态读取当时有效的 `security_master_version`。
- 题材和行业读取当时有效的板块成员版本。
- 财务摘要只能使用当时已观察到的版本，不能仅按报告期判断可见性。
- F10 正文以 `observed_at` 控制可见性。
- K 线使用未复权源数据；需要复权时记录 corporate action 快照和算法版本。

这会修复当前研究快照已经明确记录的两个缺口：

- `point_in_time_finance = false`
- `point_in_time_classification = false`

## 11. 数据质量和对账

每个发布批次至少执行：

- 证券目录：市场计数、代码唯一性、名称空值、交易单位和小数位范围。
- 板块：未知证券比例、空板块、成员数量突变和重复关系。
- 财务：日期格式、有限数值、股本非负、资产负债基本关系和字段集合变化。
- 除权除息：合法日期、类别范围、重复事件和异常负值。
- F10：目录区间合法、正文长度、编码替换率和内容哈希。
- K 线：`low <= open/close <= high`、时间递增、成交量非负、交易日覆盖率。
- 分时：最多 240 个位置、午休边界和分钟索引唯一。
- 分笔：分页连续、位置唯一、价格和成交量非负。
- 历史财务包：源 MD5、对象 SHA-256、报告期、股票数和指标 Schema 哈希。

每日生成数据覆盖矩阵，至少包含总股票数、成功数、空结果数、失败数、过期数、最早/最晚日期和
上一批次变化率。报告任务只读取 `published` 批次。

## 12. 生命周期

| 数据 | 在线保留 | 对象归档 |
| --- | --- | --- |
| 证券、板块、行业版本 | PostgreSQL 长期保留 | 原始文件长期保留 |
| 当前财务摘要历史 | PostgreSQL 至少 10 年 | 原始批次长期保留 |
| 除权除息与股本事件 | PostgreSQL 长期保留 | 原始批次长期保留 |
| F10 版本元数据 | PostgreSQL 长期保留 | 正文至少 10 年 |
| 日/周/月/季/年 K 线 | ClickHouse 长期保留 | 年度 Parquet 长期保留 |
| 1/5/15/30/60 分钟 K 线 | ClickHouse 5 年 | 至少 10 年 |
| 历史分时 | ClickHouse 5 年 | 至少 10 年 |
| 历史分笔 | ClickHouse 180 天至 1 年 | 按研究与许可要求保留 |
| 历史财务报表 | ClickHouse 长期保留 | 原始 zip 长期保留 |
| 同步日志和错误 | PostgreSQL 2 年 | 月度汇总长期保留 |

删除单个策略不得删除这些共享市场和公司数据。

## 13. 实施顺序

### 阶段一：主数据与读取解耦

- 状态：已于 2026-09-26 实现。
- `011_market_reference.sql` 已新增采集批次、水位、证券主数据和板块版本表。
- `market-reference-sync` 每个工作日 16:20 采集证券目录及五个参考文件，先写
  `market-raw`，再在 PostgreSQL 单事务内发布版本。
- `/api/v1/stocks`、`/api/v1/stock-blocks` 和证券存在性检查已改为 PostgreSQL
  优先读取，响应携带当前快照与新鲜度；未启用持久化仓储的兼容模式保留 mootdx 回退。

### 阶段二：公司资料

- 增加当前财务摘要、除权除息、F10 目录和正文版本。
- 原始响应和正文写 MinIO。
- 股票详情 API 改为持久化读取，并支持后台刷新。

### 阶段三：历史行情模型升级

- 新建 `market_kline_v2`，引入无冲突的 `instrument_id`。
- 覆盖股票和指数全部原生周期。
- 将历史分时从伪 OHLC 中拆出。
- 迁移并核对现有 `market_history_bar`。

### 阶段四：大体量冷数据

- 接入历史财务包目录、原始 zip 和 ClickHouse 报告期数据。
- 增加历史分笔按需回填、覆盖率和 MinIO 归档。
- 完成 Redis 投影重建和跨存储对账。

每个阶段都必须先完成迁移、适配器、单元测试、真实组件集成测试和回滚方案，再切换 API 读取。
