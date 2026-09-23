# 半夏风格 A 股每日观察工具

这是一个面向研究用途的沪深主板短线筛选器。它在每个交易日收盘后读取公开行情，
从首板股票中筛选可能进入二板的观察候选，并为每只股票生成条件化的次日计划。

工具不会登录券商、不会连接交易账户，也不会自动下单。

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

## 自动运行

脚本默认在每周一至周五 16:20 运行，节假日会自动回退到最近有数据的交易日：

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

## 配置

编辑 `config/strategy.json` 可调整：

- 候选最低评分与数量。
- 成交额、换手率和流通市值区间。
- 单一行业候选上限。
- 单票仓位和组合风险敞口上限。
- 次日竞价触发区间及硬止损阈值。

## 统计口径

评分满分约 100：

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
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前版本没有集合竞价和 Level-2 数据；封单金额取通达信收盘五档盘口，无法代表真实排队
成交概率，也不应直接用于自动交易。严谨评价策略仍需要完整交割单，并进行手续费、滑点、
涨跌停无法成交和幸存者偏差处理后的回测。
