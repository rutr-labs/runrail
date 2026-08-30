#!/usr/bin/env python
"""Fill a RunRail home with a realistic large history: demo data, and the
reproducible input behind any performance claim about this app.

Run it against a THROWAWAY home — ``--home`` is required and it refuses the
default one, so it cannot be aimed at a real install by omission:

    python scripts/seed_demo.py --home /tmp/rr-demo --reset
    RUNRAIL_HOME=/tmp/rr-demo runrail serve

What "realistic" has to mean for this to be worth anything: runs land on their
workflow's own crontab (so /schedule-gaps sees the fires the app expects),
failures arrive in streaks rather than uniformly (so the activity feed's
transition rule and auto-pause both fire), outages remove whole stretches of
fires (so there are gaps to find), and log files exist for the newest slice
only — which is what a home looks like once retention has run.

Every number is a flag; the defaults produce roughly 50k runs over 90 days.
"""

import argparse
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ is None:  # runnable straight from a source checkout, uninstalled
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

#: Schedules, and how many workflows carry each. Top-heavy in cheap schedules
#: and light in dense ones, because that is the shape of a real install: a fleet
#: of */5 workflows would produce a run count nobody's history matches.
SCHEDULES: list[tuple[str | None, str | None, int]] = [
    ("*/15 * * * *", None, 2),
    ("*/30 * * * *", "Europe/London", 3),
    ("0 * * * *", None, 8),
    ("15 * * * *", "Asia/Dubai", 3),
    ("0 */4 * * *", None, 6),
    ("30 6 * * *", "America/New_York", 7),
    ("0 2 * * *", None, 5),
    ("0 5 * * 1", None, 4),
    (None, None, 5),  # manual-only workflows: no schedule, still plenty of runs
]

DOMAINS = ["billing", "crm", "inventory", "marketing", "payroll", "pricing", "risk",
           "sales", "shipping", "support", "telemetry", "warehouse"]
SUBJECTS = ["daily_rollup", "sync", "ingest", "reconcile", "export", "refresh",
            "digest", "snapshot", "audit", "reindex"]
TASK_NAMES = ["extract", "transform", "validate", "load", "publish"]

LOG_LINES = [
    "INFO  starting {task} for ds={ds}",
    "INFO  connected to warehouse in {ms}ms",
    "DEBUG fetched {rows} rows",
    "INFO  wrote {rows} rows to staging",
    "WARN  retrying chunk {n} after a transient timeout",
    "INFO  checkpoint {n} committed",
    "INFO  {task} finished in {ms}ms",
]
ERROR_LINES = [
    "ERROR connection reset by peer",
    "Traceback (most recent call last):",
    '  File "/opt/pipelines/{task}.py", line {n}, in main',
    "OperationalError: could not connect to server: connection refused",
    "ERROR {task} failed after {ms}ms",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", required=True, type=Path,
                        help="RUNRAIL_HOME to seed. Required, and never the default home.")
    parser.add_argument("--db-url", help="Override RUNRAIL_DB_URL (e.g. postgresql+psycopg://…)")
    parser.add_argument("--reset", action="store_true", help="Delete --home before seeding")
    parser.add_argument("--days", type=int, default=90, help="How far back history reaches")
    parser.add_argument("--workflows", type=int, default=sum(count for *_, count in SCHEDULES),
                        help="Workflow count; schedules are dealt out in the SCHEDULES ratio")
    parser.add_argument("--max-fires", type=int, default=20000,
                        help="Safety valve on a dense crontab over a long window")
    parser.add_argument("--log-files", type=int, default=6000,
                        help="Task runs whose log files are written, newest first. Older rows keep "
                             "their paths — that is a pruned home, not a bug.")
    parser.add_argument("--log-lines", type=int, default=60, help="Lines per stdout log")
    parser.add_argument("--failure-rate", type=float, default=0.07)
    parser.add_argument("--seed", type=int, default=7, help="Same seed, same database")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def guard_home(home: Path) -> Path:
    """Refuse the real install. This script is destructive by design, and the
    one mistake it must make impossible is running against live data.

    Both the platform default and whatever RUNRAIL_HOME/.env already point at,
    because on a machine that has configured one it is the second that holds the
    real database.
    """
    from runrail.config import Settings, _default_home

    resolved = home.expanduser().resolve()
    live = {_default_home().resolve(), Settings().home.expanduser().resolve()}
    if resolved in live:
        raise SystemExit(f"Refusing to seed a live RunRail home ({resolved}); "
                         "pass a scratch path.")
    return resolved


