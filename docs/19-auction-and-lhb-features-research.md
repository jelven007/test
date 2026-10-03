# 竞价与龙虎榜特征探索：新维度能否突破现有胜率瓶颈？

> 实验窗口：2025-01-01 ~ 2026-09-28  
> 样本：D1 涨幅 ≥ 3%，剔除 D2/D3 公司行为（送股、合股、分红），剔除 D1 完全封板样本  
> 入场：D2 09:31 开盘（除 Variant B 用 09:40）；退出：D3 09:31 开盘  
> 费率：双边合计 0.30%  
> Holdout：D1 日期 ≥ 2026-07-01

---

## 背景与动机

此前通过"D1 收盘前 30 分钟量价结构"（Rule A：`r_last30 < -0.5%` 且 `close_loc < 0.75`）在 2026H2
holdout 的 D1≥3% 候选集上取得 **win% 49.35%, mean_net -0.37%, Wilson L 41.57%**，距离
"月度收益≥5% 概率≥80%" 目标仍有差距。本次实验探索两类新特征：

1. **类集合竞价特征** —— 从 D2 开盘跳空、首 10 分钟价量结构。
2. **龙虎榜（LHB）特征** —— 从 akshare 回溯 2025-01 ~ 2026-09 的全量龙虎榜明细。

> **真·集合竞价明细（09:15-09:25 tick）由于数据源限制无法回溯**：mootdx 历史分笔接口返回空；
> 实时分笔仅保留当日且最早 09:30；akshare 无稳定历史接口。因此改为上述两个"替代维度"。

---

## 数据工程

### 龙虎榜回溯

