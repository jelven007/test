from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class StrategyConfig:
    lookback_sessions: int = 5
    max_candidates: int = 8
    max_per_industry: int = 2
    minimum_score: float = 58.0
    minimum_amount_cny: float = 200_000_000
    maximum_amount_cny: float = 3_000_000_000
    minimum_turnover_pct: float = 2.0
    maximum_turnover_pct: float = 28.0
    minimum_float_market_cap_cny: float = 1_500_000_000
    maximum_float_market_cap_cny: float = 30_000_000_000
    maximum_break_count: int = 3
    exclude_st: bool = True
    main_board_only: bool = True
    position_limit_pct: int = 20
    portfolio_risk_limit_pct: int = 60
    hard_stop_pct: float = 4.0
    entry_open_min_pct: float = 0.5
    entry_open_max_pct: float = 5.0
    report_timezone: str = "Asia/Shanghai"

    @classmethod
    def from_file(cls, path: Path) -> "StrategyConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError("Unknown config keys: " + ", ".join(unknown))
        return cls(**raw)


class MarketDataProvider(Protocol):
    def trading_dates(self) -> Sequence[date]:
        ...

    def limit_up_pool(self, session: date) -> List[Dict[str, Any]]:
        ...

    def broken_board_pool(self, session: date) -> List[Dict[str, Any]]:
        ...


@dataclass
class Candidate:
    rank: int
    code: str
    name: str
    industry: str
    score: float
    strategy: str
    latest_price: float
    amount_cny: float
    turnover_pct: float
    float_market_cap_cny: float
    first_seal_time: str
    last_seal_time: str
    break_count: int
    seal_amount_ratio: float
    industry_limit_up_count: int
    industry_active_days: int
    industry_max_board: int
    reasons: List[str]
    entry_trigger: str
    invalidation: str
    exit_plan: str
    position_limit_pct: int


@dataclass
class DailyReport:
    as_of: str
    next_session: Optional[str]
    generated_at: str
    data_source: str
    data_sessions: List[str]
    market: Dict[str, Any]
    candidates: List[Candidate]
    rejected_count: int
    disclaimer: str

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        return result


COLUMN_ALIASES: Dict[str, Sequence[str]] = {
    "code": ("代码", "股票代码"),
    "name": ("名称", "股票简称"),
    "latest_price": ("最新价", "收盘价"),
    "amount": ("成交额",),
    "float_market_cap": ("流通市值",),
    "turnover": ("换手率",),
    "seal_amount": ("封板资金", "封单资金"),
    "first_seal_time": ("首次封板时间",),
    "last_seal_time": ("最后封板时间",),
    "break_count": ("炸板次数", "开板次数"),
    "board_count": ("连板数",),
    "industry": ("所属行业", "行业"),
}