def open_home(home: Path, db_url: str | None, reset: bool):
    """Point the process at `home` and migrate it. Settings are read once at
    import and the engine is built from them, so both have to be rebuilt here."""
    if reset and home.exists():
        shutil.rmtree(home)
    os.environ["RUNRAIL_HOME"] = str(home)
    if db_url:
        os.environ["RUNRAIL_DB_URL"] = db_url
    from runrail.config import get_settings

    get_settings.cache_clear()
    import runrail.db as database

    database.engine.dispose()
    database.engine = database._make_engine()
    database.SessionLocal.configure(bind=database.engine)
    database.init_db()
    get_settings().ensure_directories()
    return database


def workflow_specs(args: argparse.Namespace, rng: random.Random) -> list[dict]:
    """Workflow rows plus the traits the run generator needs.

    Traits sit alongside the row rather than in the database: they are how this
    script decides what happened, not something RunRail stores.
    """
    schedules = [(cron, tz) for cron, tz, count in SCHEDULES for _ in range(count)]
    names = [f"{domain}_{subject}" for domain in DOMAINS for subject in SUBJECTS]
    rng.shuffle(names)
    specs = []
    for index in range(args.workflows):
        cron, timezone_name = schedules[index % len(schedules)]
        specs.append({
            "name": names[index] if index < len(names) else f"pipeline_{index}",
            "schedule_cron": cron,
            "schedule_timezone": timezone_name,
            "task_names": TASK_NAMES[:rng.randint(2, len(TASK_NAMES))],
            "base_seconds": rng.choice((3.0, 12.0, 45.0, 180.0, 600.0)),
            "failure_rate": args.failure_rate * rng.choice((0.2, 0.5, 1.0, 1.0, 2.5)),
            # The feature flags the new endpoints branch on, on a minority of
            # workflows each: volume alone leaves both sides of every branch
            # unexercised.
            "sla_minutes": rng.choice((None, None, None, 15, 60)),
            "missed_run_grace_minutes": rng.choice((None, None, 30, 120)),
            "auto_pause_failures": rng.choice((None, None, None, 3)),
            "lock_resource": rng.choice((None, None, None, "warehouse", "licence")),
            "gated": index % 11 == 0,
        })
    return specs


def cron_fires(cron: str, timezone_name: str | None, since: datetime, until: datetime,
               cap: int) -> list[datetime]:
    """The schedule's fires in the window, from APScheduler itself.

    A second cron implementation here would disagree with schedule_gaps about a
    DST boundary and seed history the app then reports as broken.
    """
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(cron, timezone=timezone_name or "UTC")
    fires: list[datetime] = []
    fire = trigger.get_next_fire_time(None, since)
    while fire is not None and fire <= until and len(fires) < cap:
        fires.append(fire.astimezone(timezone.utc))
        fire = trigger.get_next_fire_time(fire, fire)
    return fires


def outage_spans(rng: random.Random, since: datetime,
                 until: datetime) -> list[tuple[datetime, datetime]]:
    """Stretches where the workflow produced nothing at all — the thing
    /schedule-gaps exists to find. Without them every fire has a run and that
    endpoint is only ever measured on its happy path."""
    window = (until - since).total_seconds()
    spans = []
    for _ in range(rng.randint(0, 3)):
        start = since + timedelta(seconds=rng.random() * window)
        spans.append((start, start + timedelta(hours=rng.choice((2, 6, 14, 30)))))
    return spans


