"""Scheduled runs that never happened, computed on read.

A run that did not happen has no row, and it must never be given one: a
placeholder WorkflowRun would corrupt success rates, 24-hour counts, average
durations and the activity heatmap, and it would be wrong the moment someone
edits the crontab. So the gaps are derived on every request — from the
workflow's own CronTrigger, the runs that do exist, and the two spans where no
fire was ever owed.

The cost of computing rather than recording, stated plainly: only the CURRENT
schedule exists, so editing the crontab re-renders yesterday under the new one.
A table of ghosts would go stale in exactly the same way, only silently.

Every dimension is bounded, and `complete`/`stopped_by` name the bound that
fired: "* * * * *" expects 1440 fires a day, and a UI that quietly showed the
first 2000 of a month's 43200 would be claiming a clean history it never looked
at.
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from runrail.crontab import cron_trigger
from runrail.models import TriggerType, Workflow, WorkflowRun, _aware, now

#: How far from its expected instant a scheduled run may land and still answer
#: for that fire. Asymmetric because APScheduler dispatches at or after the fire
#: and never before: LATE is misfire_grace_time (55s, see SchedulerService.sync)
#: plus room for the job's own commit, while EARLY absorbs nothing but clock
#: jitter — created_at is stamped by the process that fired the job. Matching
#: also clips each window to the next fire's, so no run can answer for two.
MATCH_EARLY = timedelta(seconds=5)
MATCH_LATE = timedelta(seconds=90)

#: Safety valve on the one query. A window holding N fires holds roughly N
#: scheduled runs, so this only bites when a workflow's schedule used to be far
#: denser than it is now — reported, never silently dropped.
MAX_RUN_ROWS = 20000

DEFAULT_DAYS = 14
DEFAULT_FIRES = 2000
MAX_FIRES = 10000
DEFAULT_MISSED = 200


def _trigger(workflow: Workflow) -> CronTrigger | None:
    """The workflow's own trigger, from the one builder scheduler/service.py
    also uses.

    A second cron implementation here would disagree with the scheduler about a
    day of the week or a DST boundary and paint gaps for fires that correctly
    never happened; the tests pin the first fire of this module to the
    watchdog's own.
    """
    if not workflow.schedule_cron:
        return None
    try:
        return cron_trigger(workflow.schedule_cron, workflow.schedule_timezone or "UTC")
    except (ValueError, KeyError):
        return None  # a crontab sync() also rejects: skipped, never raised


def _walk(trigger: CronTrigger, since: datetime, until: datetime,
          cap: int) -> tuple[list[datetime], bool]:
    """Fires in [since, until], and whether the cap cut the walk short.

    Passing the previous fire back in is APScheduler's own iteration contract;
    it is what makes a repeated fall-back hour come out twice, as the scheduler
    would really have fired it.
    """
    fires: list[datetime] = []
    fire = trigger.get_next_fire_time(None, since)
    while fire is not None and fire <= until:
        if len(fires) >= cap:
            return fires, True
        fires.append(fire)
        fire = trigger.get_next_fire_time(fire, fire)
    return fires, False


def _fires(trigger: CronTrigger, since: datetime, until: datetime,
           cap: int) -> tuple[list[datetime], bool]:
    """The expected fires in the window, keeping the NEWEST when the cap bites.

    Truncating the old end is the only useful direction — a run list opens on
    the most recent gap — but CronTrigger walks forwards only. So an overflowing
    first pass pays for itself: it measures the mean interval, and the second
    pass re-anchors the floor to the last `cap` fires of the window.
    """
    fires, overflowed = _walk(trigger, since, until, cap)
    if not overflowed or len(fires) < 2:
        return fires, overflowed
    interval = (fires[-1] - fires[0]) / (len(fires) - 1)
    # One fire of slack, then the tail: the estimated floor can land a hair
    # either side of a boundary, and the newest fire is the one that must survive.
    fires, _ = _walk(trigger, max(since, until - interval * cap), until, cap + 1)
    return fires[-cap:], True


def _runs(db: Session, workflow: Workflow, since: datetime,
          current: datetime) -> tuple[list[datetime], list[tuple[datetime, datetime]], bool]:
    """One query, two uses: which runs can answer a fire, and when the workflow
    was blocked from taking one.

    enqueue_scheduled drops a fire outright while a queued run is waiting, so a
    fire inside a run's QUEUED span — [created_at, started_at), which is why an
    ancient still-queued run has to be fetched however old it is — was
    coalesced by design and is not a miss. A running run does not block:
    enqueue_scheduled only counts queued ones.
    """
    queued_end = func.coalesce(WorkflowRun.started_at, WorkflowRun.finished_at)
    rows = db.execute(
        select(WorkflowRun.created_at, WorkflowRun.started_at, WorkflowRun.finished_at,
               WorkflowRun.trigger_type)
        .where(WorkflowRun.workflow_id == workflow.id,
               WorkflowRun.created_at <= current,
               or_(WorkflowRun.created_at >= since - MATCH_EARLY,
                   queued_end.is_(None), queued_end >= since))
        .order_by(WorkflowRun.created_at).limit(MAX_RUN_ROWS + 1)).all()
    scheduled: list[datetime] = []
    spans: list[tuple[datetime, datetime]] = []
    for row in rows[:MAX_RUN_ROWS]:
        created = _aware(row.created_at)
        # Only a scheduled run answers a fire: a manual or CLI run is somebody
        # pressing the button, and it says nothing about whether cron fired.
        if row.trigger_type == TriggerType.schedule:
            scheduled.append(created)
        end = _aware(row.started_at) or _aware(row.finished_at) or current
        if end > created:
            spans.append((created, end))
    return scheduled, spans, len(rows) > MAX_RUN_ROWS


def _match(fires: list[datetime], scheduled: list[datetime]) -> list[bool]:
    """Which fires a scheduled run actually landed on.

    Greedy and one-to-one over two ascending lists: each run answers at most one
    fire, so a single run cannot make a whole outage disappear.
    """
    matched = [False] * len(fires)
    index = 0
    for position, fire in enumerate(fires):
        limit = fire + MATCH_LATE
        if position + 1 < len(fires):
            limit = min(limit, fires[position + 1] - MATCH_EARLY)
        while index < len(scheduled) and scheduled[index] < fire - MATCH_EARLY:
            index += 1  # too old for this fire, and every later fire is later still
        if index < len(scheduled) and scheduled[index] < limit:
            matched[position] = True
            index += 1
    return matched


def _paused_spans(workflow: Workflow, since: datetime, until: datetime) -> list[dict]:
    """The stretches where no fire was owed, reconstructed from what the row knows.

    Neither `enabled` nor a snooze keeps history; updated_at is the only record
    of when the operator last touched the workflow, and that is when they paused
    or muted it unless they have edited something since. Same one-column trade
    check_missed_runs makes for its anchor, and it fails in the safe direction:
    a later edit shortens the paused span, so a real miss is never hidden.
    """
    edited = _aware(workflow.updated_at) or since
    spans = []
    if not workflow.enabled:
        spans.append(("disabled", max(edited, since), until))
    snooze_until = _aware(workflow.snooze_until)
    # Not workflow.snoozed: an expired pause is exactly the "muted it for a
    # month" case whose history still has to read as paused rather than dead.
    if workflow.snooze_pauses_runs and snooze_until:
        spans.append(("snoozed", max(edited, since), min(snooze_until, until)))
    return [{"reason": reason, "since": start, "until": end}
            for reason, start, end in spans if end > start]


def _bucket(day: str) -> dict:
    return {"date": day, "expected": 0, "ran": 0, "missed": 0, "blocked": 0, "paused": 0}


def find_gaps(db: Session, workflow: Workflow, *, days: int = DEFAULT_DAYS,
              max_fires: int = DEFAULT_FIRES, limit: int = DEFAULT_MISSED) -> dict:
    """Expected fires in the window and what became of each one."""
    current = now()
    # A fire younger than the match tolerance is not history yet — misfire grace
    # means its run is still allowed to arrive — so the window stops short of it.
    until = current - MATCH_LATE
    requested = until - timedelta(days=days)
    # Fires from before the workflow existed were never owed to anyone.
    since = max(requested, _aware(workflow.created_at) or requested)

    trigger = _trigger(workflow)
    fires, truncated = _fires(trigger, since, until, max_fires) if trigger else ([], False)
    scheduled, spans, row_capped = _runs(db, workflow, since, current)
    matched = _match(fires, scheduled)
    paused = _paused_spans(workflow, since, until)
    if truncated and fires:
        since = fires[0]  # the cap moved the floor; the response has to say so

    daily: dict[str, dict] = {}
    totals = {"expected": 0, "ran": 0, "missed": 0, "blocked": 0, "paused": 0}
    missed: list[datetime] = []
    index, open_until = 0, None
    for position, fire in enumerate(fires):
        # Sweep, not a scan per fire: both lists ascend, so the widest queued
        # span opened so far is all a fire needs to know about.
        while index < len(spans) and spans[index][0] <= fire:
            open_until = max(open_until or spans[index][1], spans[index][1])
            index += 1
        # Evidence beats reconstruction: a run that exists proves the workflow
        # was live, whatever updated_at implies about a paused span.
        if matched[position]:
            state = "ran"
        elif any(span["since"] <= fire <= span["until"] for span in paused):
            state = "paused"
        elif open_until is not None and open_until > fire:
            state = "blocked"
        else:
            state = "missed"
        # UTC, like every other timestamp the API returns and like the day
        # buckets in /stats/daily — a missed cell has to land on the same column
        # of the same heatmap as the runs beside it.
        stamp = fire.astimezone(timezone.utc)
        key = stamp.date().isoformat()
        day = daily.setdefault(key, _bucket(key))
        day["expected"] += 1
        day[state] += 1
        totals["expected"] += 1
        totals[state] += 1
        if state == "missed":
            missed.append(stamp)

    # One reason, most significant first: a caller that has to widen the window
    # cannot act on the row valve until the fire cap stops biting.
    if trigger is None and workflow.schedule_cron:
        stopped_by = "invalid_cron"  # sync() skips it too, so nothing has fired
    elif truncated:
        stopped_by = "max_fires"
    elif row_capped:
        stopped_by = "run_rows"
    else:
        stopped_by = None
    # Newest first, like the run list these rows are merged into.
    shown = missed[-limit:][::-1]
    return {
        "workflow_id": workflow.id,
        "schedule_cron": workflow.schedule_cron,
        "schedule_timezone": workflow.schedule_timezone or "UTC",
        # `since` is what was actually examined; it moves when the workflow is
        # younger than the window or the cap bit.
        "window": {"since": since, "until": until, "requested_since": requested},
        "totals": totals,
        "daily": [daily[key] for key in sorted(daily)],
        "missed": [{"expected_at": stamp, "date": stamp.date().isoformat()} for stamp in shown],
        "missed_shown": len(shown),
        "paused_spans": paused,
        "complete": stopped_by is None,
        "stopped_by": stopped_by,
    }
