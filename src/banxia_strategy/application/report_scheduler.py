from __future__ import annotations

from datetime import datetime, time, timedelta
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