def plan_runs(args: argparse.Namespace, rng: random.Random, specs: list[dict]) -> list[dict]:
    """One dict per run: the row to insert plus the shape of its task runs.

    Held in memory on purpose — 50k of these is tens of megabytes, and one
    executemany per table is what makes this finish in a minute rather than an
    hour.
    """
    from runrail.models import RunStatus, TriggerType

    current = datetime.now(timezone.utc)
    since = current - timedelta(days=args.days)
    plans: list[dict] = []
    for spec in specs:
        fires = (cron_fires(spec["schedule_cron"], spec["schedule_timezone"], since, current,
                            args.max_fires) if spec["schedule_cron"] else [])
        outages = outage_spans(rng, since, current)
        # Manual runs regardless of schedule: someone always reruns things by
        # hand, and the manual-only workflows would otherwise have no history.
        manual = [since + timedelta(seconds=rng.random() * (current - since).total_seconds())
                  for _ in range(rng.randint(3, 40) if fires else rng.randint(40, 200))]
        streak = 0
        for created, trigger in sorted([(fire, TriggerType.schedule) for fire in fires]
                                       + [(at, TriggerType.manual) for at in manual]):
            if any(start <= created <= end for start, end in outages):
                continue  # the run that never happened: no row, by definition
            # Failures cluster: once something is broken it usually stays broken
            # for a few fires. A uniform sprinkle would make every failure a
            # transition and turn the activity feed into a wall of red.
            failing = rng.random() < (0.55 if streak else spec["failure_rate"])
            streak = streak + 1 if failing else 0
            status = RunStatus.failed if failing else RunStatus.success
            if not failing and rng.random() < 0.02:
                status = RunStatus.cancelled
            duration = spec["base_seconds"] * rng.lognormvariate(0, 0.35)
            started = created + timedelta(seconds=rng.random() * 3)
            plans.append({
                "spec": spec, "status": status, "started": started, "duration": duration,
                "row": {
                    "workflow_id": spec["id"], "status": status, "trigger_type": trigger,
                    "run_key": None, "parameters_json": {"ds": created.date().isoformat()},
                    "started_at": started, "finished_at": started + timedelta(seconds=duration),
                    "duration_seconds": round(duration, 3), "created_at": created,
                    "resume_count": 1 if failing and rng.random() < 0.05 else 0,
                    # The scheduler stamps this once per run; a run that overran
                    # its workflow's promise is rare and must read that way.
                    "sla_breached_at": started + timedelta(minutes=spec["sla_minutes"])
                    if spec["sla_minutes"] and duration > spec["sla_minutes"] * 54 else None,
                },
            })
    plans.extend(_live_plans(rng, specs, current))
    # Chronological, because ids are inserted in list order and in a real
    # install a higher id means a later run. Several endpoints lean on that —
    # task-durations ranks by TaskRun.id, logsearch orders by created_at and
    # reads files by id — so per-workflow insertion order would flatter or
    # punish them for a reason no production database shares.
    plans.sort(key=lambda plan: plan["row"]["created_at"])
    return plans


def _live_plans(rng: random.Random, specs: list[dict], current: datetime) -> list[dict]:
    """The tail of unfinished runs the dashboard and wallboard are built around.
    Appended last so they are the newest rows whatever order the workflows came
    in, and the approval gate is put on a workflow that actually has one."""
    from runrail.models import RunStatus, TriggerType

    gated = [spec for spec in specs if spec["gated"]]
    live = [(spec, RunStatus.running) for spec in rng.sample(specs, min(4, len(specs)))]
    live += [(spec, RunStatus.queued) for spec in rng.sample(specs, min(3, len(specs)))]
    live += [(spec, RunStatus.waiting_approval) for spec in rng.sample(gated, min(2, len(gated)))]
    plans = []
    for spec, status in live:
        created = current - timedelta(minutes=rng.randint(1, 20))
        plans.append({
            "spec": spec, "status": status, "started": created, "duration": 0.0,
            "row": {
                "workflow_id": spec["id"], "status": status,
                "trigger_type": TriggerType.schedule, "run_key": None,
                "parameters_json": {"ds": created.date().isoformat()},
                "started_at": None if status == RunStatus.queued else created,
                "finished_at": None, "duration_seconds": None, "created_at": created,
                "resume_count": 0, "sla_breached_at": None,
            },
        })
    return plans


