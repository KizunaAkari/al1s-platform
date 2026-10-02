from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from al1s.execution.errors import InvalidRequestError
from al1s.execution.scheduling_types import TimedScheduleSpec


def timed_instants(spec: TimedScheduleSpec, *, maximum: int) -> list[datetime]:
    if spec.end_date < spec.start_date:
        raise InvalidRequestError(
            "invalid_timed_date_range", "Timed task end date must not precede start date"
        )
    if not spec.daily_times:
        raise InvalidRequestError(
            "missing_timed_daily_times", "Timed task requires at least one daily time"
        )
    try:
        zone = ZoneInfo(spec.timezone)
    except ZoneInfoNotFoundError as exc:
        raise InvalidRequestError(
            "invalid_task_timezone", "Timed task timezone is not a valid IANA timezone"
        ) from exc
    instants: list[datetime] = []
    day: date = spec.start_date
    while day <= spec.end_date:
        for local_time in sorted(set(spec.daily_times)):
            naive = datetime.combine(day, local_time)
            candidates = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
            valid = [
                candidate
                for candidate in candidates
                if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
            ]
            offsets = {candidate.utcoffset() for candidate in valid}
            if not valid or len(offsets) > 1:
                raise InvalidRequestError(
                    "ambiguous_timed_local_time",
                    "Timed task contains an ambiguous or nonexistent local time",
                )
            instants.append(valid[0].astimezone(UTC))
        day += timedelta(days=1)
        if len(instants) > maximum:
            break
    return sorted(set(instants))
