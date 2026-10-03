# 火山引擎生产资源清单

## 1. 盘点范围

本文记录 `2026-10-02` 当前生产系统在火山引擎上直接使用或由 VKE 自动创建的资源。

归属判断依据：

- VKE 集群 ID、VKE 自动标签和资源挂载关系。
- Kubernetes Node、PV/PVC、Ingress 和系统组件运行状态。
- 火山引擎控制台中的实例、网络、计费方式和关联资源。
- 生产域名 `shanao.asia` 的实际入口。

火山账号中存在其他项目资源，本文不使用账号首页的全局资源数量作为系统数量。

## 2. 汇总结论

当前生产系统涉及 **14 类火山引擎产品**：

1. 容器服务 VKE
2. 云服务器 ECS
3. 弹性容器实例 VCI
4. 弹性块存储 EBS
5. 文件存储 NAS
6. 私有网络 VPC
7. 公网 NAT 网关
8. 公网 IP EIP
9. 应用型负载均衡 ALB
10. 传统型负载均衡 CLB
11. 弹性伸缩
12. 镜像仓库 CR
13. 证书中心
14. 托管 Prometheus

生产核心资源汇总：

| 资源 | 当前数量或容量 |
| --- | --- |
| VKE 托管集群 | 1 个 |
| ECS 生产节点 | 3 台，共 12 vCPU、48 GiB |
| VCI 实例 | 13 个，共申请 23.5 Core、58 GiB |
| VKE EBS 云盘 | 13 块，共 740 GiB |
| NAS 文件系统 | 1 个 |
| NAT 网关 | 1 个 |
| 系统关联 EIP | 3 个 |
| 业务 ALB | 1 个 |
| API Server CLB | 1 个 |
| 弹性伸缩组 | 1 个 |
| CR 实例 | 1 个，1 个命名空间、8 个仓库 |
| SSL 证书 | 1 张 |
| Prometheus 工作区 | 1 个 |

## 3. 计算与容器

### 3.1 容器服务 VKE

| 属性 | 当前值 |
| --- | --- |
| 集群名称 | `cluster-XM` |
| 集群 ID | `cdaus2p98m08ce3b12nn0` |
| 类型 | 托管集群 |
| 地域/可用区 | 华东 2（上海）/ 可用区 A |
| Kubernetes | `v1.36.1-vke.4` |
| 网络模型 | VPC-CNI、共享弹性网卡、IPv4 单栈 |
| Service CIDR | `10.0.0.0/15` |
| 转发模式 | eBPF |
| 节点池 | 1 个 |
| ECS 节点 | 3 个，全部 Ready |
| 虚拟节点 | 1 个，运行中 |

VKE 负责 Kubernetes 控制面、调度、组件管理、升级和节点池管理。业务数据服务仍运行在
集群内，并不是火山托管数据库。

### 3.2 ECS 工作节点

3 台生产节点均为包年包月、自动续费，到期时间为 `2026-11-01 23:59:59`：

| 节点 | 实例 ID | 规格 | 私网 IP |
| --- | --- | --- | --- |
| `node-0000000` | `i-yew9srlssgay8n8nyamo` | `ecs.g4il.xlarge`，4C16G | `172.31.79.233` |
| `node-0000001` | `i-yew9srlssgay8n8nzxxv` | `ecs.g4il.xlarge`，4C16G | `172.31.79.217` |
| `node-0000002` | `i-yew9srlssgay8n8o0v7f` | `ecs.g4il.xlarge`，4C16G | `172.31.79.218` |

节点没有独立公网 EIP，通过 NAT 网关访问 mootdx。

### 3.3 弹性容器实例 VCI

VKE 配置了一个虚拟节点：

- 名称：`vci-node1-cn-shanghai-a`
- ID：`ndaus5l4366rpjalqddi0`
- 私网 IP：`172.31.79.202`
- 当前 VCI 实例：13 个
- 当前资源申请：23.5 Core、58 GiB

VCI 当前承载 CoreDNS、APIG Controller、EBS/NAS CSI Controller、Metrics Server、
Snapshot Controller 和 Prometheus Agent 等系统 Pod。

### 3.4 弹性伸缩

VKE 节点池关联一个弹性伸缩组：

- Scaling Group：`scg-yew9srf3edan0mqy9sbz`
- 节点池：`pdausev1o4a5057n7c3f0`

弹性伸缩控制节点池实例生命周期；实际计算费用仍由 ECS 产生。

## 4. 存储

### 4.1 EBS 云盘

VKE 共使用 13 块 ESSD PL0，合计 740 GiB。

节点基础盘：