def plan_task_runs(rng: random.Random, plans: list[dict], run_ids: list[int],
                   task_ids: dict[tuple[int, str], int]) -> list[dict]:
    """The task rows each planned run leaves behind."""
    from runrail.models import RunStatus, TaskRunStatus

    rows: list[dict] = []
    for run_id, plan in zip(run_ids, plans, strict=True):
        spec, status = plan["spec"], plan["status"]
        if status == RunStatus.queued:
            continue  # nothing has picked it up yet, so there is nothing to show
        names = spec["task_names"]
        # A failed or cancelled run stops at the task that ended it; everything
        # after never started and has no row, exactly as the worker leaves it.
        stops_at = (rng.randrange(len(names))
                    if status in (RunStatus.failed, RunStatus.cancelled) else len(names) - 1)
        share = plan["duration"] / len(names)
        at = plan["started"]
        for position, name in enumerate(names[:stops_at + 1]):
            last = position == stops_at
            if status == RunStatus.waiting_approval and last:
                # A gate is its own attempt-0 row with no logs and no duration.
                rows.append(_task_row(run_id, task_ids[(spec["id"], name)],
                                      TaskRunStatus.awaiting_approval, at, None))
                break
            state = TaskRunStatus.success
            if last and status == RunStatus.failed:
                state = TaskRunStatus.failed
            elif last and status == RunStatus.cancelled:
                state = TaskRunStatus.cancelled
            elif last and status == RunStatus.running:
                state = TaskRunStatus.running
            seconds = share * rng.lognormvariate(0, 0.3)
            rows.append(_task_row(run_id, task_ids[(spec["id"], name)], state, at, seconds))
            at += timedelta(seconds=seconds)
    return rows


def _task_row(run_id: int, task_id: int, status, started: datetime,
              seconds: float | None) -> dict:
    from runrail.models import TaskRunStatus

    settled = seconds is not None and status in (
        TaskRunStatus.success, TaskRunStatus.failed, TaskRunStatus.cancelled)
    return {
        "workflow_run_id": run_id, "task_id": task_id, "status": status, "attempt": 1,
        "started_at": started,
        "finished_at": started + timedelta(seconds=seconds) if settled else None,
        "duration_seconds": round(seconds, 3) if settled else None,
        "exit_code": 1 if status == TaskRunStatus.failed else 0 if settled else None,
        "error_message": "Task exited with code 1" if status == TaskRunStatus.failed else None,
        "rendered_command": "python /opt/pipelines/job.py", "created_at": started,
        "resume_index": 0, "approval_note": None, "approved_at": None,
        # Filled by one UPDATE once the ids these paths are built from exist.
        "stdout_log_path": None, "stderr_log_path": None,
    }


def log_path(logs_root: Path, run_id: int, task_run_id: int, stream: str) -> Path:
    """The worker's own layout, in one place — logsearch confines itself to
    logs_dir and retention deletes by run directory, so a seeded path that
    disagreed with either would be invisible to both."""
    return logs_root / f"run_{run_id}" / f"task_run_{task_run_id}.{stream}.log"


def insert_rows(db, model, rows: list[dict], chunk: int = 5000) -> None:
    from sqlalchemy import insert

    for start in range(0, len(rows), chunk):
        db.execute(insert(model), rows[start:start + chunk])
    db.commit()


