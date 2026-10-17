"""Weekly windows: when a scheduled service should be running.

A window is a set of weekdays and a wall-clock span in the service's zone:
`{"days": ["mon", "tue"], "start": "08:00", "end": "17:00"}`. The days name
where the window STARTS, so `22:00 → 06:00` on Friday runs into Saturday
morning. An end at or before the start crosses midnight; an end equal to the
start is the whole 24 hours. A service's windows are unioned, so overlapping
or touching windows behave as one.

Wall times are resolved in the zone with zoneinfo, so DST is exact: on the
day clocks go forward a window starting inside the gap opens at the first
instant that exists, and on the day they go back a window spanning the repeat
lasts an hour longer — it is the wall clock that is promised, not a duration.

The UI's week editor mirrors this module (frontend/src/serviceWindows.ts);
tests on both sides pin the same cases.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: How far ahead a next change is looked for. Every window repeats weekly, so
#: eight days always finds one if any window exists.
_HORIZON_DAYS = 8


def parse_time(value: str) -> time:
    """`HH:MM`, 00:00 to 23:59. Raises ValueError otherwise."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError(f"Time must be HH:MM, got {value!r}")
    hours, minutes = value[:2], value[3:]
    if not (hours.isdigit() and minutes.isdigit()) or int(hours) > 23 or int(minutes) > 59:
        raise ValueError(f"Time must be HH:MM between 00:00 and 23:59, got {value!r}")
    return time(int(hours), int(minutes))


def validate_windows(windows: object) -> list[dict]:
    """The windows, normalised (days in week order, deduplicated), or
    ValueError naming what is wrong. Used by the API and by YAML apply."""
    if not isinstance(windows, list):
        raise ValueError("Windows must be a list")
    cleaned = []
    for index, window in enumerate(windows, start=1):
        if not isinstance(window, dict):
            raise ValueError(f"Window {index} must be an object with days, start and end")
        days = window.get("days")
        if not isinstance(days, list) or not days:
            raise ValueError(f"Window {index} needs at least one day")
        unknown = [day for day in days if day not in DAYS]
        if unknown:
            raise ValueError(f"Window {index} has unknown days: {', '.join(map(str, unknown))} "
                             f"(use {', '.join(DAYS)})")
        try:
            start, end = parse_time(window.get("start")), parse_time(window.get("end"))
        except ValueError as exc:
            raise ValueError(f"Window {index}: {exc}") from None
        cleaned.append({"days": [day for day in DAYS if day in days],
                        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")})
    return cleaned


def _local(day: date, at: time, zone: ZoneInfo) -> datetime:
    """A wall time on a date, as a UTC instant. Inside a spring-forward gap the
    wall time does not exist; fold=0 resolves it with the offset before the
    jump, which lands on the far side of the gap (02:30 → 03:30)."""
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)


def intervals(windows: Sequence[dict], zone: ZoneInfo, first: date,
              days: int) -> list[tuple[datetime, datetime]]:
    """Every window occurrence starting on [first, first + days), as merged,
    sorted UTC intervals."""
    spans = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        weekday = DAYS[day.weekday()]
        for window in windows:
            if weekday not in window["days"]:
                continue
            start, end = parse_time(window["start"]), parse_time(window["end"])
            end_day = day if end > start else day + timedelta(days=1)
            spans.append((_local(day, start, zone), _local(end_day, end, zone)))
    spans.sort()
    merged: list[list[datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _around(windows: Sequence[dict], zone: ZoneInfo,
            instant: datetime) -> list[tuple[datetime, datetime]]:
    # From the day before: yesterday's overnight window may still be open.
    today = instant.astimezone(zone).date()
    return intervals(windows, zone, today - timedelta(days=1), _HORIZON_DAYS + 1)


def is_active(windows: Sequence[dict], zone_name: str | None, instant: datetime) -> bool:
    zone = ZoneInfo(zone_name or "UTC")
    return any(start <= instant < end for start, end in _around(windows, zone, instant))


def next_change(windows: Sequence[dict], zone_name: str | None,
                instant: datetime) -> datetime | None:
    """The next instant the service's schedule flips — the end of the window it
    is in, or the start of the next one. None when there are no windows, or
    when they cover the whole week without a break."""
    zone = ZoneInfo(zone_name or "UTC")
    spans = _around(windows, zone, instant)
    for start, end in spans:
        if start <= instant < end:
            # A window that runs to the edge of the horizon may be part of an
            # unbroken week: no change is coming.
            return None if end >= spans[-1][1] and _covers_week(spans) else end
        if start > instant:
            return start
    return None


def _covers_week(spans: Iterable[tuple[datetime, datetime]]) -> bool:
    spans = list(spans)
    return len(spans) == 1 and spans[0][1] - spans[0][0] >= timedelta(days=7)