def _first(row: Dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier = 100_000_000
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return default


def _integer(value: Any, default: int = 0) -> int:
    return int(round(_number(value, float(default))))


def _time_minutes(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().replace(".0", "")
    digits = re.sub(r"\D", "", text)
    if len(digits) < 4:
        return None
    if len(digits) == 4:
        hour, minute = int(digits[:2]), int(digits[2:4])
    else:
        digits = digits.zfill(6)
        hour, minute = int(digits[:2]), int(digits[2:4])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _format_time(value: Any) -> str:
    minutes = _time_minutes(value)
    if minutes is None:
        return "-"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _normalize(row: Dict[str, Any]) -> Dict[str, Any]:
    data = {
        key: _first(row, aliases)
        for key, aliases in COLUMN_ALIASES.items()
    }
    data.update(
        code=str(data["code"] or "").zfill(6),
        name=str(data["name"] or "").strip(),
        industry=str(data["industry"] or "其他").strip() or "其他",
        latest_price=_number(data["latest_price"]),
        amount=_number(data["amount"]),
        float_market_cap=_number(data["float_market_cap"]),
        turnover=_number(data["turnover"]),
        seal_amount=_number(data["seal_amount"]),
        first_seal_minutes=_time_minutes(data["first_seal_time"]),
        last_seal_minutes=_time_minutes(data["last_seal_time"]),
        break_count=_integer(data["break_count"]),
        board_count=max(1, _integer(data["board_count"], 1)),
    )
    return data


def _is_main_board(code: str) -> bool:
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _range_score(value: float, minimum: float, ideal_low: float, ideal_high: float, maximum: float) -> float:
    if value < minimum or value > maximum:
        return 0.0
    if ideal_low <= value <= ideal_high:
        return 1.0
    if value < ideal_low:
        return _clamp((value - minimum) / max(ideal_low - minimum, 1e-9))
    return _clamp((maximum - value) / max(maximum - ideal_high, 1e-9))


class StrategyEngine:
    def __init__(self, provider: MarketDataProvider, config: StrategyConfig):
        self.provider = provider
        self.config = config

    def run(self, requested_date: Optional[date] = None) -> DailyReport:
        requested_date = requested_date or date.today()
        all_sessions = sorted(set(self.provider.trading_dates()))
        calendar = [item for item in all_sessions if item <= requested_date]
        if not calendar:
            raise RuntimeError(f"No trading session found on or before {requested_date}")

        pools: List[tuple[date, List[Dict[str, Any]]]] = []
        for session in reversed(calendar[-max(20, self.config.lookback_sessions * 3):]):
            try:
                rows = self.provider.limit_up_pool(session)
            except Exception:
                continue
            if rows:
                pools.append((session, [_normalize(row) for row in rows]))
            if len(pools) >= self.config.lookback_sessions:
                break
        if not pools:
            raise RuntimeError("The market data source returned no limit-up pool data")
        pools.reverse()

        as_of, today_rows = pools[-1]
        broken_data_available = True
        try:
            broken_rows = self.provider.broken_board_pool(as_of)
        except Exception:
            broken_rows = []
            broken_data_available = False

        market = self._market_state(today_rows, broken_rows, broken_data_available)
        industry_stats = self._industry_stats(pools)
        raw_candidates = self._build_candidates(today_rows, industry_stats, market)
        selected = self._select(raw_candidates)
        for index, candidate in enumerate(selected, start=1):
            candidate.rank = index

        return DailyReport(
            as_of=as_of.isoformat(),
            next_session=next(
                (session.isoformat() for session in all_sessions if session > as_of),
                None,
            ),
            generated_at=datetime.now(
                ZoneInfo(self.config.report_timezone)
            ).isoformat(timespec="seconds"),
            data_source=str(getattr(self.provider, "source_name", "custom")),
            data_sessions=[session.isoformat() for session, _ in pools],
            market=market,
            candidates=selected,
            rejected_count=max(
                0,
                sum(1 for row in today_rows if row["board_count"] == 1) - len(selected),
            ),
            disclaimer=(
                "本报告仅用于量化研究和次日观察，不构成投资建议。所有入场均为条件触发，"
                "不得自动下单；涨停接力可能出现无法成交、炸板和隔日跌停。"
            ),
        )

    def _market_state(
        self,
        limit_rows: List[Dict[str, Any]],
        broken_rows: List[Dict[str, Any]],
        broken_data_available: bool,
    ) -> Dict[str, Any]:
        limit_count = len(limit_rows)
        broken_count = len(broken_rows)
        denominator = limit_count + broken_count
        break_rate = broken_count / denominator if denominator and broken_data_available else None
        max_board = max((row["board_count"] for row in limit_rows), default=0)

        breadth_score = _clamp((limit_count - 15) / 45)
        break_score = 1.0 - _clamp(break_rate / 0.55) if break_rate is not None else 0.5
        height_score = _clamp((max_board - 1) / 5)
        score = 0.45 * breadth_score + 0.35 * break_score + 0.20 * height_score

        if score >= 0.72:
            regime = "强势接力"
        elif score >= 0.48:
            regime = "中性试错"
        else:
            regime = "退潮防守"
        return {
            "regime": regime,
            "score": round(score * 100, 1),
            "limit_up_count": limit_count,
            "broken_board_count": broken_count,
            "broken_board_data_available": broken_data_available,
            "break_rate_pct": round(break_rate * 100, 1) if break_rate is not None else None,
            "max_board": max_board,
        }

    @staticmethod
    def _industry_stats(
        pools: Sequence[tuple[date, List[Dict[str, Any]]]]
    ) -> Dict[str, Dict[str, Any]]:
        per_day: List[Dict[str, Dict[str, int]]] = []
        for _, rows in pools:
            daily: Dict[str, Dict[str, int]] = {}
            for row in rows:
                item = daily.setdefault(row["industry"], {"count": 0, "max_board": 0})
                item["count"] += 1
                item["max_board"] = max(item["max_board"], row["board_count"])
            per_day.append(daily)

        industries = {key for daily in per_day for key in daily}
        result: Dict[str, Dict[str, Any]] = {}
        for industry in industries:
            counts = [daily.get(industry, {}).get("count", 0) for daily in per_day]
            max_boards = [daily.get(industry, {}).get("max_board", 0) for daily in per_day]
            previous = counts[:-1]
            previous_average = sum(previous) / len(previous) if previous else 0.0
            result[industry] = {
                "today_count": counts[-1],
                "previous_count": counts[-2] if len(counts) > 1 else 0,
                "active_days": sum(1 for count in counts if count > 0),
                "momentum": counts[-1] - previous_average,
                "max_board": max_boards[-1],
            }
        return result

    def _passes_filters(self, row: Dict[str, Any]) -> bool:
        cfg = self.config
        if row["board_count"] != 1:
            return False
        if not row["code"] or not row["name"]:
            return False
        if cfg.exclude_st and ("ST" in row["name"].upper() or "退" in row["name"]):
            return False
        if cfg.main_board_only and not _is_main_board(row["code"]):
            return False
        if not cfg.minimum_amount_cny <= row["amount"] <= cfg.maximum_amount_cny:
            return False
        if not cfg.minimum_turnover_pct <= row["turnover"] <= cfg.maximum_turnover_pct:
            return False
        if not (
            cfg.minimum_float_market_cap_cny
            <= row["float_market_cap"]
            <= cfg.maximum_float_market_cap_cny
        ):
            return False
        if row["break_count"] > cfg.maximum_break_count:
            return False
        return True

    def _build_candidates(
        self,
        rows: List[Dict[str, Any]],
        industry_stats: Dict[str, Dict[str, Any]],
        market: Dict[str, Any],
    ) -> List[Candidate]:
        eligible = [row for row in rows if self._passes_filters(row)]
        industry_order: Dict[str, List[str]] = {}
        for industry in {row["industry"] for row in eligible}:
            members = [row for row in eligible if row["industry"] == industry]
            members.sort(
                key=lambda row: (
                    row["first_seal_minutes"] or 24 * 60,
                    -(row["seal_amount"] / max(row["amount"], 1)),
                )
            )
            industry_order[industry] = [row["code"] for row in members]

        result: List[Candidate] = []
        for row in eligible:
            stats = industry_stats.get(row["industry"], {})
            first_minutes = row["first_seal_minutes"] or 15 * 60
            last_minutes = row["last_seal_minutes"] or first_minutes
            first_after_open = max(0, first_minutes - (9 * 60 + 30))
            early_score = _clamp(1.0 - first_after_open / 300)
            reseal_delay = max(0, last_minutes - first_minutes)
            persistence_score = _clamp(1.0 - reseal_delay / 240)
            persistence_score *= _clamp(1.0 - row["break_count"] / 5)
            seal_ratio = row["seal_amount"] / max(row["amount"], 1)
            seal_score = _clamp(seal_ratio / 0.18)
            board_quality = 12 * early_score + 8 * persistence_score + 10 * seal_score

            turnover_score = _range_score(
                row["turnover"],
                self.config.minimum_turnover_pct,
                5.0,
                18.0,
                self.config.maximum_turnover_pct,
            )
            amount_score = _range_score(
                row["amount"],
                self.config.minimum_amount_cny,
                350_000_000,
                1_800_000_000,
                self.config.maximum_amount_cny,
            )
            cap_score = _range_score(
                row["float_market_cap"],
                self.config.minimum_float_market_cap_cny,
                2_500_000_000,
                15_000_000_000,
                self.config.maximum_float_market_cap_cny,
            )
            liquidity = 10 * turnover_score + 5 * amount_score + 5 * cap_score

            today_count = int(stats.get("today_count", 1))
            active_days = int(stats.get("active_days", 1))
            momentum = float(stats.get("momentum", 0.0))
            theme = (
                12 * _clamp(today_count / 5)
                + 6 * _clamp(active_days / max(self.config.lookback_sessions, 1))
                + 7 * _clamp((momentum + 1) / 4)
            )

            order = industry_order.get(row["industry"], [row["code"]])
            local_rank = order.index(row["code"]) + 1
            rank_score = 1.0 if local_rank == 1 else 0.7 if local_rank == 2 else 0.35
            max_board = int(stats.get("max_board", 1))
            leadership = 10 * rank_score + 5 * _clamp((max_board - 1) / 3)
            market_component = float(market["score"]) / 10

            penalty = 0.0
            if first_minutes >= 14 * 60 + 30:
                penalty += 8
            if row["break_count"] >= 2:
                penalty += 4
            score = round(
                board_quality + liquidity + theme + leadership + market_component - penalty,
                1,
            )

            if max_board >= 3:
                strategy = "龙头补涨"
            elif today_count >= 2 and int(stats.get("previous_count", 0)) == 0:
                strategy = "新题材切换"
            else:
                strategy = "一进二试错"

            reasons = [
                f"{row['industry']}涨停{today_count}只，近{self.config.lookback_sessions}日活跃{active_days}日",
                f"首封{_format_time(row['first_seal_time'])}，炸板{row['break_count']}次",
                f"封单/成交额{seal_ratio:.1%}，换手{row['turnover']:.1f}%",
            ]
            if max_board >= 3:
                reasons.append(f"板块已有{max_board}板高度，具备补涨参照")
            if momentum > 0.8:
                reasons.append("板块涨停家数较近期均值扩张")

            entry_trigger = (
                f"次日竞价涨幅位于{self.config.entry_open_min_pct:.1f}%～"
                f"{self.config.entry_open_max_pct:.1f}%，板块仍有前排助攻；"
                "仅在10:00前放量封二板或首次炸板后快速回封时观察"
            )
            invalidation = (
                f"竞价低于-2%或高于{self.config.entry_open_max_pct + 2:.1f}%、"
                "板块无助攻、开盘快速跌破昨日收盘价、回封超过两次则放弃"
            )
            exit_plan = (
                f"单票不超过{self.config.position_limit_pct}%；成本回撤"
                f"{self.config.hard_stop_pct:.1f}%触发风险预警；"
                "A股T+1，当日新买仓位不能当日卖出，最早下一交易日按可成交情况退出；"
                "次日无溢价或板块退潮优先退出，跳空和跌停可能导致损失超过预警阈值"
            )
            result.append(
                Candidate(
                    rank=0,
                    code=row["code"],
                    name=row["name"],
                    industry=row["industry"],
                    score=score,
                    strategy=strategy,
                    latest_price=row["latest_price"],
                    amount_cny=row["amount"],
                    turnover_pct=row["turnover"],
                    float_market_cap_cny=row["float_market_cap"],
                    first_seal_time=_format_time(row["first_seal_time"]),
                    last_seal_time=_format_time(row["last_seal_time"]),
                    break_count=row["break_count"],
                    seal_amount_ratio=round(seal_ratio, 4),
                    industry_limit_up_count=today_count,
                    industry_active_days=active_days,
                    industry_max_board=max_board,
                    reasons=reasons,
                    entry_trigger=entry_trigger,
                    invalidation=invalidation,
                    exit_plan=exit_plan,
                    position_limit_pct=self.config.position_limit_pct,
                )
            )
        return result

    def _select(self, candidates: List[Candidate]) -> List[Candidate]:
        candidates.sort(key=lambda item: (-item.score, item.first_seal_time, item.code))
        selected: List[Candidate] = []
        industry_counts: Dict[str, int] = {}
        total_position = 0
        for candidate in candidates:
            if candidate.score < self.config.minimum_score:
                continue
            if industry_counts.get(candidate.industry, 0) >= self.config.max_per_industry:
                continue
            if total_position + candidate.position_limit_pct > self.config.portfolio_risk_limit_pct:
                break
            selected.append(candidate)
            industry_counts[candidate.industry] = industry_counts.get(candidate.industry, 0) + 1
            total_position += candidate.position_limit_pct
            if len(selected) >= self.config.max_candidates:
                break
        return selected


def write_report(report: DailyReport, output_root: Path) -> Dict[str, Path]:
    output_dir = output_root / report.as_of
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "candidates.json"
    csv_path = output_dir / "candidates.csv"
    markdown_path = output_dir / "report.md"

    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rows = []
    for candidate in report.candidates:
        row = asdict(candidate)
        row["reasons"] = "；".join(candidate.reasons)
        rows.append(row)
    fieldnames = [item.name for item in fields(Candidate)]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "markdown": markdown_path}


def _render_markdown(report: DailyReport) -> str:
    market = report.market
    lines = [
        f"# 半夏风格次日观察报告 - {report.as_of}",
        "",
        f"> {report.disclaimer}",
        "",
        "## 市场状态",
        "",
        f"- 计划交易日：{report.next_session or '待交易日历更新'}",
        f"- 环境：**{market['regime']}**（{market['score']} / 100）",
        f"- 涨停：{market['limit_up_count']} 只",
        (
            f"- 炸板：{market['broken_board_count']} 只，炸板率 {market['break_rate_pct']}%"
            if market["broken_board_data_available"]
            else "- 炸板：数据源不可用，市场评分已按中性值降级"
        ),
        f"- 连板高度：{market['max_board']} 板",
        f"- 数据来源：{report.data_source}",
        f"- 数据交易日：{', '.join(report.data_sessions)}",
        "",
        "## 次日候选",
        "",
    ]
    if not report.candidates:
        lines.extend(
            [
                "没有标的同时满足评分和风控条件。策略结论：空仓观察。",
                "",
            ]
        )
    for candidate in report.candidates:
        lines.extend(
            [
                f"### {candidate.rank}. {candidate.name}（{candidate.code}）",
                "",
                f"- 评分：**{candidate.score}**；策略：**{candidate.strategy}**；行业：{candidate.industry}",
                f"- 首封/末封：{candidate.first_seal_time}/{candidate.last_seal_time}；"
                f"炸板 {candidate.break_count} 次；换手 {candidate.turnover_pct:.1f}%",
                f"- 逻辑：{'；'.join(candidate.reasons)}",
                f"- 触发：{candidate.entry_trigger}",
                f"- 放弃：{candidate.invalidation}",
                f"- 风控：{candidate.exit_plan}",
                "",
            ]
        )
    lines.extend(
        [
            "## 组合约束",
            "",
            f"- 候选数量：{len(report.candidates)}",
            f"- 单票仓位上限：{report.candidates[0].position_limit_pct if report.candidates else '-'}%",
            "- 未触发入场条件的候选不应买入。",
            "- 同一题材最多选择两只，优先评分更高且先封板的标的。",
            "",
        ]
    )
    return "\n".join(lines)
