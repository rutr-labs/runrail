"""Retention cleanup for finished runs and their log/artifact files."""

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from runrail.config import get_settings
from runrail.models import Artifact, RunStatus, TaskRun, WorkflowRun

_TERMINAL = (RunStatus.success, RunStatus.failed, RunStatus.cancelled)


@dataclass
class CleanupStats:
    runs_deleted: int = 0
    files_deleted: int = 0
    errors: list[str] = field(default_factory=list)


#: Runs deleted per transaction. Small enough that a pass over a large history
#: never holds SQLite's write lock for long, or loads it all into memory.
_BATCH = 200


def _count_files(path: Path) -> int:
    try:
        return sum(1 for entry in path.rglob("*") if entry.is_file()) if path.is_dir() else 0
    except OSError:
        return 0


def _remove_dir(path: Path, stats: CleanupStats) -> None:
    files = _count_files(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
            stats.files_deleted += files
    except OSError as exc:
        stats.errors.append(f"{path}: {exc}")


def _remove_file(path: Path, stats: CleanupStats) -> None:
    try:
        if path.is_file():
            path.unlink()
            stats.files_deleted += 1
    except OSError as exc:
        stats.errors.append(f"{path}: {exc}")


def _run_files(db: Session, run_ids: list[int], logs_root: Path,
               artifacts_root: Path) -> dict[int, tuple[list[Path], list[Path]]]:
    """Per run: its directories, plus any recorded file outside them (logs and
    artifacts from layouts before per-run directories existed)."""
    files: dict[int, set[Path]] = {run_id: set() for run_id in run_ids}
    for run_id, stdout, stderr in db.execute(
            select(TaskRun.workflow_run_id, TaskRun.stdout_log_path, TaskRun.stderr_log_path)
            .where(TaskRun.workflow_run_id.in_(run_ids))):
        files[run_id] |= {Path(path) for path in (stdout, stderr) if path}
    for run_id, path in db.execute(select(Artifact.workflow_run_id, Artifact.path)
                                   .where(Artifact.workflow_run_id.in_(run_ids))):
        if path:
            files[run_id].add(Path(path))
    layout = {}
    for run_id, paths in files.items():
        directories = [logs_root / f"run_{run_id}", artifacts_root / str(run_id)]
        loose = [path for path in paths if not any(path.is_relative_to(d) for d in directories)]
        layout[run_id] = (directories, loose)
    return layout


def _sweep_orphans(db: Session, logs_root: Path, artifacts_root: Path,
                   stats: CleanupStats) -> None:
    """Remove run directories whose run no longer exists: what a file that
    would not delete — held open on Windows, say — leaves behind. Directories
    are only ever created for a run that already has a row."""
    candidates: dict[int, list[Path]] = {}
    for root, prefix in ((logs_root, "run_"), (artifacts_root, "")):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            suffix = path.name[len(prefix):] if path.name.startswith(prefix) else ""
            if path.is_dir() and suffix.isdigit():
                candidates.setdefault(int(suffix), []).append(path)
    ids = list(candidates)
    for start in range(0, len(ids), _BATCH):
        chunk = ids[start:start + _BATCH]
        alive = set(db.scalars(select(WorkflowRun.id).where(WorkflowRun.id.in_(chunk))))
        for run_id in chunk:
            if run_id not in alive:
                for path in candidates[run_id]:
                    _remove_dir(path, stats)


def cleanup_runs(db: Session, older_than_days: int, dry_run: bool = False) -> CleanupStats:
    """Delete finished runs older than the cutoff along with their files on disk.

    Only terminal runs (success/failed/cancelled) that FINISHED before the
    cutoff are considered. Not created: a resume keeps a run's original
    created_at on purpose, so selecting on it deleted a month-old run resumed
    today the moment it finished, and a cancelled run whose worker had not yet
    stopped (finished_at is stamped when it does). The newest successful run of
    each workflow is always kept: it is what the workflow's pinned "latest
    report" link and its last-success figures point at.

    Rows go first, in small committed batches, and files after, outside any
    transaction. Deleting all the files before a single final commit meant a
    failed commit left rows pointing at nothing, and on SQLite the write lock
    was held for the whole pass, locking the worker and the API out. A file
    that will not delete is reported and its directory swept on a later pass.
    The database cascades a run's task runs, artifacts and notes.
    """
    settings = get_settings()
    # Aware UTC cutoff: SQLite drops the offset on bind (values are stored as
    # naive UTC), and PostgreSQL compares it correctly against timestamptz.
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    stats = CleanupStats()
    logs_root = settings.logs_dir.resolve()
    artifacts_root = settings.artifacts_dir.resolve()
    newest_success = (select(func.max(WorkflowRun.id))
                      .where(WorkflowRun.status == RunStatus.success)
                      .group_by(WorkflowRun.workflow_id))
    expired = (WorkflowRun.status.in_(_TERMINAL), WorkflowRun.finished_at.is_not(None),
               WorkflowRun.finished_at < cutoff, WorkflowRun.id.not_in(newest_success))
    after = 0
    while True:
        run_ids = list(db.scalars(select(WorkflowRun.id).where(*expired, WorkflowRun.id > after)
                                  .order_by(WorkflowRun.id).limit(_BATCH)))
        if not run_ids:
            break
        after = run_ids[-1]
        layout = _run_files(db, run_ids, logs_root, artifacts_root)
        if dry_run:
            stats.runs_deleted += len(run_ids)
            for directories, loose in layout.values():
                stats.files_deleted += (sum(map(_count_files, directories))
                                        + sum(1 for path in loose if path.is_file()))
            continue
        # Re-checked in the DELETE: a run resumed since it was selected is live again.
        db.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(run_ids), *expired))
        db.commit()
        survivors = set(db.scalars(select(WorkflowRun.id).where(WorkflowRun.id.in_(run_ids))))
        db.rollback()  # end the read transaction before the file I/O
        for run_id, (directories, loose) in layout.items():
            if run_id in survivors:
                continue
            stats.runs_deleted += 1
            for path in [*loose]:
                _remove_file(path, stats)
            for path in directories:
                _remove_dir(path, stats)
    if not dry_run:
        _sweep_orphans(db, logs_root, artifacts_root, stats)
        db.rollback()
    return stats
