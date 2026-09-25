"""Strategy parameters, their editor schema and atomic local persistence."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping


GROUPS = [
    {"id": "selection", "title": "候选筛选", "description": "收盘后筛选首板，按得分和组合约束选出次日候选。"},
    {"id": "scoring", "title": "个股评分", "description": "各项满分相加后扣分；调整权重时请同步检查最低入选分。"},
    {"id": "market", "title": "市场环境", "description": "涨停广度、炸板率和连板高度加权归一为百分制。"},
    {"id": "entry", "title": "竞价与入场", "description": "新计划保存这些规则；竞价重排、放量和回封封稳仍需人工核验。"},
    {"id": "risk", "title": "仓位与退出", "description": "仓位限制参与候选数量计算，成本回撤为人工风险预警。"},
    {"id": "runtime", "title": "运行设置", "description": "各策略独立调度，行情共用连接并按最快配置采集。保存后自动读取；时间均为北京时间。"},
]


def param(default, label, group, low=None, high=None, unit="", scale=1, kind=None, note=""):
    return field(default=default, metadata={
        "label": label, "group": group, "min": low, "max": high,
        "unit": unit, "scale": scale, "kind": kind or (
            "boolean" if isinstance(default, bool) else
            "text" if isinstance(default, str) else "number"
        ), "note": note,
    })


@dataclass(frozen=True)
class StrategyConfig:
    lookback_sessions: int = param(5, "回看交易日", "selection", 1, 60, "日")
    max_candidates: int = param(8, "候选数量上限", "selection", 1, 100, "只")
    max_per_industry: int = param(5, "同题材数量上限", "selection", 1, 100, "只")
    minimum_score: float = param(58.0, "最低入选评分", "selection", 0, 1000, "分")
    minimum_industry_limit_up_count: int = param(2, "同题材涨停数量下限", "selection", 1, 100, "只")
    minimum_amount_cny: float = param(200_000_000.0, "成交额下限", "selection", 0, 1e12, "亿元", 1e8)
    maximum_amount_cny: float = param(3_000_000_000.0, "成交额上限", "selection", 1, 1e12, "亿元", 1e8)
    minimum_turnover_pct: float = param(2.0, "换手率下限", "selection", 0, 100, "%")
    maximum_turnover_pct: float = param(28.0, "换手率上限", "selection", 0, 100, "%")
    minimum_float_market_cap_cny: float = param(1_500_000_000.0, "流通市值下限", "selection", 0, 1e13, "亿元", 1e8)
    maximum_float_market_cap_cny: float = param(30_000_000_000.0, "流通市值上限", "selection", 1, 1e13, "亿元", 1e8)
    maximum_break_count: int = param(1, "历史炸板次数上限", "selection", 0, 30, "次")
    exclude_st: bool = param(True, "排除 ST 与退市股", "selection", kind="fixed")
    main_board_only: bool = param(True, "仅沪深主板首板", "selection", kind="fixed")
    early_weight: float = param(12.0, "首封时间满分", "scoring", 0, 100, "分")
    persistence_weight: float = param(8.0, "封板持续性满分", "scoring", 0, 100, "分")
    seal_weight: float = param(10.0, "封单比例满分", "scoring", 0, 100, "分")
    turnover_weight: float = param(10.0, "换手率满分", "scoring", 0, 100, "分")
    amount_weight: float = param(5.0, "成交额满分", "scoring", 0, 100, "分")
    cap_weight: float = param(5.0, "流通市值满分", "scoring", 0, 100, "分")
    theme_count_weight: float = param(12.0, "题材涨停家数满分", "scoring", 0, 100, "分")
    theme_active_weight: float = param(6.0, "题材活跃天数满分", "scoring", 0, 100, "分")
    theme_momentum_weight: float = param(7.0, "题材增量满分", "scoring", 0, 100, "分")
    leadership_weight: float = param(10.0, "题材顺位满分", "scoring", 0, 100, "分")
    height_weight: float = param(5.0, "题材高度满分", "scoring", 0, 100, "分")
    market_weight: float = param(10.0, "市场环境满分", "scoring", 0, 100, "分")
    early_decay_minutes: float = param(300.0, "首封时间衰减跨度", "scoring", 1, 600, "分钟")
    persistence_decay_minutes: float = param(240.0, "首末封间隔衰减跨度", "scoring", 1, 600, "分钟")
    break_decay_count: float = param(5.0, "炸板持续性归零次数", "scoring", 1, 30, "次")
    seal_ratio_full_score: float = param(0.18, "封单 / 成交额满分线", "scoring", 0.001, 1, "%", 0.01)
    ideal_turnover_min_pct: float = param(5.0, "理想换手率下限", "scoring", 0, 100, "%")
    ideal_turnover_max_pct: float = param(18.0, "理想换手率上限", "scoring", 0, 100, "%")
    ideal_amount_min_cny: float = param(350_000_000.0, "理想成交额下限", "scoring", 0, 1e12, "亿元", 1e8)
    ideal_amount_max_cny: float = param(1_800_000_000.0, "理想成交额上限", "scoring", 0, 1e12, "亿元", 1e8)
    ideal_cap_min_cny: float = param(2_500_000_000.0, "理想流通市值下限", "scoring", 0, 1e13, "亿元", 1e8)
    ideal_cap_max_cny: float = param(15_000_000_000.0, "理想流通市值上限", "scoring", 0, 1e13, "亿元", 1e8)
    theme_count_full_score: int = param(5, "题材涨停家数满分线", "scoring", 1, 100, "只")
    momentum_offset: float = param(1.0, "题材增量平移值", "scoring", 0, 100, "只")
    momentum_span: float = param(4.0, "题材增量满分跨度", "scoring", 0.1, 100, "只")
    second_rank_ratio: float = param(0.7, "第二顺位得分比例", "scoring", 0, 1, "%", 0.01)
    other_rank_ratio: float = param(0.35, "其余顺位得分比例", "scoring", 0, 1, "%", 0.01)
    theme_height_span: int = param(3, "题材高度满分增量", "scoring", 1, 30, "板", note="从一板起计算增量，默认四板及以上满分。")
    late_seal_time: str = param("14:30", "尾盘封板扣分起点", "scoring", kind="time")
    late_seal_penalty: float = param(8.0, "尾盘封板扣分", "scoring", 0, 100, "分")
    single_break_penalty: float = param(3.0, "一次炸板扣分", "scoring", 0, 100, "分")
    missing_seal_amount_penalty: float = param(6.0, "封单数据缺失扣分", "scoring", 0, 100, "分")
    catchup_board_count: int = param(3, "补涨参照连板高度", "scoring", 2, 30, "板")
    expansion_momentum: float = param(0.8, "题材扩张说明阈值", "scoring", 0, 100, "只")
    market_breadth_floor: int = param(15, "涨停广度起点", "market", 0, 1000, "只")
    market_breadth_span: int = param(45, "涨停广度满分增量", "market", 1, 1000, "只")
    market_break_ceiling: float = param(0.55, "炸板率零分线", "market", 0.01, 1, "%", 0.01)
    market_height_span: int = param(5, "市场高度满分增量", "market", 1, 30, "板")
    market_breadth_weight: float = param(45.0, "广度权重", "market", 0, 100, "份")
    market_break_weight: float = param(35.0, "炸板率权重", "market", 0, 100, "份")
    market_height_weight: float = param(20.0, "高度权重", "market", 0, 100, "份")
    missing_break_score: float = param(0.5, "炸板数据缺失替代分", "market", 0, 1, "%", 0.01)
    market_strong_score: float = param(72.0, "强势接力评分线", "market", 0, 100, "分")
    market_neutral_score: float = param(48.0, "中性试错评分线", "market", 0, 100, "分")
    entry_open_min_pct: float = param(0.5, "竞价合格涨幅下限", "entry", -10, 10, "%")
    entry_open_max_pct: float = param(5.0, "竞价合格涨幅上限", "entry", -10, 10, "%")
    reject_open_min_pct: float = param(-2.0, "开盘直接放弃下限", "entry", -10, 10, "%")
    reject_open_max_pct: float = param(7.0, "开盘直接放弃上限", "entry", -10, 10, "%")
    entry_cutoff_time: str = param("10:00", "入场截止时间", "entry", kind="time", note="到达此时刻即停止新增仓位。")
    reject_below_previous_close: bool = param(True, "跌破昨日收盘价即放弃", "entry")
    minimum_sector_sample_size: int = param(2, "板块确认最小样本", "entry", 2, 100, "只")
    minimum_sector_rise_ratio: float = param(0.5, "板块上涨比例确认线", "entry", 0, 1, "%", 0.01)
    near_limit_pct: float = param(1.0, "临近涨停提示距离", "entry", 0, 10, "%")
    quote_max_age_seconds: int = param(180, "行情最大有效年龄", "entry", 1, 600, "秒")
    manual_max_intraday_breaks: int = param(1, "盘中允许炸板次数", "entry", 0, 10, "次", note="人工核验；当前盘口快照不能自动识别完整炸板过程。")
    position_limit_pct: int = param(20, "单票仓位上限", "risk", 1, 100, "%")
    portfolio_risk_limit_pct: int = param(60, "组合仓位上限", "risk", 1, 100, "%")
    hard_stop_pct: float = param(4.0, "成本回撤预警线", "risk", 0.1, 100, "%", note="写入退出计划，需人工核验持仓成本并执行；T+1 约束不变。")
    report_schedule: str = param("16:30,23:30", "工作日报告时间", "runtime", kind="schedule", note="多个时间用英文逗号分隔；非交易日跳过。")
    quote_interval_seconds: float = param(1.0, "交易时段行情间隔", "runtime", 1, 60, "秒")
    bar_interval_seconds: float = param(60.0, "分钟线采集间隔", "runtime", 1, 600, "秒")
    idle_interval_seconds: float = param(60.0, "非交易时段采集间隔", "runtime", 1, 3600, "秒")
    report_timezone: str = param("Asia/Shanghai", "时区", "runtime", kind="fixed")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StrategyConfig":
        if not isinstance(raw, dict):
            raise ConfigError({"config": "参数必须为 JSON 对象"})
        descriptors = {item.name: item for item in fields(cls)}
        errors = {key: "未知参数" for key in raw if key not in descriptors}
        values = asdict(cls())
        values.update(raw)
        for key, item in descriptors.items():
            value, meta = values[key], item.metadata
            default = item.default
            if isinstance(default, bool):
                valid = isinstance(value, bool)
            elif isinstance(default, str):
                valid = isinstance(value, str)
            else:
                valid = (
                    isinstance(value, (float, int)) and not isinstance(value, bool)
                    and math.isfinite(value)
                    and (not isinstance(default, int) or float(value).is_integer())
                    and meta["min"] <= value <= meta["max"]
                )
            if not valid:
                errors[key] = f"{meta['label']}：类型或取值范围不正确"
            elif meta["kind"] == "fixed" and value != default:
                errors[key] = "当前策略的固定约束不可修改"
            elif meta["kind"] in {"time", "schedule"}:
                times = value.split(",") if meta["kind"] == "schedule" else [value]
                if not times or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
                    errors[key] = "请使用 HH:MM 格式，多个时间用英文逗号分隔"
                elif len(times) != len(set(times)):
                    errors[key] = "运行时间不能重复"
        if errors:
            raise ConfigError(errors)
        for sequence in [
            ("minimum_amount_cny", "ideal_amount_min_cny", "ideal_amount_max_cny", "maximum_amount_cny"),
            ("minimum_turnover_pct", "ideal_turnover_min_pct", "ideal_turnover_max_pct", "maximum_turnover_pct"),
            ("minimum_float_market_cap_cny", "ideal_cap_min_cny", "ideal_cap_max_cny", "maximum_float_market_cap_cny"),
            ("reject_open_min_pct", "entry_open_min_pct", "entry_open_max_pct", "reject_open_max_pct"),
            ("position_limit_pct", "portfolio_risk_limit_pct"),
            ("market_neutral_score", "market_strong_score"),
            ("other_rank_ratio", "second_rank_ratio"),
            ("quote_interval_seconds", "idle_interval_seconds"),
        ]:
            if any(values[a] > values[b] for a, b in zip(sequence, sequence[1:])):
                description = " ≤ ".join(descriptors[key].metadata["label"] for key in sequence)
                errors.update({key: description for key in sequence})
        if not "09:30" < values["entry_cutoff_time"] <= "11:30":
            errors["entry_cutoff_time"] = "入场截止时间应晚于09:30且不晚于11:30"
        if not "09:30" <= values["late_seal_time"] <= "15:00":
            errors["late_seal_time"] = "尾盘封板起点应在09:30至15:00之间"
        for keys in [
            ("market_breadth_weight", "market_break_weight", "market_height_weight"),
            tuple(key for key in descriptors if key.endswith("_weight") and key not in {
                "market_breadth_weight", "market_break_weight", "market_height_weight"}),
        ]:
            if sum(values[key] for key in keys) <= 0:
                errors[keys[0]] = "评分权重之和必须大于0"
        if errors:
            raise ConfigError(errors)
        for key, item in descriptors.items():
            if isinstance(item.default, int) and not isinstance(item.default, bool):
                values[key] = int(values[key])
            elif isinstance(item.default, float):
                values[key] = float(values[key])
        return cls(**values)

    @classmethod
    def from_file(cls, path: Path) -> "StrategyConfig":
        return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))

    def entry_rules(self):
        return {
            "open_min_pct": self.entry_open_min_pct, "open_max_pct": self.entry_open_max_pct,
            "reject_min_pct": self.reject_open_min_pct, "reject_max_pct": self.reject_open_max_pct,
            **{key: getattr(self, key) for key in (
                "entry_cutoff_time", "reject_below_previous_close", "minimum_sector_sample_size",
                "minimum_sector_rise_ratio", "near_limit_pct", "quote_max_age_seconds",
                "manual_max_intraday_breaks", "hard_stop_pct",
            )},
        }


class ConfigError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("；".join(dict.fromkeys(errors.values())))


class ConfigConflict(ValueError):
    pass


def revision_for(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def version_for(config, base, commit):
    return f"{base}-{revision_for({'config': config, 'commit': commit})[:16]}"


def stamp_report(report, settings):
    report.code_commit = settings.code_commit
    report.strategy_version = version_for(report.strategy_config, settings.strategy_version, settings.code_commit)


class StrategyConfigStore:
    def __init__(self, path: Path, runtime_defaults=None):
        self.path = Path(path)
        self.runtime_defaults = runtime_defaults or {}

    def read(self):
        raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(raw, dict):
            raise ConfigError({"config": "参数必须为 JSON 对象"})
        return StrategyConfig.from_mapping({**self.runtime_defaults, **raw})

    def payload(self):
        values = asdict(self.read())
        return {
            "config": values, "defaults": asdict(StrategyConfig()),
            "revision": revision_for(values), "groups": GROUPS,
            "fields": [
                {"key": item.name, **dict(item.metadata),
                 "integer": isinstance(item.default, int) and not isinstance(item.default, bool)}
                for item in fields(StrategyConfig)
            ],
        }

    def save(self, raw, revision):
        # Validate before acquiring the lock or creating any file.
        values = asdict(StrategyConfig.from_mapping(raw))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not isinstance(revision, str) or revision != revision_for(asdict(self.read())):
                raise ConfigConflict("参数已在其他页面修改，请重新加载后再保存")
            fd, temporary = tempfile.mkstemp(prefix=".strategy-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(values, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                if self.path.exists():
                    os.chmod(temporary, self.path.stat().st_mode & 0o777)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return self.payload()
