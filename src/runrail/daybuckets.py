"""Calendar days in the viewer's timezone.

Everything RunRail stores is UTC, which is right for storage and wrong for the
question "what happened on Tuesday?". A run at 9pm in Toronto is 1am UTC the
next day, so a UTC-bucketed heatmap put it on Wednesday's square, and clicking
that square listed it under Wednesday too — self-consistent, and disagreeing
with the person reading it.

Three surfaces answer that question and they must agree exactly, or a cell
would show a count the filtered list beneath it could not reproduce:
/stats/daily (the squares), /workflows/{id}/schedule-gaps (the missed-run marks
drawn on the same squares) and /runs?day= (what a square opens).

The offset is constant between DST transitions, so a local calendar date is
`date(instant + offset)` within a segment that contains no transition. Windows
are cut at their transitions and each piece is grouped in SQL with its own
offset: exact for every zone and every DST rule, while keeping the aggregation
in the database. Bucketing 60k rows in Python instead measured 202ms against
57ms for the SQL path, on a database this app is expected to hold.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func

UTC = timezone.utc

#: Coarse probe for transition hunting. Every real zone changes offset far less
#: often than this, and the bisect below finds the exact second regardless.
_PROBE = timedelta(hours=6)


def resolve_zone(name: str | None) -> ZoneInfo:
    """The viewer's IANA zone, or UTC when unset.

    An unknown name raises: the browser sends its own resolved zone, so a value
    the server cannot load is a bug worth surfacing rather than silently
    answering in UTC — which is exactly the quiet mismatch this module exists
    to remove.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone '{name}'") from exc


def day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """The UTC half-open interval covering one local calendar day.

    Not `midnight + 24h`: a spring-forward day is 23 hours long and a fall-back
    day is 25, so both edges are resolved through the zone independently.
    """
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _offset_minutes(instant: datetime, zone: ZoneInfo) -> int:
    offset = instant.astimezone(zone).utcoffset() or timedelta()
    return round(offset.total_seconds() / 60)


def offset_segments(
    since: datetime, until: datetime, zone: ZoneInfo,
) -> list[tuple[datetime, datetime, int]]:
    """Split [since, until) into spans whose UTC offset never changes.

    Returned as (start, end, offset_minutes). One span for a zone without DST,
    or for a window that happens to miss the transition; two or three otherwise.
    """
    if until <= since:
        return []
    segments: list[tuple[datetime, datetime, int]] = []
    span_start = since
    span_offset = _offset_minutes(since, zone)

    probe = since
    while probe < until:
        nxt = min(probe + _PROBE, until)
        offset = _offset_minutes(nxt, zone)
        if offset != span_offset:
            # Bisect to the second so a run either side of the transition is
            # grouped with the offset that was actually in force for it.
            low, high = probe, nxt
            while (high - low) > timedelta(seconds=1):
                mid = low + (high - low) / 2
                if _offset_minutes(mid, zone) == span_offset:
                    low = mid
                else:
                    high = mid
            segments.append((span_start, high, span_offset))
            span_start, span_offset = high, offset
        probe = nxt
    segments.append((span_start, until, span_offset))
    return segments


def local_date_expr(column, offset_minutes: int, dialect: str):
    """SQL for the local calendar date of `column`, given a constant offset.

    Both backends can shift a timestamp and truncate it, in their own spelling.
    The offset is an int this module computed from zoneinfo — never client
    text — and both forms bind it as a parameter.
    """
    if offset_minutes == 0:
        return func.date(column)
    if dialect == "sqlite":
        # date(ts, '+240 minutes') — SQLite's own modifier syntax.
        return func.date(column, f"{offset_minutes:+d} minutes")
    # PostgreSQL: make_interval(years, months, weeks, days, hours, mins, secs).
    # date() truncates in the session timezone, which db pins to UTC.
    return func.date(column + func.make_interval(0, 0, 0, 0, 0, offset_minutes))


def local_day_key(instant: datetime, zone: ZoneInfo) -> str:
    """The YYYY-MM-DD a UTC instant belongs to, in `zone`."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(zone).date().isoformat()