def write_logs(db, logs_root: Path, rng: random.Random, limit: int, lines: int) -> tuple[int, int]:
    """Real files for the newest task runs only.

    Writing one for every task run would take longer than the rest of the script
    put together and model nothing: a home that has been running for ninety days
    has had retention delete the old ones, leaving exactly this — a path on
    every row and a file on the recent ones.
    """
    from sqlalchemy import select

    from runrail.models import TaskRun, TaskRunStatus

    files = written = 0
    rows = db.execute(
        select(TaskRun.id, TaskRun.workflow_run_id, TaskRun.status, TaskRun.created_at,
               TaskRun.task_id).order_by(TaskRun.id.desc()).limit(limit)).all()
    for row in rows:
        directory = logs_root / f"run_{row.workflow_run_id}"
        directory.mkdir(parents=True, exist_ok=True)
        fill = {"task": row.task_id, "ds": row.created_at.date().isoformat(),
                "ms": rng.randint(20, 9000), "rows": rng.randint(10, 500_000),
                "n": rng.randint(1, 99)}
        bodies = {
            "stdout": "\n".join(rng.choice(LOG_LINES).format(**fill) for _ in range(lines)),
            "stderr": "\n".join(line.format(**fill) for line in ERROR_LINES)
            if row.status == TaskRunStatus.failed else "",
        }
        for stream, body in bodies.items():
            path = log_path(logs_root, row.workflow_run_id, row.id, stream)
            path.write_text(body + "\n")
            files += 1
            written += len(body)
    return files, written


