# 本地基础设施

该 Compose 环境用于生产化迭代的本地开发和组件测试，不代表生产拓扑。

包含：

- PostgreSQL 16
- ClickHouse 24.8
- Redis 7.4
- MinIO
- Kafka 3.9 单节点 KRaft
- Flink 1.20 JobManager、TaskManager 和实时特征作业
- `market-collector`、`market-sink`、`strategy-engine`、`outbox-relay`
- `projection-worker`、`report-scheduler`、FastAPI 与 Prometheus

默认实时指标由 Flink 生成。仅在排查 Flink 故障时，使用
`--profile python-feature-fallback` 启动确定性的 Python 降级 Worker，
不要同时运行两套特征生产者。

## 启动

```bash
cp deploy/compose/.env.example deploy/compose/.env
make infra-config
make infra-up
make infra-status
make infra-check
```

`report-scheduler` 随常驻服务启动，默认在交易日 16:30 生成初版、23:30 覆盖更新。
非交易日由 mootdx 交易日历校验后跳过。调度器重启后会补跑最近缺失的交易日报；
报告任务通过 host 网络访问 mootdx，并通过完成标记区分完整持久化和残留文件。
也可以手工运行一次性 Worker：

```bash
docker compose \
  --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  --profile jobs run --rm report-worker
```

应用侧适配器依赖和环境变量：

```bash
.venv/bin/pip install -e '.[storage]'
set -a
source deploy/compose/.env
set +a
export BANXIA_STORAGE_MODE=best_effort
```

`off`（默认）完全禁用新存储；`best_effort` 保留文件主链路并显式报告远端失败；
`required` 会在适配器初始化、日报双写或盘中队列无法接收数据时返回错误。盘中远端写入
使用有界后台队列，状态可在 `/api/monitor` 的 `storage` 字段查看。

服务端口：

| 服务 | 地址 |
| --- | --- |
| PostgreSQL | `127.0.0.1:5432` |
| ClickHouse HTTP | `http://127.0.0.1:8123` |
| ClickHouse Native | `127.0.0.1:9000` |
| Redis | `127.0.0.1:6379` |
| MinIO API | `http://127.0.0.1:9002` |
| MinIO Console | `http://127.0.0.1:9001` |
| Kafka | `127.0.0.1:29092` |
| Flink | `http://127.0.0.1:8081` |
| Strategy API | `http://127.0.0.1:8765` |
| Prometheus | `http://127.0.0.1:9090` |

默认凭证只允许本地开发。共享环境必须修改 `.env`，生产环境必须使用密钥管理服务。

## 初始化行为

- PostgreSQL 首次创建数据卷时执行 `migrations/postgres/*.sql`。
- ClickHouse 首次创建数据卷时执行 `migrations/clickhouse/*.sql`。
- `minio-init` 创建并启用版本控制：
  - `market-raw`
  - `strategy-reports`
  - `strategy-audit`
  - `flink-state`
- `kafka-init` 创建业务 Topic 和对应 DLQ。

初始化脚本只在空数据卷或显式初始化容器中运行。修改历史迁移后，不应直接依赖重启容器
覆盖已有数据库；后续变更必须增加新的有序迁移文件。

## 验证

完整冒烟检查：

```bash
make infra-check
make integration-test
```

`integration-test` 会向四类存储写入固定的 `2099-01-02/03` 测试数据，并验证报告对象哈希、
PostgreSQL 事务记录、ClickHouse 幂等查询和 Redis TTL。建议在独立测试卷上执行。

也可以逐项检查：

```bash
docker compose \
  --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  ps

docker compose \
  --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml \
  exec postgres \
  psql -U banxia -d banxia -c "\dt banxia.*"

curl "http://127.0.0.1:8123/?user=banxia&password=banxia-local" \
  --data-binary "SHOW TABLES FROM banxia"
```

## 停止

```bash
make infra-down
```

`infra-down` 不删除数据卷。确需删除本地数据时，应明确执行带 `--volumes` 的 Compose 命令，
并确认没有需要保留的测试数据。
