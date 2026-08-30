import os
import signal
import sys
import threading
import time
from datetime import date
from pathlib import Path

import typer
import uvicorn
from sqlalchemy import select

from runrail.api.crud import create_backfill, create_run
from runrail.config import get_settings
from runrail.db import SessionLocal, init_db
from runrail.models import RunStatus, TriggerType, Workflow, WorkflowRun, _aware, now
from runrail.scheduler.service import SchedulerService
from runrail.worker.service import WorkerService

app = typer.Typer(help="RunRail: workflows for the scripts you already have", no_args_is_help=True)


def _workflow(db, name: str) -> Workflow:
    workflow = db.scalar(select(Workflow).where(Workflow.name == name))
    if not workflow: raise typer.BadParameter(f"Workflow '{name}' does not exist")
    return workflow


def _params(values: list[str] | None) -> dict[str, str]:
    result = {}
    for value in values or []:
        if "=" not in value: raise typer.BadParameter(f"Parameter must be key=value: {value}")
        key, item = value.split("=", 1); result[key] = item
    return result


@app.command()
def init() -> None:
    """Create RunRail's home, database, log, and artifact directories."""
    init_db()
    typer.echo(f"RunRail initialized at {get_settings().home.resolve()}")


def _apply_yaml(file: Path) -> None:
    """Apply a workflows YAML (shared by 'apply', 'import', and first-run)."""
    import yaml

    from runrail.workflow_io import apply_workflows

    init_db()
    try:
        data = yaml.safe_load(file.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise typer.BadParameter(f"Could not read {file}: {exc}") from exc
    with SessionLocal() as db:
        try:
            summary = apply_workflows(db, data or {})
        except (ValueError, KeyError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    for verb in ("created", "updated"):
        for wf_name in summary[verb]:
            typer.echo(f"{verb}: {wf_name}")


def _import_source(source: Path) -> None:
    """Import a previous data directory or a workflows YAML into this home.

    Directories are copied before init_db so the normal startup migrations
    upgrade the imported database; YAML files need the schema first, so there
    the order flips.
    """
    from runrail.importer import ImportSourceError, import_home

    source = source.expanduser()
    if source.is_dir():
        settings = get_settings()
        try:
            lines = import_home(source, settings.home, on_progress=typer.echo)
        except ImportSourceError as exc:
            raise typer.BadParameter(str(exc)) from exc
        init_db()  # upgrades the imported database to the current schema
        typer.echo(f"Imported {source.expanduser().resolve()} → {settings.home.resolve()}")
        for line in lines:
            typer.echo(f"  {line}")
    elif source.is_file():
        _apply_yaml(source)
    else:
        raise typer.BadParameter(f"{source} does not exist")


@app.command("import")
def import_data(source: str = typer.Argument(
        ..., help="A previous RunRail data directory, or a YAML file from 'runrail export'")) -> None:
    """Import an existing RunRail data directory or workflows YAML into this home."""
    _import_source(Path(source))


def _offer_first_run_import() -> None:
    """On a brand-new home, offer to bring over an existing setup (TTY only)."""
    from runrail.importer import is_fresh_home

    settings = get_settings()
    if not is_fresh_home(settings) or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    typer.echo(f"Setting up a new RunRail workspace at {settings.home.resolve()}")
    typer.echo("  Have an existing setup? Enter the path to its data directory")
    typer.echo("  (e.g. ./.runrail) or a workflows YAML from 'runrail export'.")
    typer.echo("  Press Enter to start fresh.")
    try:
        answer = typer.prompt("Import from", default="", show_default=False).strip()
    except typer.Abort:  # Ctrl+C / Ctrl+D at the offer means "no import", not "don't start"
        typer.echo("")
        return
    if not answer:
        return
    try:
        _import_source(Path(answer))
    except typer.BadParameter as exc:
        typer.echo(f"Import failed: {exc.message}", err=True)
        typer.echo("Fix the source and retry with 'runrail import <path>', or run "
                   "'runrail serve' again to start fresh.", err=True)
        raise typer.Exit(1) from exc


@app.command()
def api(host: str | None = None, port: int | None = None) -> None:
    """Start the API and bundled web UI only."""
    init_db(); settings = get_settings()
    uvicorn.run("runrail.api.app:app", host=host or settings.host, port=port or settings.port)


@app.command()
def worker(concurrency: int | None = typer.Option(None, "--concurrency", min=1,
                                                  help="Max runs executing at once")) -> None:
    """Start a local subprocess worker."""
    init_db()
    service = WorkerService(concurrency=concurrency)
    typer.echo(f"RunRail worker started (concurrency {service.concurrency})")
    service.run()


@app.command()
def scheduler() -> None:
    """Start the scheduling clock only."""
    init_db(); typer.echo("RunRail scheduler started"); SchedulerService().run_forever()


def _executing_summary(worker_service: WorkerService) -> list[str]:
    """Human lines for what is still executing, with a median-based ETA."""
    run_ids = worker_service.executing_run_ids()
    if not run_ids:
        return ["Waiting for the worker to finish up… (Ctrl+C to force quit)"]
    lines = []
    with SessionLocal() as db:
        for run_id in run_ids:
            run = db.get(WorkflowRun, run_id)
            if run is None:
                continue
            workflow = db.get(Workflow, run.workflow_id)
            name = workflow.name if workflow else f"workflow {run.workflow_id}"
            # Through _aware: PostgreSQL hands back an aware datetime and SQLite a
            # naive one, and subtracting a mix of the two raises TypeError.
            started = _aware(run.started_at) or _aware(run.created_at)
            elapsed = (now() - started).total_seconds()
            durations = sorted(d for d in db.scalars(
                select(WorkflowRun.duration_seconds).where(
                    WorkflowRun.workflow_id == run.workflow_id,
                    WorkflowRun.status == RunStatus.success,
                    WorkflowRun.duration_seconds.is_not(None),
                ).order_by(WorkflowRun.id.desc()).limit(5)
            ) if d is not None)
            median = durations[len(durations) // 2] if durations else None
            eta = (f"~{max(0.0, median - elapsed):.0f}s remaining (median {median:.0f}s)"
                   if median is not None else f"{elapsed:.0f}s elapsed")
            lines.append(f"  run #{run.id} of '{name}' — {eta}")
    if lines:
        lines.append("Waiting for the run(s) above to finish. Press Ctrl+C again to force quit.")
    return lines or ["Waiting for the worker to finish up… (Ctrl+C to force quit)"]


def _drain_worker(worker_service: WorkerService, thread: threading.Thread) -> None:
    """Wait for executing runs after shutdown begins; a second Ctrl+C force-quits."""
    def force(*_):
        typer.echo("\nForce quitting — interrupted runs will be marked failed on the next start.")
        os._exit(130)
    # Explicit handler: uvicorn/asyncio leave SIGINT dispositions in an
    # unreliable state after their own shutdown dance.
    try:
        signal.signal(signal.SIGINT, force)
        signal.signal(signal.SIGTERM, force)
    except ValueError:
        pass  # not the main thread; KeyboardInterrupt fallback below still applies
    try:
        last_note = 0.0
        while thread.is_alive():
            thread.join(timeout=1)
            if not thread.is_alive():
                break
            if time.monotonic() - last_note >= 5:
                last_note = time.monotonic()
                for line in _executing_summary(worker_service):
                    typer.echo(line)
        typer.echo("RunRail stopped cleanly.")
    except KeyboardInterrupt:
        force()


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    """Start API, UI, scheduler, and local worker together."""
    _offer_first_run_import()
    init_db(); settings = get_settings()
    worker_service = WorkerService(); scheduler_service = SchedulerService()
    thread = threading.Thread(target=worker_service.run, kwargs={"install_signals": False},
                              name="runrail-worker", daemon=True)
    thread.start(); scheduler_service.start()
    typer.echo(f"RunRail is ready at http://{host or settings.host}:{port or settings.port}")
    try:
        uvicorn.run("runrail.api.app:app", host=host or settings.host, port=port or settings.port)
    except KeyboardInterrupt:
        pass  # uvicorn re-raises the captured SIGINT after its own shutdown
    finally:
        scheduler_service.shutdown()
        worker_service.stop()
        _drain_worker(worker_service, thread)


@app.command("run")
def run_workflow(name: str, param: list[str] | None = typer.Option(None, "--param")) -> None:
    """Queue a workflow run."""
    init_db()
    with SessionLocal() as db:
        run = create_run(db, _workflow(db, name), TriggerType.cli, _params(param))
        typer.echo(f"Queued workflow run {run.id}")


@app.command()
def backfill(name: str, from_date: str = typer.Option(..., "--from"),
             to_date: str = typer.Option(..., "--to"),
             param: list[str] | None = typer.Option(None, "--param")) -> None:
    """Queue one workflow run per date in an inclusive range."""
    init_db()
    with SessionLocal() as db:
        try:
            start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
        except ValueError as exc:
            raise typer.BadParameter("Dates must use YYYY-MM-DD") from exc
        runs = create_backfill(db, _workflow(db, name), start, end, _params(param))
        typer.echo(f"Queued {len(runs)} backfill run(s)")


@app.command()
def export(name: str | None = typer.Argument(None, help="Workflow name; omit for all"),
           output: str | None = typer.Option(None, "--output", "-o",
                                             help="Write to a file instead of stdout")) -> None:
    """Export workflows and their tasks as YAML (projects/environments by name)."""
    import yaml

    from runrail.workflow_io import export_workflows

    init_db()
    with SessionLocal() as db:
        try:
            data = export_workflows(db, name)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if output:
        Path(output).write_text(text)
        typer.echo(f"Wrote {len(data['workflows'])} workflow(s) to {output}")
    else:
        typer.echo(text)


@app.command()
def apply(file: str = typer.Argument(..., help="YAML file produced by 'runrail export'")) -> None:
    """Create or update workflows from a YAML file (declarative upsert by name)."""
    _apply_yaml(Path(file))


@app.command()
def cleanup(older_than_days: int = typer.Option(30, "--older-than-days", min=1,
                                                help="Delete finished runs older than this"),
            dry_run: bool = typer.Option(False, "--dry-run",
                                         help="Report what would be deleted without deleting")) -> None:
    """Delete old finished runs plus their log and artifact files."""
    from runrail.maintenance import cleanup_runs

    init_db()
    with SessionLocal() as db:
        stats = cleanup_runs(db, older_than_days, dry_run=dry_run)
    action = "Would delete" if dry_run else "Deleted"
    typer.echo(f"{action} {stats.runs_deleted} run(s) and {stats.files_deleted} file(s)")
    for error in stats.errors:
        typer.echo(f"warning: {error}", err=True)


@app.command()
def status() -> None:
    """Show workflows and recent run statuses."""
    init_db()
    with SessionLocal() as db:
        workflows = db.scalars(select(Workflow).order_by(Workflow.name)).all()
        for workflow in workflows:
            latest = db.scalar(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow.id)
                               .order_by(WorkflowRun.created_at.desc()).limit(1))
            typer.echo(f"{workflow.name:30} {latest.status.value if latest else 'never run'}")
