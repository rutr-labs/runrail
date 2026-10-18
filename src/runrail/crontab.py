"""One crontab dialect, one parser, for every part of the product.

APScheduler's day_of_week field counts 0=Mon..6=Sun. Standard crontab — the
schedule builder, the UI's labels, and every operator's mental model — counts
0=Sun..6=Sat, and spells Sunday 7 as well as 0. Handing it a raw expression
therefore fires a weekly schedule one day late and rejects `7` outright, so the
day-of-week field's digits are rewritten to APScheduler's day NAMES, which mean
the same thing in both dialects, before the trigger is built.

Validation goes through the same trigger. A second validator would eventually
accept an expression the scheduler then skips, which is the silent
never-runs-again this module exists to prevent.
"""

from datetime import timedelta
from datetime import timezone as _tz

from apscheduler.triggers.cron import CronTrigger

#: APScheduler's own day names, indexed by the STANDARD-cron digit.
_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

#: crontab field order, for naming the field an invalid expression tripped on.
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


def _days(element: str) -> list[int] | None:
    """The standard-cron day numbers one comma-separated element covers, or None
    when the element is not this module's question.

    None never means "invalid": day names already mean the same thing in both
    dialects, and anything malformed is passed through untouched so APScheduler
    raises the single error message the whole product reports.
    """
    body, slash, step = element.partition("/")
    stride = int(step) if step.isdigit() else 0
    if slash and stride < 1:
        return None
    first, dash, last = body.partition("-")
    if body == "*":
        low, high = 0, 6
    elif first.isdigit() and (last.isdigit() if dash else not last):
        low = int(first)
        # A bare `N/step` counts from N to the end of the week — the same shape,
        # and the same reading, APScheduler gives it.
        high = int(last) if dash else (6 if slash else low)
        if low > high or high > 7:
            return None
    else:
        return None
    # 7 is a second spelling of Sunday, which APScheduler's 0-6 field has no room
    # for at all — the reason `0 9 * * 7` used to skip the workflow entirely.
    return [day % 7 for day in range(low, high + 1, stride or 1)]


def _day_of_week(field: str) -> str:
    """The field with every numeric element rewritten to APScheduler day names."""
    if field == "*":
        return field
    translated = []
    for element in field.split(","):
        days = _days(element)
        translated.append(element if days is None else
                          ",".join(_DAY_NAMES[day] for day in dict.fromkeys(days)))
    return ",".join(translated)


class _OnceAtWallClockTrigger(CronTrigger):
    """A cron trigger that fires once at a repeated fall-back wall time.

    On the night clocks go back, 01:30 happens twice, and APScheduler fires at
    both instants — so a job set for "daily at 01:30" ran twice that night, an
    hour apart (the two fires land in different UTC minutes, so the per-minute
    run key never caught it). The schedule builder and its preview have always
    promised the earlier instant only; this makes that true. Used only for
    schedules pinned to particular hours: an hourly job owes a run in each of
    the two 01:00 hours, which really are two hours.
    """

    def get_next_fire_time(self, previous_fire_time, now):
        fire = super().get_next_fire_time(previous_fire_time, now)
        while fire is not None and _is_second_instance(fire):
            # In UTC: arithmetic on a zone-aware datetime is wall-clock and drops
            # `fold`, so `fire + 1µs` would land an hour BEFORE the second
            # instance and find it again, forever.
            after = fire.astimezone(_tz.utc) + timedelta(microseconds=1)
            fire = super().get_next_fire_time(after, after)
        return fire


def _is_second_instance(moment) -> bool:
    """True when this instant is the later of two that share its wall time."""
    first = moment.replace(tzinfo=None).replace(tzinfo=moment.tzinfo, fold=0)
    return first.astimezone(_tz.utc) != moment.astimezone(_tz.utc)


def _pins_hours(field: str) -> bool:
    return all(part.replace("-", "").isdigit() for part in field.split(","))


def cron_trigger(expr: str, timezone: str = "UTC") -> CronTrigger:
    """The trigger for a standard-crontab expression, in the given IANA zone.

    Raises whatever APScheduler raises for an expression it cannot run, so the
    scheduler, the gap report and the API accept exactly the same set.
    """
    fields = expr.split()
    if len(fields) == len(_FIELD_NAMES):
        fields[-1] = _day_of_week(fields[-1])
    pinned = len(fields) == len(_FIELD_NAMES) and _pins_hours(fields[1])
    trigger_class = _OnceAtWallClockTrigger if pinned else CronTrigger
    return trigger_class.from_crontab(" ".join(fields), timezone=timezone)


def _explain(expr: str, error: Exception) -> str:
    """The failure, attributed to the field that on its own reproduces it.

    APScheduler names the offending sub-expression but never the column it sat
    in, and "the last value (24) is higher than the maximum value (23)" is only
    actionable once the operator knows which of five columns to look at. The
    probe's own message is reported, not the original: with two bad fields the
    two disagree about which limit was exceeded.
    """
    fields = expr.split()
    if len(fields) == len(_FIELD_NAMES):
        for index, name in enumerate(_FIELD_NAMES):
            probe = ["*"] * len(_FIELD_NAMES)
            probe[index] = fields[index]
            try:
                cron_trigger(" ".join(probe))
            except (ValueError, KeyError) as exc:
                return f" ({name} field): {exc}"
    return f": {error}"


def validate_cron(expr: str) -> str:
    """The expression, trimmed, or ValueError naming the field that is wrong."""
    expr = expr.strip()
    try:
        cron_trigger(expr)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid cron expression{_explain(expr, exc)}") from exc
    fields = expr.split()
    if len(fields) == len(_FIELD_NAMES) and fields[2] != "*" and fields[4] != "*":
        # Standard cron runs `0 9 1 * 1` on the 1st AND every Monday; the
        # scheduler requires both at once, so it ran once or twice a year. Only
        # new expressions are refused: a stored one keeps loading and running as
        # it always has.
        raise ValueError("Invalid cron expression: restrict day-of-month or day-of-week, "
                         "not both (use two workflows for both)")
    return expr
