# 一进二策略研究与当日实盘系统

这是一个面向研究用途的沪深主板短线筛选器。它在每个交易日收盘后读取公开行情，
从首板股票中筛选可能进入二板的观察候选，并为每只股票生成条件化的次日计划。

工具不会登录券商、不会连接交易账户，也不会自动下单。

## 项目状态

当前仓库已实现可运行的多服务研究系统：

```text
mootdx -> SQLite WAL -> Kafka -> Flink / 策略状态机
       -> PostgreSQL + ClickHouse + Redis + S3/MinIO
       -> /api/v1 -> Web 看板
```

本地 Compose 已包含完整链路，Kubernetes 清单提供生产部署基线；目标集群的容量、故障和
备份恢复演练仍未完成。当前行为以本 README 的运行说明为准，完整文档以
[docs/README.md](docs/README.md) 为统一入口：

- [需求规格](docs/01-requirements.md)
- [系统架构](docs/02-architecture.md)
- [数据与存储](docs/03-data-storage.md)
- [接口规范](docs/04-api.md)
- [测试策略](docs/05-testing.md)
- [部署与运维](docs/06-deployment-operations.md)
- [迭代计划](docs/07-iteration-plan.md)
- [策略研究](docs/08-strategy-research.md)
- [多策略管理](docs/09-multi-strategy.md)
- [Web UI 标准](docs/10-web-ui-standard.md)
- [mootdx 非实时数据持久化](docs/11-mootdx-persistence.md)
- [OpenAPI](docs/openapi.yaml) / [AsyncAPI](docs/asyncapi.yaml)

## 本地基础设施

生产化迭代使用 Docker Compose 提供 PostgreSQL、ClickHouse、Redis、MinIO 和 Kafka：

```bash
cp deploy/compose/.env.example deploy/compose/.env
make infra-config
make infra-up
make infra-status
```

初始化 Schema 位于 `migrations/postgres/` 和 `migrations/clickhouse/`。详细说明见
[deploy/compose/README.md](deploy/compose/README.md)。

当前 Compose 已接入 Kafka、SQLite WAL、Flink、PostgreSQL、ClickHouse、Redis 和 MinIO，
同时保留本地报告和 JSONL 作为兼容链路。

## 策略范围

策略参考公开资料中对“半夏1992”风格的描述，但不是对个人交易系统的精确复制：

- 首板晋级二板为主要候选池。
- 优先主线题材、板块补涨和新题材切换。
- 综合首次封板时间、回封稳定性、封单强度、换手、成交额和流通市值。
- 使用涨停家数、炸板率、连板高度判断接力环境。
- 分仓、限制同题材数量；没有候选时明确输出空仓观察。

默认仅覆盖沪深主板 10cm 股票，排除 ST、退市整理、创业板、科创板和北交所。

## 安装

需要 Python 3.9 或更高版本：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .
```

启用生产存储适配器时安装可选依赖：

```bash
.venv/bin/pip install -e '.[storage]'
```

检查行情连接：

```bash
.venv/bin/banxia-strategy doctor
```

## 每日运行

```bash
.venv/bin/banxia-strategy run
```

指定研究日期：

```bash
.venv/bin/banxia-strategy run --date 2026-09-23
```

结果保存在 `reports/YYYY-MM-DD/`：

- `report.md`：适合人工阅读的市场状态、候选和次日条件。
- `candidates.csv`：便于在表格中筛选。
- `candidates.json`：便于接入后续程序。

报告中的股票是条件观察候选，不是开盘即买清单。只有入场触发条件成立时才进入下一步
人工判断；放弃条件优先于入场条件。

## Web 看板

启动本地看板：

```bash
.venv/bin/banxia-strategy serve
```

浏览器打开 <http://127.0.0.1:8765>。FastAPI 提供四个统一页面：

- `/strategy`：策略列表、关键参数对比、详情、重命名、参数版本化、激活切换、血缘和逐日记录。
- `/`：次日计划、策略选择、历史日期和完整报告刷新。
- `/monitor`：盘中行情、策略状态、分时和决策事件。
- `/research`：历史准确率、参数实验和研究资产。

共享样式由 `src/banxia_strategy/web/ui-standard.css` 管理，桌面端和 `390x844` 移动端
均要求无页面级横向溢出。次日计划页默认读取持久化策略记录，也兼容
`scheduled_reports` 和 `reports` 本地文件。每只候选会依次列出：

- 集合竞价参考价格区间。
- 09:30～10:00 的确认入场条件。
- 盘中逐项放弃条件。
- 单票仓位、硬止损和次日退出规则。
- 封板质量、成交额、换手率及板块强度依据。

竞价参考价格由昨收价和策略涨幅条件估算，实际交易价格、涨停价和可成交状态必须以券商
行情为准。自定义端口或报告目录：

```bash
.venv/bin/banxia-strategy serve --port 9000 \
  --reports-dir scheduled_reports --reports-dir reports
