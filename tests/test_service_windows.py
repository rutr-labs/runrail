"""Weekly windows for scheduled services. frontend/src/serviceWindows.test.ts
pins the same cases for the week editor, so the two cannot drift."""

from datetime import UTC, datetime

import pytest

from runrail.services.windows import is_active, next_change, validate_windows

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_a_weekday_daytime_window():
    windows = [{"days": WEEKDAYS, "start": "08:00", "end": "17:00"}]
    # Mon 2026-10-19, UTC zone.
    assert not is_active(windows, "UTC", at("2026-10-19T07:59"))
    assert is_active(windows, "UTC", at("2026-10-19T08:00"))
    assert not is_active(windows, "UTC", at("2026-10-19T17:00")), "the end is exclusive"
    assert next_change(windows, "UTC", at("2026-10-19T12:00")) == at("2026-10-19T17:00")
    # Friday evening: next start is Monday morning.
    assert next_change(windows, "UTC", at("2026-10-23T18:00")) == at("2026-10-26T08:00")
    assert not is_active(windows, "UTC", at("2026-10-24T10:00")), "Saturday"


def test_an_overnight_window_belongs_to_the_day_it_starts():
    windows = [{"days": ["fri"], "start": "22:00", "end": "06:00"}]
    assert is_active(windows, "UTC", at("2026-10-23T23:00"))  # Friday night
    assert is_active(windows, "UTC", at("2026-10-24T05:59"))  # Saturday morning
    assert not is_active(windows, "UTC", at("2026-10-24T22:30")), "not Saturday night"
    assert next_change(windows, "UTC", at("2026-10-24T01:00")) == at("2026-10-24T06:00")


def test_overlapping_and_touching_windows_act_as_one():
    windows = [{"days": ["mon"], "start": "08:00", "end": "12:00"},
               {"days": ["mon"], "start": "12:00", "end": "13:00"},
               {"days": ["mon"], "start": "10:00", "end": "11:00"}]
    assert next_change(windows, "UTC", at("2026-10-19T09:00")) == at("2026-10-19T13:00"), \
        "touching windows must not stop the service at noon"


def test_start_equal_to_end_is_the_whole_day_and_a_full_week_never_changes():
    every_day = [{"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                  "start": "00:00", "end": "00:00"}]
    assert is_active(every_day, "UTC", at("2026-10-21T03:00"))
    assert next_change(every_day, "UTC", at("2026-10-21T03:00")) is None


def test_windows_follow_the_wall_clock_across_dst():
    windows = [{"days": WEEKDAYS, "start": "08:00", "end": "17:00"}]
    # New York: 08:00 EDT is 12:00Z in October, 08:00 EST is 13:00Z in November.
    assert next_change(windows, "America/New_York", at("2026-10-30T22:00")) is not None
    assert is_active(windows, "America/New_York", at("2026-10-30T12:00"))
    assert not is_active(windows, "America/New_York", at("2026-11-02T12:30"))
    assert is_active(windows, "America/New_York", at("2026-11-02T13:00"))


def test_a_night_window_over_the_autumn_change_lasts_the_extra_hour():
    """22:00 → 06:00 on the night clocks go back is nine hours, not eight."""
    windows = [{"days": ["sat"], "start": "22:00", "end": "06:00"}]
    opened = next_change(windows, "America/New_York", at("2026-10-31T20:00"))
    closed = next_change(windows, "America/New_York", opened)
    assert (closed - opened).total_seconds() == 9 * 3600


def test_no_windows_means_never_active():
    assert not is_active([], "UTC", at("2026-10-19T12:00"))
    assert next_change([], "UTC", at("2026-10-19T12:00")) is None


@pytest.mark.parametrize("bad", [
    [{"days": [], "start": "08:00", "end": "17:00"}],
    [{"days": ["monday"], "start": "08:00", "end": "17:00"}],
    [{"days": ["mon"], "start": "8:00", "end": "17:00"}],
    [{"days": ["mon"], "start": "08:00", "end": "24:00"}],
    "mon 08:00-17:00",
])
def test_invalid_windows_are_refused(bad):
    with pytest.raises(ValueError):
        validate_windows(bad)


def test_valid_windows_are_normalised():
    assert validate_windows([{"days": ["fri", "mon", "mon"], "start": "08:00", "end": "17:30"}]) \
        == [{"days": ["mon", "fri"], "start": "08:00", "end": "17:30"}]


def test_a_window_starting_inside_the_spring_gap_opens_when_the_clocks_jump():
    """Los Angeles, 8 March 2026: 02:00 PST becomes 03:00 PDT (10:00Z). A
    02:30 start has no instant of its own; it opens at the jump, not an hour
    after it. A window lying entirely inside the gap never happens that day."""
    windows = [{"days": ["sun"], "start": "02:30", "end": "04:00"}]
    assert is_active(windows, "America/Los_Angeles", at("2026-03-08T10:00"))
    assert next_change(windows, "America/Los_Angeles", at("2026-03-08T09:00")) == at("2026-03-08T10:00")
    swallowed = [{"days": ["sun"], "start": "02:00", "end": "03:00"}]
    assert not is_active(swallowed, "America/Los_Angeles", at("2026-03-08T10:00"))
    # Next Sunday, 02:00 PDT is 09:00Z.
    assert next_change(swallowed, "America/Los_Angeles", at("2026-03-08T09:00")) == at("2026-03-15T09:00")
