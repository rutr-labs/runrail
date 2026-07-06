"""Retention cleanup for finished runs and their log/artifact files."""

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from runrail.config import get_settings
from runrail.models import Artifact, RunStatus, WorkflowRun

_TERMINAL = (RunStatus.success, RunStatus.failed, RunStatus.cancelled)


@dataclass
class CleanupStats:
    runs_deleted: int = 0
    files_deleted: int = 0
    errors: list[str] = field(default_factory=list)


def _remove_file(path: Path, stats: CleanupStats) -> None:
    try:
        if path.is_file():
            path.unlink()
            stats.files_deleted += 1
    except OSError as exc:
        stats.errors.append(f"{path}: {exc}")


def _remove_dir(path: Path, stats: CleanupStats) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
    except OSError as exc:
        stats.errors.append(f"{path}: {exc}")


def cleanup_runs(db: Session, older_than_days: int, dry_run: bool = False) -> CleanupStats:
    """Delete finished runs older than the cutoff along with their files on disk.

    Only terminal runs (success/failed/cancelled) are considered; queued and
    running runs are never touched. Deleting the WorkflowRun row cascades to its
    task runs and artifact rows.
    """
    settings = get_settings()
    # Aware UTC cutoff: SQLite drops the offset on bind (values are stored as
    # naive UTC), and PostgreSQL compares it correctly against timestamptz.
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    stats = CleanupStats()
    runs = db.scalars(
        select(WorkflowRun)
        .where(WorkflowRun.status.in_(_TERMINAL), WorkflowRun.created_at < cutoff)
        .options(selectinload(WorkflowRun.task_runs))
    ).all()
    logs_root = settings.logs_dir.resolve()
    artifacts_root = settings.artifacts_dir.resolve()
    for run in runs:
        stats.runs_deleted += 1
        if dry_run:
            continue
        for task_run in run.task_runs:
            for log_path in (task_run.stdout_log_path, task_run.stderr_log_path):
                if log_path:
                    _remove_file(Path(log_path), stats)
        for artifact in db.scalars(select(Artifact).where(Artifact.workflow_run_id == run.id)):
            _remove_file(Path(artifact.path), stats)
        # Per-run directories (current layout); harmless no-ops for legacy runs.
        _remove_dir(logs_root / f"run_{run.id}", stats)
        _remove_dir(artifacts_root / str(run.id), stats)
        db.delete(run)
    if not dry_run:
        db.commit()
    return stats