| 类型 | 数量 | 单盘容量 | 合计 | 计费 |
| --- | ---: | ---: | ---: | --- |
| ECS 系统盘 | 3 | 100 GiB | 300 GiB | 包年包月 |
| ECS 节点数据盘 | 3 | 100 GiB | 300 GiB | 包年包月 |

应用 PVC：

| 用途 | PVC | 云盘 ID | 容量 |
| --- | --- | --- | ---: |
| PostgreSQL | `data-postgres-0` | `vol-3xdjzijmw65az39dv9cb` | 20 GiB |
| Redis | `data-redis-0` | `vol-3xdjziksbe2wua8uaa8s` | 10 GiB |
| Kafka | `data-kafka-0` | `vol-3xdjzii8bq5az39dv99j` | 20 GiB |
| ClickHouse | `data-clickhouse-0` | `vol-3xdjzii8bq5az39dv99f` | 40 GiB |
| MinIO | `data-minio-0` | `vol-3xdjzii8bq5az39dv9bf` | 30 GiB |
| Collector WAL 0 | `wal-market-collector-0` | `vol-3xdk2ex4125az39e6zu0` | 10 GiB |
| Collector WAL 1 | `wal-market-collector-1` | `vol-3xdk2ex4125az39e6zu1` | 10 GiB |

7 块应用盘按量计费，并采用 `Retain` 回收策略。当前没有配置自动快照策略。

### 4.2 文件存储 NAS

| 属性 | 当前值 |
| --- | --- |
| 名称 | `banxia-reports` |
| 文件系统 ID | `enas-cnsha08273a84410dd` |
| Kubernetes PVC | `banxia-reports` |
| PVC 申请容量 | 10 GiB |
| 用途 | API、策略引擎和报告任务共享报告目录 |
| 回收策略 | `Retain` |

NAS 使用 NFS v3 挂载，实际计费以 NAS 账单用量为准。

## 5. 网络与公网

### 5.1 VPC

生产集群使用：

- VPC：`Default`
- VPC ID：`vpc-1pm13zlt8zq4g643rfzcpm2z8`
- CIDR：`172.31.0.0/16`
- 核心子网：`Subnet-WZQD`
- 子网 ID：`subnet-7uuemtcskd1c72200s8k98yw`
- 节点安全组：`cdaus2p98m08ce3b12nn0-common`
- Pod 安全组：`cdaus2p98m08ce3b12nn0-pod`

控制面、Pod、VCI、ALB 和 ECS 节点均依赖该 VPC。

### 5.2 NAT 网关

| 属性 | 当前值 |
| --- | --- |
| 名称 | `NATGW-sOuu` |
| ID | `ngw-2hrn9ajwpze2o777owzrknykv` |
| VPC | `Default` |
| 模式 | 直通模式 |
| 计费 | 按量计费、按使用量 |
| EIP | `118.196.108.124` |

该资源带有 VKE 集群标签，是节点和 Pod 访问 mootdx、镜像源及其他公网服务的出网通道。

### 5.3 公网 IP

系统当前关联 3 个 EIP：

| 用途 | EIP ID | 地址 | 带宽上限 | 计费 |
| --- | --- | --- | ---: | --- |
| 业务 ALB | `eip-2ctz5s1qpklj42x5w1eht5034` | `118.196.108.134` | 5 Mbps | 按实际流量 |
| VKE API Server | `eip-2xs2dcvx2jcao28ojqb1ym4f3` | `118.196.122.161` | 10 Mbps | 按实际流量 |
| NAT 网关 | `eip-9mb01jvms1z45umv6vtqpfs` | `118.196.108.124` | 200 Mbps | 按实际流量 |

200 Mbps 是带宽上限；按流量计费时不代表持续按 200 Mbps 固定收费。

### 5.4 应用型负载均衡 ALB

| 属性 | 当前值 |
| --- | --- |
| 名称 | `banxia-alb` |
| ID | `alb-1vyswvxt39la83766qanxn7ey` |
| 版本 | 基础版 |
| 公网/私网 IP | `118.196.108.134` / `172.31.79.243` |
| 监听 | HTTP 80、HTTPS 443 |
| 用途 | `shanao.asia` 生产入口和 TLS 终止 |
| 删除保护 | 开启 |

### 5.5 传统型负载均衡 CLB

| 属性 | 当前值 |
| --- | --- |
| 名称 | `cdaus2p98m08ce3b12nn0-apiserver-lb-internal` |
| ID | `clb-3qe17svi74jk07prml17vipej` |
| 规格 | 小型 II |
| 私网/公网 IP | `172.31.79.199` / `118.196.122.161` |
| 监听 | TCP 6443 |
| 用途 | VKE API Server 私网和公网接入 |
| 计费 | 按量、按规格 |