```

## 当日实盘

同一个 Web 服务提供 <http://127.0.0.1:8765/monitor>，保留原日报的安洁科技（002635）、
依顿电子（603328）、威星智能（002849），并从 `config/monitor_watchlist.json` 读取补充股票：
集泰股份（002909）、康强电子（002119）、东方中科（002819）、勤上股份（002638）、
天奥电子（002935），目前共8只。补充股票按最近一次盘中重筛顺序优先展示，原日报股票
保留在后面追踪。首页的“当日实盘”链接可直接进入。

```bash
.venv/bin/banxia-strategy serve --watch-date 2026-09-23
```

- 后台启动时立即采集。集合竞价、等待开盘和连续竞价阶段，每1秒通过 mootdx 批量获取
  全部监控股票的盘口；一分钟线按 mootdx 支持的最小K线粒度每60秒刷新。午休、收盘后
  及其他非交易时段自动降为每60秒采集。行情连接会复用，失败时自动切换备用服务器。
  多个页面共享采集结果，关闭页面后，只要 Web 服务仍运行就会继续采集。
- 页面每1秒同步后台缓存，显示下次采集倒计时、盘口时间、行情、建议依据、分时走势和
  判断变化记录。“同步页面”只重新读取缓存，不额外请求行情。
- 按原报告的开盘涨幅区间、放弃阈值、跌破昨收和10:00截止条件判断。
  跌破昨收采用当日最低价保守过滤；反弹不会清除这一条件。
- 参考二板价按昨收的110%四舍五入到分；竞价价位仅为估算，边界判断使用实际涨幅。
  分钟成交量倍数是最新有效分钟与之前5个有效分钟的均量之比，仅展示，不作为单独买入信号。
- 封板仅表示当前1秒盘口快照：仍需人工核验板块助攻、量能、首次快速回封和实际成交机会。
  采集间隔内的瞬时炸板仍可能遗漏，页面不生成无条件买入指令。
- 交易中核验盘口时间与当日分钟线，超过180秒、缺失当日分钟线、昨收不一致或请求失败时
  暂停判断。交易阶段后台超过6秒未更新、非交易时段超过90秒未更新，或页面与服务断连时，
  同样停用旧建议。
- 默认选取包含原三只股票的最新日报，补充股票无需出现在原日报中，原始报告不变。
  补充清单独立保存数据来源、核验时间、昨收日期、计划日期和静态门槛结果，不生成原日报评分；
  页面分别标注“日报入选”和“盘中补充”。早盘重筛只更新观察优先级，竞价超限、
  跌破昨收或超过10:00的股票仍会显示“放弃／不追”，不会因盘中走强恢复入场资格。
- 使用 `--watchlist 路径` 可指定其他补充清单；每项需提供六位代码、名称、有效昨收、
  `eligible`、`eligibility_reason`、入场和放弃条件。`eligible` 只表示基础过滤通过，
  不代表已完成评分、题材确认或满足实时入场条件。空 `candidates` 数组可只监控原三只。
  同代码以原日报为准。配置错误或文件缺失会明确报错，修改配置后重启服务生效。
- 启动后固定使用所选计划，不自动替换股票。逐股核对各自计划交易日，
  过期股票继续显示行情，但停用其入场判断；更新对应日报或补充清单后重新启动服务。
- 每次盘口采集的报价和判断追加保存到 `logs/intraday/YYYY-MM-DD.jsonl`。
  可用 `--monitor-log-dir` 指定目录。页面变化记录保留本次启动以来的最近40条。

## 存储双写

默认 `BANXIA_STORAGE_MODE=off`，程序行为与原文件链路一致。启动本地基础设施并安装
`storage` 可选依赖后，可加载 Compose 配置并启用迁移模式：

```bash
set -a
source deploy/compose/.env
set +a
export BANXIA_STORAGE_MODE=best_effort
export BANXIA_CODE_COMMIT="$(git rev-parse --short HEAD)"
```

- `banxia-strategy run` 继续写本地 Markdown、JSON、CSV，同时将内容寻址对象上传至
  MinIO，并在 PostgreSQL 保存策略版本、运行、计划、候选和对象元数据。
- `banxia-strategy serve` 继续写 JSONL，同时通过有界后台队列批量写 ClickHouse
  盘口/分钟线、PostgreSQL 决策状态与 Outbox，并更新 Redis 监控快照。
- `best_effort` 适用于迁移验证：本地链路继续运行，远端错误写入 stderr 或监控 API
  的 `storage.last_error`。`required` 用于要求初始化和队列接收成功的环境。
- 远端写入不替代本地数据，也不包含自动下单能力。后台队列不是 WAL；进入 Kafka/WAL
  阶段前，进程异常仍可能造成尚未落远端的数据丢失。

真实组件双写测试要求 Compose 已健康启动：

```bash
make infra-check
export BANXIA_TEST_POSTGRES_DSN="postgresql://banxia:banxia-local@127.0.0.1:5432/banxia_test"
make integration-test
```

`BANXIA_TEST_POSTGRES_DSN` 必须指向隔离测试库；集成测试不会回退使用业务数据库。

成本回撤4%是预警阈值，并非保证成交的止损价。A股T+1，当天新买的股票不能当天卖出；
本页没有持仓成本信息，不能计算个人盈亏或代替账户风控。电脑休眠、关机或 Web 服务停止
后采集也会停止；恢复后会继续采集并检查数据时效。

## 自动运行

脚本默认在每周一至周五 16:30 生成初版，并于 23:30 基于最新行情覆盖更新。任务会通过
mootdx 交易日历确认日期，周末和节假日不生成报告，也不会回退覆盖前一交易日：

```bash
chmod +x scripts/run_daily.sh scripts/install_launchd.sh
./scripts/install_launchd.sh
```

为避开 macOS 对 `Documents` 的后台访问限制，安装脚本会把独立运行环境放在
`~/Library/Application Support/BanxiaStrategy`，并创建项目内的
`scheduled_reports` 链接。日志保存在该运行目录的 `logs/` 下。卸载任务：

```bash
launchctl bootout "gui/$UID/com.jelven.banxia-strategy"
rm "$HOME/Library/LaunchAgents/com.jelven.banxia-strategy.plist"
```

## 策略管理

打开 <http://127.0.0.1:8765/strategy>，主导航中的“策略管理”位于“次日计划”之前。
默认页面横向展示全部策略的关键参数；点击策略名称进入详情。详情参数分为候选筛选、
个股评分、市场环境、竞价与入场、仓位与退出、运行设置六组。
成交额和市值以亿元输入，比例以百分数输入；文件仍保留元和小数比例的计算单位。

- 详情页仅显示一个动态操作按钮：参数未变化时显示“保存”，用于修改名称；参数变化后显示“另存”。
  保存名称时策略 ID、配置、血缘和历史记录保持不变。
- 完整策略目录模式下，“保存为新策略”会校验类型、上下限、理想评分区间和组合仓位关系，
  提示输入新策略名，创建参数不可变的子策略，并保存父策略与逐项差异；原策略不被覆盖。
- 全库最多一条策略激活。创建时可立即激活，也可稍后切换；删除会永久清理该策略的计划、监控、回测和报告资产。
- 文件兼容模式仍可保存到 `BANXIA_STRATEGY_CONFIG` 指定的文件
  （默认 `config/strategy.json`），但不具备完整多策略能力。
- “恢复默认值”只填入草稿，保存后生效；“撤销修改”恢复本页最近加载/保存的值。
  多页面同时编辑时，通过配置版本检测冲突，避免覆盖其他页面的修改。
- 评分权重、理想区间、历史炸板扣分、题材门槛、入场时间、跌破昨收开关、
  板块确认比例、仓位及回撤预警均可调整。沪深主板、排除 ST、mootdx、
  北京时间、T+1 和不自动下单为当前系统固定约束。
- 激活后，手动刷新或定时报表使用该策略；新报告的 JSON 包含 `strategy_config`、
  `strategy_version`、`code_commit`，每只候选的 `plan` 保存结构化入场规则。
  已生成的报告和已激活的计划不因保存参数而变更。历史报告继续兼容原文字规则。
- 定时任务与页面“刷新”均在每次执行开始时读取最新已保存的配置，包括排队后、
  执行前保存的修改。复盘日期只选择行情日期；刷新历史日期也使用当前策略重算。
  执行中的任务使用该次读取的完整配置，未保存的表单草稿不参与计算。
  最新配置无效时任务失败，不回退使用旧报告的规则。
  次日计划显示本次参数版本；刷新任务结果包含实际 `strategy_version`、
  `strategy_revision`、`run_id` 和报告生成时间，可与策略管理中的配置版本核对。
- 版本由基础版本、配置和代码标识计算，防止同一数据库版本混入不同规则。
  报告发布时，观察清单和计划事件在同一 PostgreSQL 事务中写入。
- `banxia-service report-scheduler` 自动读取报告时间设置；行情采集服务在下一轮
  读取间隔设置，无需重启。JSON 尚未包含相应运行字段时，沿用环境变量
  `BANXIA_REPORT_SCHEDULE`、`BANXIA_QUOTE_INTERVAL_SECONDS` 和
  `BANXIA_IDLE_INTERVAL_SECONDS`，保存后的文件设置优先。
  独立 macOS launchd 的日历不受此页面控制，需重新安装相应定时任务。
- 竞价重排、放量、持续封稳、盘中炸板次数及持仓成本风控仍需人工核验；
  页面调整不会增加自动成交或自动下单能力。

读取接口为 `GET /api/v1/strategy-config?strategy_id=...`。目录模式通过
`POST /api/v1/strategies` 创建新策略，`PATCH /api/v1/strategies/{strategy_id}` 单独修改
名称或切换状态；
此时 `PUT /api/v1/strategy-config` 固定返回 409，防止覆盖。参数校验失败返回 422，
revision 冲突返回 409；配置了 API Bearer token 时沿用相同鉴权。
字段定义和默认值见 `src/banxia_strategy/strategy_config.py`。

## 历史回测与策略优化

主导航新增 [回测优化](http://127.0.0.1:8765/research)：查看近一个月历史次日计划、
逐股实际行情、每天/每周/每月收盘封板准确率，以及独立保存的策略候选和全部实验。
研究数据不会激活历史计划或替换当前执行策略。

```bash
PYTHONPATH=src .venv/bin/python -m banxia_strategy.research \
  --start 2026-08-25 --end 2026-09-24 --config config/strategy.json
