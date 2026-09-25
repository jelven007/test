# 历史次日计划、准确率与独立策略研究

入口：`/research`（主导航“回测优化”）。支持切换实验、日/周/月统计、逐日候选与实际
开高低收、入场/放弃/退出条件、参数变化、实验日志和下载。研究候选保持独立，不自动启用。

## 本次结果（2026-08-25 至 2026-09-24）

- 用 mootdx 重建 23 个参考交易日的计划；其中 22 个执行日有完整次日日线。
  按 mootdx 交易日历，最后一期执行日为 09-28，3 个候选待验证，不计入准确率。
- 基线使用实验开始时保存的完整配置，不使用默认值，也不回写 `config/strategy.json`。
- 两轮分别保存 18、42 个配置实验。首轮改变题材涨停数量门槛，最终候选结果与基线相同。
  扩展轮探索评分门槛、题材数量、炸板次数、题材分散、成交额和评分权重。
- 扩展轮选定“同题材数量上限 5 → 1”，其他参数保留。全区间：
  基线 **15/66 = 22.73%**，研究候选 **14/66 = 21.21%**。
  留出区间基线 **40.00%**、研究候选 **26.67%**。
- 结果不支持替换当前策略。扩展轮复用了首轮的留出日期，明确标记
  `holdout_reused=true`；后续需要新交易日验证，不能把这次留出比较当成独立样本外证据。

## 准确率定义

主指标：**次日收盘封住涨停的候选数 / 有完整次日日线的候选数**。

盘中触及涨停与收盘涨停分开统计。未收盘为 `pending`，缺日线、停牌或无效价格为
`missing`，均不进入主指标分母；无验证样本时准确率为 `null`，不是 0% 或 100%。
同时显示总候选数、待验证数、缺失数和数据覆盖率。

日统计按计划**执行日**分组；周按 ISO 周，月按自然月。周/月准确率为总命中数除以
总已验证候选数，同时保存有效日准确率均值。一个月窗口跨两个自然月，不能作为两个完整月验证。
汇总含 Wilson 95% 区间。全区间结果包含训练期，只作描述性比较。

竞价合格采用日线开盘价近似，单列过滤后的封板命中率。历史分钟线不足以确认秒级封稳、
完整板块助攻、排队成交和滑点，因此 `execution_accuracy_pct` 始终为 `null`；
本研究不是实际交易收益回测。

## 实验协议与数据边界

22 个有标签参考日按时间分成训练 13 日、验证 4 日、留出 5 日。
搜索前保存完整参数网格及协议；目标为 40% 候选加权命中率 + 30% 日均命中率 +
30% 周均命中率。训练阶段取前 3 名，验证集定选，保存选择记录后才评估留出集。
候选数和有效日均不得少于基线 60%，且至少 5 个验证候选、3 个有效日，不得有缺失标签。

不据留出集挑选最优参数；没有通过提升与样本约束也保留研究候选和失败结果。
不放宽入场时间、开盘阈值或仓位约束。每次后续实验生成新 ID，原始记录保留。

历史池在每日回放时深拷贝，拒绝读取参考日期之后的涨停池与炸板池。
历史封单统一缺失处理，不使用当前盘口。采集覆盖主板 3307 个代码，其中 3196 个有日线，
111 个缺失代码单独记录。流通股本、名称、题材分类使用采集时快照，存在时点偏差和
幸存者偏差，所以不能将结果描述为完全无偏的历史回测。

## 运行与保存

```bash
PYTHONPATH=src .venv/bin/python -m banxia_strategy.research \
  --start 2026-08-25 --end 2026-09-24 --config config/strategy.json
```

省略日期时默认上月同日至昨天，仅接受已完整收盘的历史区间。行情采集按日期检查点复用；
`--snapshot 文件` 可以直接重放已有快照。新实验在开始时冻结配置。
扩展已有实验时传入 `--prior-run-id UUID`，保留前序关系并标注留出日期复用。

本地保存路径：

```text
research/20260825-20260924/
  inputs/snapshot.json
  runs/<UUID>/
    protocol.json                    # 搜索范围、指标、日期划分
    trials/*.json                    # 各次训练与入围验证的逐日输出
    selection-before-holdout.json     # 评估留出集前定选的策略
    baseline/<参考日>/plan.json
    baseline/<参考日>/review.json
    optimized/<参考日>/plan.json
    optimized/<参考日>/review.json
    baseline-daily.csv
    optimized-daily.csv
    optimized-strategy.json           # 可单独读取的完整配置
    strategy-metadata.json
    result.json                      # 全部逐股结果、日周月统计
    report.md
    source/                          # Python 源码快照
    source-manifest.json              # 代码哈希及捕获阶段
    experiment.tar.gz                # --persist 时生成，含行情快照
    persistence.json                 # 远端对象及哈希
```

本次两轮源码在实验结束、入库前归档，manifest 明确记录此阶段；首轮/扩展轮的实际搜索
范围以各自 `protocol.json` 为准。后续运行在实验开始前保存代码快照。
原始输入、全部参数与每日结果均保留，不再对失败实验反复覆盖。

添加 `--persist` 会先校验输入哈希和磁盘结果一致，再上传 MinIO，最后在一个 PostgreSQL
事务中保存 `research_run`、`research_daily`、`research_strategy`。这三张表独立于
当前计划、候选、观察清单和交易事件；数据库约束要求研究策略 `active=false`。
数据库和 MinIO 凭据使用 `BANXIA_POSTGRES_DSN`、`BANXIA_MINIO_ENDPOINT`、
`BANXIA_MINIO_ACCESS_KEY`、`BANXIA_MINIO_SECRET_KEY` 等现有环境变量。

已有 PostgreSQL 数据卷需先执行新增迁移（首次初始化会自动执行）：

```bash
docker compose --env-file deploy/compose/.env -f deploy/compose/docker-compose.yml \
  exec -T postgres sh -c 'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -v ON_ERROR_STOP=1' \
  < migrations/postgres/004_strategy_research.sql
```

Compose API 从 PostgreSQL 读取记录、从 MinIO 流式下载。
独立 `banxia-strategy serve` 从当前工作目录 `research/` 读取本地结果。
线上接口沿用现有 Bearer token 鉴权：

| 接口 | 用途 |
|---|---|
| `GET /api/v1/research` | 实验列表，最新在前 |
| `GET /api/v1/research/{run_id}` | 完整计划、复盘、日周月和优化结果 |
| `GET /api/v1/research/{run_id}/strategy` | 下载独立策略 JSON |
| `GET /api/v1/research/{run_id}/assets/{filename}` | 下载报告、CSV 或完整归档 |