脚本 [collect_lhb_history.py](file:///Users/bytedance/Documents/trae_projects/test/scripts/collect_lhb_history.py)
按月分文件缓存 `research/auction-features/lhb_cache/YYYYMM.csv.gz`，共 **21 个月，35015 行，29761
组 (symbol, 上榜日)**。

字段保留 21 列，其中后见信息（"上榜后 1/2/5/10 日"）仅作为参考输出，不进入建模特征。
D1 可用特征：

- `龙虎榜净买额占总成交比`（分析脚本内聚合为 `lhb_net_buy_pct`）
- `换手率` → `lhb_turnover`
- `流通市值` → `lhb_mktcap_floating`
- `解读` 文本抽取：`lhb_inst_buy` / `lhb_inst_sell` / `lhb_inst_count`
- `上榜原因` 文本抽取：`reason_up` / `reason_turnover` / `reason_amp` / `reason_down`

### 类竞价特征

由 `research/monthly-target-v1-3pct/daily/<sym>.json.gz` 的 D1 收盘与
`research/monthly-target-v1-3pct/minutes/<d2>_<sym>.json.gz` 的 D2 分钟构造：

- `gap_pct_d2 = (D2 price[0] / D1 close - 1) * 100`
- `early_price_strength_10min_pct = (max(D2 prices[:10]) / D2 price[0] - 1) * 100`
- `early_pullback_10min_pct = (min(D2 prices[:10]) / D2 price[0] - 1) * 100`
- `early_up_ratio_10min = up_tick_count / 9`
- `early_vol_ratio_3min = (sum vol[:3] / 3) / (day_vol / 240)`

**严格 lookahead-free 处理**：首 10 分钟内的所有特征，只在"推迟到 09:40 买入"（Variant B）
场景下合法使用；09:31 买入场景（Variant A）只能用 `gap_pct_d2`。

---

## 结果

### LHB 特征（[lhb_report.json](file:///Users/bytedance/Documents/trae_projects/test/research/auction-features/lhb_report.json)）

9036 条候选样本中 LHB 命中 2256 条，占比 24.97%。

| 过滤规则 | n | win% | Wilson L | mean_net | share |
|---|---:|---:|---:|---:|---:|
| 基线 (not_sealed) | 9036 | 43.82 | 42.80 | -0.229 | 100.00% |
| Rule A（D1 最后 30 分钟弱） | 1190 | 45.63 | 42.82 | -0.036 | 13.17% |
| **LHB 命中 D1（全部）** | 2256 | **43.75** | 41.72 | -0.397 | 24.97% |
| LHB 命中 & inst_buy | 1366 | 44.22 | 41.60 | -0.408 | 15.12% |
| LHB 命中 & net_buy>5% | 774 | 44.70 | 41.23 | -0.141 | 8.57% |
| LHB 命中 & reason_amp（振幅） | 164 | **37.80** | 30.74 | **-1.955** | 1.81% |
| Rule A + LHB 命中 | 235 | 46.81 | 40.53 | +0.189 | 2.60% |
| Rule A + LHB 未命中 | 955 | 45.34 | 42.21 | -0.092 | 10.57% |

#### 2026H2 holdout

| 过滤规则 | n | win% | Wilson L | mean_net |
|---|---:|---:|---:|---:|
| 基线 | 1334 | 42.58 | 39.95 | -0.799 |
| Rule A | 154 | **49.35** | 41.57 | -0.370 |
| LHB 命中 | 287 | 42.86 | 37.26 | -1.351 |
| LHB 命中 & reason_amp（振幅） | 22 | 45.45 | 26.92 | -1.559 |
| Rule A + LHB 命中 | 23 | 60.87 | 40.79 | -0.738 |

**结论**：
1. **LHB 单独命中几乎无信号**：全区间 win% 43.75% ≈ 基线 43.82%，holdout 42.86% ≈ 基线 42.58%。
2. **"机构卖出"特征在 D1≥3% 候选集上几乎不触发（0 命中）** —— 合理，机构卖出常伴随跌停。
3. **振幅过大（reason_amp）是负 Alpha**：全区间 win% 37.80%, mean_net -1.955%。可当反向过滤
   删除此类样本，但影响面仅 1.81%，边际效益有限。
4. **Rule A + LHB 命中 holdout n=23, win% 60.87%** 看似强势，但样本量过小（Wilson L 仅
   40.79%），不能确认是信号而非随机波动。

### 类竞价特征（[quasi_auction_report.json](file:///Users/bytedance/Documents/trae_projects/test/research/auction-features/quasi_auction_report.json)）

#### Variant A：09:31 买入（仅 `gap_pct_d2` 合法）

**Holdout**：

| 过滤规则 | n | win% | Wilson L | mean_net |
|---|---:|---:|---:|---:|
| 基线 | 1334 | 42.58 | 39.95 | -0.799 |
| Rule A | 154 | 49.35 | 41.57 | -0.370 |
| gap_pct_d2 > 0 | 505 | 41.19 | 36.98 | -1.150 |
| gap_pct_d2 ≥ 1 | 306 | 42.16 | 36.75 | -1.300 |
| gap_pct_d2 ∈ [0, 2] | 328 | 43.29 | 38.04 | -0.651 |
| gap_pct_d2 < 0 | 822 | 43.31 | 39.96 | -0.586 |
| Rule A + gap ∈ [0,2] | 38 | 50.00 | 34.85 | +0.375 |

**结论**：`gap_pct_d2` 无稳健 Alpha。全部跳空分区 holdout 平均收益皆为负；"Rule A + gap∈[0,2]"
holdout mean_net 转正但 n=38 过少。

#### Variant B：09:40 买入（可用 10 分钟特征，但付出推迟成本）

**基线 win% 从 43.82%（09:31）跌至 40.73%（09:40）**，推迟 9 分钟即吃掉 3 个百分点胜率。

| 过滤规则（holdout） | n | win% | Wilson L | mean_net |
|---|---:|---:|---:|---:|
| 基线 (buy 09:40) | 1334 | 40.10 | 37.51 | -0.903 |
| Rule A + early_price_strength > 1% | 80 | 50.00 | 39.30 | -0.199 |
| Rule A + early_pullback > -1% | 66 | 46.97 | 35.43 | -0.194 |

**结论**：所有"看起来强"的早盘特征叠加后，holdout mean_net 仍为负——**推迟入场的滑点成本
超过早盘特征提供的筛选 Alpha**。

---

## 核心判断

1. **当前免费数据源（mootdx + akshare）无法支撑"真·集合竞价"研究**，L2 竞价明细需要付费源
   （Tushare Pro Level2 或 QMT）。
2. **龙虎榜历史在 D1≥3% 候选集上没有显著 Alpha**，无论是机构席位、净买额占比，还是上榜原因。
3. **D2 开盘跳空**本身不构成稳健信号；**推迟入场至 09:40 换取盘后信息**在滑点与胜率下降上不划算。
4. **Rule A（D1 最后 30 分钟量价结构弱）仍是唯一 holdout 稳健的过滤**，但 win% 49.35% 距离目标
   仍有距离。

---

## 下一步选项

- **目标调整**：从"月度≥5% 概率 80%"降为"季度≥5%"，或"月度≥3% 概率 70%"等更现实的指标。
- **数据源升级**：接入 Tushare Pro（付费）获取真实竞价 L2 数据与分笔明细。
- **放弃次日卖出策略**：当前"T+1 持仓 1 日"的回测空间已被 Rule A 探索完，可考虑 T+N 持仓
  策略或引入止盈止损触发器（这会打破"开/开"的 lookahead-free 简洁性，但可能是必要代价）。
- **引入市场情绪过滤**：加入沪深 300 当日趋势、连板高度等市场宽度指标做"大盘禁止交易日"门禁。

本次实验结论不改模拟盘生产主线（first-board-positive-v1），当前策略冻结继续积累 30 笔样本。