```

配置存储连接后添加 `--persist`，将输入快照、报告、CSV、独立参数和全过程归档到
MinIO，并写入 PostgreSQL 研究表。已有数据库需先应用
`migrations/postgres/004_strategy_research.sql`。运行方法、实验结果、指标和限制见
[历史策略研究说明](docs/08-strategy-research.md)。

系统还支持：

- `catalog_backfill`：幂等补齐一年期多策略计划、执行计划和实际行情。
- `execution_analysis`：按分钟线验证“严格可买”，输出日/周/月/年对比。
- `profit_optimization`：按 60/20/20 时间切分、T+1 开盘卖出和每笔 25bp 成本筛选参数。

当前 09:45 截止候选只基于 9 笔严格可买样本，已独立保存但未激活，不代表未来盈利保证。
完整命令和结果见 [策略研究](docs/08-strategy-research.md)。

## 统计口径

默认评分满分约 100（调整权重后按新权重计算）：

- 封板质量：30 分。
- 流动性：20 分。
- 题材强度：25 分。
- 板块地位：15 分。
- 市场环境：10 分。

行情统一来自 `mootdx` 接入的通达信公开行情服务器。工具从沪深主板日线自行计算涨停、
炸板和连板，并结合历史分时、收盘盘口、流通股本及通达信概念板块生成评分字段。首次
运行通常需要约 20～40 秒；数据源可能延迟或临时不可用，因此日报会保留数据来源、
生成时间和实际数据交易日，交易前仍应与券商行情核对。

## 验证

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

当前快速测试基线为 158 项通过、8 项按环境跳过。真实存储集成测试需先启动 Compose：

```bash
make infra-check
export BANXIA_TEST_POSTGRES_DSN="postgresql://banxia:banxia-local@127.0.0.1:5432/banxia_test"
make integration-test
```

当前版本没有集合竞价和 Level-2 数据；封单金额取通达信收盘五档盘口，无法代表真实排队
成交概率，也不应直接用于自动交易。严谨评价策略仍需要完整交割单，并进行手续费、滑点、
涨跌停无法成交和幸存者偏差处理后的回测。
