import threading
from datetime import date

import typer
import uvicorn
from sqlalchemy import select

from runrail.api.crud import create_backfill, create_run
from runrail.config import get_settings
from runrail.db import SessionLocal, init_db
from runrail.models import TriggerType, Workflow, WorkflowRun
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


@app.command()
def serve(host: str | None = None, port: int | None = None) -> None:
    """Start API, UI, scheduler, and local worker together."""
    init_db(); settings = get_settings()
    worker_service = WorkerService(); scheduler_service = SchedulerService()
    thread = threading.Thread(target=worker_service.run, kwargs={"install_signals": False},
                              name="runrail-worker", daemon=True)
    thread.start(); scheduler_service.start()
    typer.echo(f"RunRail is ready at http://{host or settings.host}:{port or settings.port}")
    try:
        uvicorn.run("runrail.api.app:app", host=host or settings.host, port=port or settings.port)
    finally:
        worker_service.stop(); scheduler_service.shutdown(); thread.join(timeout=5)


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
        from pathlib import Path
        Path(output).write_text(text)
        typer.echo(f"Wrote {len(data['workflows'])} workflow(s) to {output}")
    else:
        typer.echo(text)


@app.command()
def apply(file: str = typer.Argument(..., help="YAML file produced by 'runrail export'")) -> None:
    """Create or update workflows from a YAML file (declarative upsert by name)."""
    from pathlib import Path

    import yaml

    from runrail.workflow_io import apply_workflows

    init_db()
    try:
        data = yaml.safe_load(Path(file).read_text())
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