def apply_operator_state(db, rng: random.Random, specs: list[dict], current: datetime) -> None:
    """Snooze, auto-pause and a dead schedule: the states the notification
    centre and the watchdog read, and three more branches that volume alone
    leaves untouched."""
    from sqlalchemy import select

    from runrail.models import RunStatus, Workflow, WorkflowRun

    paused = next((spec for spec in specs if spec["auto_pause_failures"]), None)
    if paused:
        workflow = db.get(Workflow, paused["id"])
        workflow.enabled = False
        # notify.py disables on a trailing failure streak and the activity feed
        # reconstructs the pause from that streak, so the rows must really end
        # in failures.
        for run in db.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.workflow_id == workflow.id,
                       WorkflowRun.status.in_((RunStatus.success, RunStatus.failed)))
                .order_by(WorkflowRun.id.desc()).limit(workflow.auto_pause_failures)):
            run.status = RunStatus.failed
    snoozed = db.get(Workflow, rng.choice(specs)["id"])
    snoozed.snooze_until = current + timedelta(hours=6)
    snoozed.snooze_pauses_runs = True
    silent = next((spec for spec in specs
                   if spec["missed_run_grace_minutes"] and spec["schedule_cron"]), None)
    if silent:
        db.get(Workflow, silent["id"]).missed_notified_at = current - timedelta(hours=2)
    db.commit()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rng = random.Random(args.seed)
    clock = time.monotonic()
    say = (lambda *_: None) if args.quiet else print

    home = guard_home(args.home)
    database = open_home(home, args.db_url, args.reset)

    from sqlalchemy import bindparam, select, update

    from runrail.config import get_settings
    from runrail.models import (
        Artifact,
        ArtifactType,
        Environment,
        LockMode,
        Project,
        RunNote,
        RunStatus,
        Task,
        TaskRun,
        TaskType,
        Workflow,
        WorkflowRun,
    )

    logs_root = get_settings().logs_dir.resolve()
    artifacts_root = get_settings().artifacts_dir.resolve()
    current = datetime.now(timezone.utc)

    with database.SessionLocal() as db:
        if db.scalar(select(Workflow.id).limit(1)):
            # Seeding twice collides on Workflow.name and half-writes the run
            # tables before it does; say so instead of failing mid-insert.
            raise SystemExit(f"{home} already holds workflows. Pass --reset to start over.")
        project = Project(name="demo", root_path=str(home), description="Seeded demo project")
        environment = Environment(name="demo-system", executable=sys.executable)
        db.add_all([project, environment])
        db.commit()

        specs = workflow_specs(args, rng)
        insert_rows(db, Workflow, [{
            "name": spec["name"], "description": f"Seeded {spec['name']}",
            "schedule_cron": spec["schedule_cron"], "schedule_timezone": spec["schedule_timezone"],
            "enabled": True, "max_concurrent_runs": 1, "notify_webhook_url": None,
            "auto_pause_failures": spec["auto_pause_failures"], "project_id": project.id,
            "default_environment_id": environment.id, "snooze_until": None,
            "snooze_pauses_runs": False, "missed_notified_at": None,
            "missed_run_grace_minutes": spec["missed_run_grace_minutes"],
            "sla_minutes": spec["sla_minutes"], "lock_resource": spec["lock_resource"],
            "lock_mode": LockMode.shared,
            # created_at has to predate the history: find_gaps clips its window
            # to the workflow's own age, and a workflow born today owes nothing.
            "created_at": current - timedelta(days=args.days + 1), "updated_at": current,
        } for spec in specs])
        ids_by_name = dict(db.execute(select(Workflow.name, Workflow.id)).all())
        for spec in specs:
            spec["id"] = ids_by_name[spec["name"]]

        insert_rows(db, Task, [{
            "workflow_id": spec["id"], "project_id": project.id,
            "environment_id": environment.id, "name": name, "task_type": TaskType.shell,
            "command": f"python /opt/pipelines/{name}.py --ds {{{{ ds }}}}",
            "depends_on_json": [spec["task_names"][position - 1]] if position else [],
            "retries": 1, "retry_delay_seconds": 60,
            "requires_approval": spec["gated"] and position == len(spec["task_names"]) - 1,
            "created_at": current, "updated_at": current,
        } for spec in specs for position, name in enumerate(spec["task_names"])])
        task_ids = {(workflow_id, name): task_id for task_id, workflow_id, name
                    in db.execute(select(Task.id, Task.workflow_id, Task.name))}

        plans = plan_runs(args, rng, specs)
        insert_rows(db, WorkflowRun, [plan["row"] for plan in plans])
        # Ascending id is insertion order on every backend RunRail supports,
        # which is what pairs a run with the plan that produced it. Reading the
        # ids back rather than assigning them keeps a PostgreSQL sequence
        # pointing past the seeded rows — otherwise the first run the app
        # created afterwards would collide.
        run_ids = list(db.scalars(select(WorkflowRun.id).order_by(WorkflowRun.id)))
        task_runs = plan_task_runs(rng, plans, run_ids, task_ids)
        insert_rows(db, TaskRun, task_runs)
        pairs = db.execute(select(TaskRun.id, TaskRun.workflow_run_id)).all()
        # Straight at the table: the ORM's bulk-update path refuses a WHERE of
        # its own, and there is no identity map here to keep in step anyway.
        db.execute(update(TaskRun.__table__).where(
            TaskRun.__table__.c.id == bindparam("row_id")), [{
            "row_id": task_run_id,
            "stdout_log_path": str(log_path(logs_root, run_id, task_run_id, "stdout")),
            "stderr_log_path": str(log_path(logs_root, run_id, task_run_id, "stderr")),
        } for task_run_id, run_id in pairs])
        db.commit()
        say(f"  workflows {len(specs)}  runs {len(plans)}  task runs {len(task_runs)}")

        # Notes and artifacts: small tables the run pages and the bell read, so
        # an empty one hides a cost the real thing would pay.
        failed = list(db.scalars(
            select(WorkflowRun.id).where(WorkflowRun.status == RunStatus.failed)
            .order_by(WorkflowRun.id.desc()).limit(4000)))
        noted = rng.sample(failed, min(len(failed), 600))
        insert_rows(db, RunNote, [{
            "workflow_run_id": run_id, "body": rng.choice((
                "Upstream export was late again — chased the vendor.",
                "Known flake; reran by hand and it passed.",
                "Root cause: warehouse connection pool exhausted.")),
            "created_at": current, "updated_at": current,
        } for run_id in noted])
        insert_rows(db, Artifact, [{
            "task_run_id": row.id, "workflow_run_id": row.workflow_run_id,
            "name": "report.html", "artifact_type": ArtifactType.html,
            "path": str(artifacts_root / str(row.workflow_run_id) / "report.html"),
            "size_bytes": rng.randint(2000, 90_000), "created_at": row.created_at,
        } for row in db.execute(
            select(TaskRun.id, TaskRun.workflow_run_id, TaskRun.created_at)
            .order_by(TaskRun.id.desc()).limit(args.log_files)) if rng.random() < 0.15])

        apply_operator_state(db, rng, specs, current)
        files, written = write_logs(db, logs_root, rng, args.log_files, args.log_lines)
        say(f"  notes {len(noted)}  log files {files} ({written // 1024} KiB)")

    say(f"Seeded {home} in {time.monotonic() - clock:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
