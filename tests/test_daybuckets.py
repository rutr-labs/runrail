"""Calendar days belong to the person reading them, not to UTC.

A run at 9pm in Toronto is 1am UTC the next day. Bucketed in UTC it landed on
tomorrow's heatmap square, and clicking that square listed it under tomorrow
too — internally consistent and disagreeing with everyone looking at it.

Three surfaces answer "what happened on this day" and they are drawn on top of
each other, so the tests that matter are the ones checking they agree: a square
must open a list containing exactly the runs it counted.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from runrail.daybuckets import day_bounds, offset_segments, resolve_zone
from runrail.db import SessionLocal
from runrail.models import RunStatus, TriggerType, WorkflowRun

TORONTO = ZoneInfo("America/Toronto")
KOLKATA = ZoneInfo("Asia/Kolkata")          # +5:30, never changes
UTC = timezone.utc


def _seed(workflow_id: int, instants) -> None:
    with SessionLocal() as db:
        for at in instants:
            db.add(WorkflowRun(
                workflow_id=workflow_id, status=RunStatus.success,
                trigger_type=TriggerType.manual, created_at=at, started_at=at,
                finished_at=at + timedelta(seconds=1), duration_seconds=1.0))
        db.commit()


def _counts(client, tz: str | None, days: int = 366) -> dict[str, int]:
    query = f"/api/stats/daily?days={days}" + (f"&tz={tz}" if tz else "")
    response = client.get(query)
    assert response.status_code == 200, response.text
    return {row["date"]: row["success"] for row in response.json() if row["success"]}


def test_an_evening_run_belongs_to_the_evening_not_the_next_utc_day(client):
    workflow = client.post("/api/workflows", json={"name": "evening"}).json()
    # 9pm on a Thursday in Toronto; 1am Friday in UTC.
    at = datetime(2026, 8, 20, 21, 0, tzinfo=TORONTO).astimezone(UTC)
    _seed(workflow["id"], [at])

    assert _counts(client, None) == {"2026-08-21": 1}, "UTC bucketing is unchanged"
    assert _counts(client, "America/Toronto") == {"2026-08-20": 1}
    # Ahead of UTC the same instant is already the next morning.
    assert _counts(client, "Asia/Kolkata") == {"2026-08-21": 1}


def test_a_square_opens_exactly_the_runs_it_counted(client):
    """The bug this replaces was self-consistent; the failure to avoid is a
    square that counts runs its own filter cannot find."""
    workflow = client.post("/api/workflows", json={"name": "agree"}).json()
    _seed(workflow["id"], [
        datetime(2026, 7, 3, hour, 30, tzinfo=TORONTO).astimezone(UTC)
        for hour in (0, 9, 21, 23)
    ])
    for zone in ("America/Toronto", "Asia/Kolkata", None):
        counted = _counts(client, zone)
        for day, total in counted.items():
            query = f"/api/runs?day={day}&limit=500" + (f"&tz={zone}" if zone else "")
            listed = client.get(query).json()
            assert len(listed) == total, f"{zone} {day}: counted {total}, listed {len(listed)}"


def test_days_either_side_of_a_dst_jump_keep_their_own_runs(client):
    """8 March 2026 is 23 hours long in Toronto. A single fixed offset across
    the window would file the runs after the jump under the wrong date."""
    workflow = client.post("/api/workflows", json={"name": "dst"}).json()
    _seed(workflow["id"], [
        datetime(2026, 3, 7, 23, 30, tzinfo=TORONTO).astimezone(UTC),   # before
        datetime(2026, 3, 8, 0, 30, tzinfo=TORONTO).astimezone(UTC),    # pre-jump
        datetime(2026, 3, 8, 23, 30, tzinfo=TORONTO).astimezone(UTC),   # post-jump
        datetime(2026, 3, 9, 0, 30, tzinfo=TORONTO).astimezone(UTC),    # after
    ])
    # Those four instants span only two UTC dates, and three local ones.
    assert _counts(client, "America/Toronto") == {
        "2026-03-07": 1, "2026-03-08": 2, "2026-03-09": 1,
    }


def test_an_unknown_zone_is_refused_rather_than_answered_in_utc(client):
    """Silently answering in UTC would reintroduce the mismatch invisibly."""
    assert client.get("/api/stats/daily?tz=Mars/Olympus").status_code == 422
    assert client.get("/api/runs?day=2026-08-20&tz=Mars/Olympus").status_code == 422


def test_offset_segments_splits_at_the_transition_instant():
    segments = offset_segments(
        datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 11, 8, tzinfo=UTC), TORONTO)
    assert [offset for *_, offset in segments] == [-240, -300]
    # 2am EDT on 1 November, the moment the clocks go back.
    assert segments[0][1] == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert segments[0][1] == segments[1][0], "segments must not leave a gap"


def test_offset_segments_is_a_single_span_without_a_transition():
    for zone in (KOLKATA, ZoneInfo("UTC")):
        segments = offset_segments(
            datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC), zone)
        assert len(segments) == 1, f"{zone} has no DST and needs one query"


@pytest.mark.parametrize("day,zone,hours", [
    # A spring-forward day is 23 hours, a fall-back day 25. Neither is
    # midnight + 24h, which is why both edges resolve through the zone.
    (datetime(2026, 3, 8).date(), TORONTO, 23),
    (datetime(2026, 11, 1).date(), TORONTO, 25),
    (datetime(2026, 6, 1).date(), TORONTO, 24),
    (datetime(2026, 6, 1).date(), KOLKATA, 24),
])
def test_day_bounds_measure_the_real_length_of_a_local_day(day, zone, hours):
    start, end = day_bounds(day, zone)
    assert (end - start) == timedelta(hours=hours)


def test_resolve_zone_defaults_to_utc_when_unset():
    assert resolve_zone(None) == ZoneInfo("UTC")
    assert resolve_zone("") == ZoneInfo("UTC")
    with pytest.raises(ValueError):
        resolve_zone("Nowhere/Special")
