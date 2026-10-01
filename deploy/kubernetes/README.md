# Kubernetes 部署

`base/` 是云厂商无关的应用基线，`overlays/volcengine/` 是火山引擎
VKE（ECS Worker 节点）生产部署。当前生产 overlay 采用经济型自托管
方案：PostgreSQL、Kafka、ClickHouse、Redis 和 MinIO 以单副本
StatefulSet 运行，并使用独立 EBS 云盘持久化。

截至 `2026-10-01`，该 overlay 已部署到集群 `cdaus2p98m08ce3b12nn0`，生产入口为
<https://shanao.asia>。架构和数据链路说明见
[当前生产技术方案](../../docs/13-current-production-solution.md)。

VKE overlay 包含：

- API、业务 Worker、工作日报 CronJob 和 Flink 工作负载。
- 使用 NAS CSI 的 RWX 报告目录。
- 使用独立 RWO 云盘保存每个 `market-collector` 副本的本地 WAL。
- 使用 `Retain` 回收策略的 EBS 云盘保存五个数据服务的数据。
- PostgreSQL/ClickHouse 迁移 Job、Kafka topic、MinIO bucket 和策略目录初始化 Job。
- 创建公网 ALB 的 HTTPS Ingress，并将 HTTP 重定向到 HTTPS。
- PDB、HPA、跨可用区/节点软分散和默认拒绝入站的 NetworkPolicy。
- 可选的 ServiceMonitor/PodMonitor。

## 前置条件

1. VKE 集群已安装 NAS CSI、ALB Ingress Controller 和 metrics-server。
2. `ssl-redirect` 要求 ALB Ingress Controller `v0.35.0` 或更高版本。
3. VKE Worker 子网可通过 NAT 访问 mootdx 行情节点。
4. 已创建 CR 镜像仓库，并镜像应用、Flink 及全部基础镜像。
5. 已开通 ALB 服务授权，并准备证书中心证书。
6. 已准备 NAS 文件系统；集群已提供 `ebs-ssd` StorageClass。

火山引擎 ESSD 云盘的最小容量为 10GiB，数据卷和 Collector WAL 均不得
低于此容量。

## 配置占位符

部署 VKE overlay 前必须替换 overlay 和 Secret 模板中的所有占位符：

```bash
rg -n 'REPLACE_WITH_' \
  deploy/kubernetes/overlays/volcengine \
  deploy/kubernetes/banxia-secrets.example.yaml
```

`base/` 中仍保留云厂商无关的占位符；VKE overlay 会覆盖这些值，不需要
直接修改 Base。

主要配置位置：

- `overlays/volcengine/kustomization.yaml`：CR 地址和 Git commit 镜像标签。
- `overlays/volcengine/data-services.yaml`：集群内数据服务及 EBS 容量。
- `overlays/volcengine/runtime-config.yaml`：集群内数据服务地址。
- `overlays/volcengine/flink-*-patch.yaml`：Flink 使用的 Kafka 和 MinIO 地址。
- `overlays/volcengine/storage.yaml`：NAS 文件系统 ID 和挂载地址。
- `overlays/volcengine/collector-statefulset.yaml`：WAL 使用的块存储类。
- `overlays/volcengine/ingress.yaml`：新建 ALB 的子网和证书中心证书 ID。
- `banxia-secrets.example.yaml`：数据库、Redis、对象存储及 CR 凭据。

`BANXIA_MINIO_ENDPOINT` 不带 URL scheme；Flink 配置中的
`s3.endpoint` 使用完整的 `http://minio:9000` 地址。密码出现在
PostgreSQL DSN 或 Redis URL 中时必须进行 URL 编码。

## Secret

示例文件不能原样部署，也不能提交替换后的真实密钥：

```bash
cp deploy/kubernetes/banxia-secrets.example.yaml \
  deploy/kubernetes/banxia-secrets.yaml
# 编辑 banxia-secrets.yaml，替换全部 REPLACE_WITH_* 值
kubectl create namespace banxia --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f deploy/kubernetes/banxia-secrets.yaml
```

## 渲染与部署

迁移 ConfigMap 直接引用仓库根目录的 SQL 文件，因此需要显式允许
Kustomize 读取 overlay 目录之外的文件：

```bash
kubectl kustomize \
  --load-restrictor LoadRestrictionsNone \
  deploy/kubernetes/overlays/volcengine \
  > /tmp/banxia-vke.yaml

! rg -n 'REPLACE_WITH_' /tmp/banxia-vke.yaml
kubectl apply --server-side --dry-run=server -f /tmp/banxia-vke.yaml
kubectl apply --server-side -f /tmp/banxia-vke.yaml
```

首次部署先创建 Secret、StorageClass 和数据服务，完成旧 ECS 数据恢复后
再应用完整清单。不要在恢复前启动采集器、消费者或定时任务。完整清单应用后，
等待数据库迁移和目录初始化完成，再检查工作负载：

```bash
kubectl -n banxia wait \
  --for=condition=complete job/banxia-schema-migration --timeout=15m
kubectl -n banxia wait \
  --for=condition=complete job/banxia-catalog-bootstrap --timeout=15m
kubectl -n banxia rollout status deployment/api --timeout=10m
kubectl -n banxia get pods,pvc,ingress
```

固定名称的 Job 不会因再次 `apply` 自动重跑。发布了新迁移或需要重新执行
初始化时，先删除对应 Job，再重新应用渲染结果：

```bash
kubectl -n banxia delete job \
  banxia-schema-migration banxia-kafka-bootstrap \
  banxia-minio-bootstrap banxia-catalog-bootstrap \
  flink-feature-job-v1 --ignore-not-found
kubectl apply --server-side -f /tmp/banxia-vke.yaml
```

PostgreSQL Job 使用 `banxia.schema_migration` 记录已完成脚本；ClickHouse
脚本使用 `IF NOT EXISTS`，可重复执行。

## DNS 与入口

ALB 就绪后查询公网地址，并把 `shanao.asia` 的 A 记录指向该地址：

```bash
kubectl -n banxia get albinstance banxia-alb
kubectl -n banxia get ingress banxia-api
```

入口由 ALB 终止 TLS，后端 Service 使用 HTTP `80 -> 8765`。不要再把域名
解析到旧 ECS 公网 IP。

## 可选监控

只有集群已安装 Prometheus Operator CRD 时才应用：

```bash
kubectl get crd servicemonitors.monitoring.coreos.com
kubectl get crd podmonitors.monitoring.coreos.com
kubectl label namespace <PROMETHEUS_NAMESPACE> \
  banxia.io/metrics-access=true --overwrite
kubectl apply -f \
  deploy/kubernetes/overlays/volcengine/servicemonitor.yaml
```

每次生产发布后都应验证桌面端和移动端页面、mootdx 出网、数据库备份状态、
collector 主备、Pod 驱逐和 ALB 健康检查。
