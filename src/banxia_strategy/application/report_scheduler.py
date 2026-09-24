from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_schedule(values: Iterable[str]) -> tuple[time, ...]:
    schedule = set()
    for value in values:
        try:
            parsed = datetime.strptime(value.strip(), "%H:%M").time()
        except ValueError as exc:
            raise ValueError(
                f"invalid report schedule {value!r}; expected HH:MM"
            ) from exc
        schedule.add(parsed)
    if not schedule:
        raise ValueError("report schedule must contain at least one time")
    return tuple(sorted(schedule))


def next_scheduled_at(now: datetime, schedule: Sequence[time]) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    if not schedule:
        raise ValueError("schedule must contain at least one time")

    local_now = now.astimezone(SHANGHAI)
    for day_offset in range(2):
        target_date = local_now.date() + timedelta(days=day_offset)
        for scheduled_time in schedule:
            candidate = datetime.combine(target_date, scheduled_time, SHANGHAI)
            if candidate > local_now:
                return candidate
    raise RuntimeError("unable to calculate the next report schedule")


def recent_scheduled_slots(
    now: datetime,
    schedule: Sequence[time],
    *,
    lookback_days: int = 7,
) -> tuple[datetime, ...]:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    if not schedule:
        raise ValueError("schedule must contain at least one time")
    if lookback_days < 0:
        raise ValueError("lookback_days must not be negative")

    local_now = now.astimezone(SHANGHAI)
    slots = []
    for day_offset in range(lookback_days + 1):
        target_date = local_now.date() - timedelta(days=day_offset)
        due = [
            datetime.combine(target_date, scheduled_time, SHANGHAI)
            for scheduled_time in schedule
            if datetime.combine(target_date, scheduled_time, SHANGHAI)
            <= local_now
        ]
        if due:
            slots.append(max(due))
    return tuple(slots)


def report_is_fresh(output_dir: Path, scheduled_at: datetime) -> bool:
    report_dir = output_dir / scheduled_at.date().isoformat()
    report_path = report_dir / "candidates.json"
    completion_path = report_dir / ".report-complete.json"
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
        if payload.get("as_of") != scheduled_at.date().isoformat():
            return False
        if completion.get("as_of") != payload.get("as_of"):
            return False
        if completion.get("generated_at") != payload.get("generated_at"):
            return False
        if not completion.get("run_id"):
            return False
        generated_at = datetime.fromisoformat(str(payload["generated_at"]))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if generated_at.tzinfo is None:
        return False
    return generated_at.astimezone(SHANGHAI) >= scheduled_at