这是 VKE 自动创建的控制面 CLB，不是此前已经删除的业务 CLB。关闭 API Server
公网访问只影响公网 EIP 和本地 `kubectl`，不能直接删除该私网控制面 CLB。

## 6. 镜像、证书与监控

### 6.1 镜像仓库 CR

| 属性 | 当前值 |
| --- | --- |
| 实例 | `xm1001` |
| 规格 | 小微版 |
| 计费 | 混合计费，部分预付 |
| 有效期 | `2026-10-01` 至 `2026-11-01 23:59:59` |
| 命名空间 | `banxia`，1/5 |
| 仓库 | 8/300 |
| 内网访问 | 开启 |
| 公网访问 | 开启 |
| 本月公网流出 | 0 MB |

8 个私有仓库：

- `banxia-strategy`
- `banxia-flink`
- `postgres`
- `redis`
- `kafka`
- `clickhouse`
- `minio`
- `minio-mc`

### 6.2 证书中心

| 属性 | 当前值 |
| --- | --- |
| 证书 ID | `cert-0b9f4e0c80eb4a9e8a0b2321cc063836` |
| 证书实例 | `test` |
| 品牌/类型 | DigiCert 免费版、DV、单域名 |
| 域名 | `shanao.asia`、`www.shanao.asia` |
| 状态 | 已签发 |
| 到期 | `2027-01-01 07:59:59` |

证书部署在 ALB HTTPS 监听器上。

### 6.3 托管 Prometheus

- 工作区：`embodied-data`
- 已启用：容器服务、控制面、DNS、CNI、CSI 基础指标。
- 集群内运行 Prometheus Agent、Allocator、Kube State Metrics、Node Exporter 和
  O11y Agent Operator。

指标采集和存储产生的具体费用需按工作区账单核对。

### 6.4 未启用的观测能力

- VKE 容器日志未安装 `log-collector`。
- VKE 集群审计未开启。
- 因此当前系统未实际使用日志服务 TLS 保存容器日志和审计日志。

### 6.5 平台基础能力

以下能力被系统使用，但没有独立业务实例，因此未计入前述 14 类资源产品：

- 访问控制 IAM：VKE、弹性伸缩、CSI 和镜像访问所需的角色与服务授权。
- 资源管理：资源均归属 `default` 项目，并使用 VKE 自动标签识别关联关系。
- 云监控：提供 ECS、EIP、NAT 和负载均衡的基础监控入口；深度容器指标由托管
  Prometheus 承担。

## 7. 明确未使用的火山托管产品

当前生产没有使用以下托管数据服务：

| 产品 | 当前替代方案 |
| --- | --- |
| ByteRDS/PostgreSQL | VKE 内单副本 PostgreSQL StatefulSet |
| 托管 Redis | VKE 内单副本 Redis StatefulSet |
| 托管 Kafka | VKE 内单 Broker Kafka StatefulSet |
| ByteHouse | VKE 内单副本 ClickHouse StatefulSet |
| TOS | VKE 内单副本 MinIO StatefulSet |
| 火山云解析 DNS | 域名 DNS 托管在阿里云 |
| CDN/WAF | 当前域名直接进入公网 ALB |
| 日志服务 TLS | VKE `log-collector` 未安装，集群审计未开启 |

这些产品没有出现在当前生产链路中，不应计入当前系统成本。

## 8. 费用关注点

固定或周期性费用主要来自：

- 3 台包年包月 ECS。
- 6 块随节点购买的 100 GiB 云盘。
- CR 小微版实例。

按量费用主要来自：

- 13 个 VCI 实例。
- 7 块应用 EBS。
- NAS 实际用量。
- NAT 网关。
- 3 个 EIP 的实际流量。
- ALB 和 API Server CLB。
- 托管 Prometheus 指标采集与存储。

当前控制台提示账号余额低于 100 元。由于账号包含大量其他项目资源，准确金额应在费用中心
按本文资源 ID、项目和标签过滤，不能使用账号总账直接归因到本系统。

## 9. 优先优化建议

1. 建立 VPC 内运维入口后关闭 API Server 公网访问，释放 10 Mbps EIP；保留私网 CLB。
2. 核对 13 个系统 VCI 实例账单，评估将部分系统组件调度到空闲 ECS 节点。
3. 为 PostgreSQL、ClickHouse 和 MinIO 建立 EBS 快照或独立备份；当前 7 块应用盘没有
   自动快照策略。
4. 按月核对 ECS、VCI、EBS、NAT、EIP、ALB、CLB、NAS、CR 和 Prometheus 十类费用。
