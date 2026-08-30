"""Bounded grep across the log files runs already recorded.

No index of log content and no filesystem walk: TaskRun.stdout_log_path /
stderr_log_path *are* the candidate list, SQL scopes it over already-indexed
columns (workflow_runs.workflow_id, workflow_runs.created_at,
task_runs.workflow_run_id), and only the content match touches disk. An index
would need a writer in the worker, retention-aware deletion, a rebuild command,
and roughly double the log storage — and it could go stale. A missing file here
is simply skipped.

Every dimension is bounded (candidate rows, files opened, bytes per file,
matches returned, wall clock) so a search over a year of logs cannot stall the
single-process server. `stopped_by` and `stats.complete` report which bound
fired, because the worst thing this feature can do is claim a false "first
appeared".
"""

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.config import get_settings
from runrail.models import (
    RunStatus,
    Task,
    TaskRun,
    TaskRunStatus,
    Workflow,
    WorkflowRun,
    _aware,
)

MAX_PATTERN_LENGTH = 200
# One minified-JSON log line must not be able to blow up the response.
MAX_LINE_CHARS = 2000

# CSI sequences (colour, cursor moves); results render in a plain table.
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
# A heuristic, not a parser: a quantified group that itself contains a
# quantifier is the shape that backtracks exponentially. The wall-clock budget
# and max_bytes_per_file are what actually bound the damage.
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[*+][^)]*\)\s*[*+{]")


def _matcher(q: str, regex: bool, case_sensitive: bool):
    """Build a predicate over one raw log line (bytes).

    Matching on bytes rather than str means only the lines that actually hit
    are ever decoded.
    """
    if regex:
        if len(q) > MAX_PATTERN_LENGTH:
            raise ValueError(f"Pattern is longer than {MAX_PATTERN_LENGTH} characters")
        if _NESTED_QUANTIFIER.search(q):
            raise ValueError("Pattern nests quantifiers and could backtrack forever")
        try:
            pattern = re.compile(q.encode(), 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        return lambda line: pattern.search(line) is not None
    if case_sensitive:
        needle = q.encode()
        return lambda line: needle in line
    lowered = q.lower().encode()
    return lambda line: lowered in line.lower()


def _utc(value: datetime) -> datetime:
    """A client-supplied bound, normalised to UTC.

    SQLite's DATETIME bind drops the offset, so a `+04:00` bound would compare
    four hours wrong; aware UTC binds correctly on SQLite and PostgreSQL both.
    """
    return _aware(value).astimezone(timezone.utc)


def _decode(raw: bytes) -> str:
    return _ANSI.sub(b"", raw).decode("utf-8", errors="replace").rstrip("\r")[:MAX_LINE_CHARS]


def _confined(path: Path, root: Path) -> bool:
    """Only DB-sourced paths are ever opened, but a hand-edited or imported row
    must not turn this endpoint into an arbitrary-file reader."""
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def search_logs(
    db: Session, *, q: str, regex: bool = False, case_sensitive: bool = False,
    workflow_id: int | None = None, task_id: int | None = None, task_name: str | None = None,
    status: RunStatus | None = None, task_status: TaskRunStatus | None = None,
    stream: str = "both", since: datetime | None = None, until: datetime | None = None,
    limit: int = 50, context: int = 2, max_files: int = 2000,
    max_bytes_per_file: int = 5_000_000, timeout_ms: int = 5000,
) -> dict:
    """Grep the newest logs in scope. Raises ValueError on an unusable pattern."""
    match = _matcher(q, regex, case_sensitive)
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    logs_root = get_settings().logs_dir.resolve()

    # Newest-first is what makes this usable during an incident; the
    # oldest-match answer is then derived from whatever window we covered.
    stmt = (select(TaskRun, WorkflowRun, Workflow.name.label("workflow_name"),
                   Task.name.label("task_name"))
            .join(WorkflowRun, TaskRun.workflow_run_id == WorkflowRun.id)
            .join(Workflow, WorkflowRun.workflow_id == Workflow.id)
            .join(Task, TaskRun.task_id == Task.id)
            .order_by(WorkflowRun.created_at.desc(), TaskRun.id.desc())
            .limit(max_files))
    if workflow_id: stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
    if task_id: stmt = stmt.where(TaskRun.task_id == task_id)
    if task_name: stmt = stmt.where(Task.name == task_name)
    if status: stmt = stmt.where(WorkflowRun.status == status)
    if task_status: stmt = stmt.where(TaskRun.status == task_status)
    if since: stmt = stmt.where(WorkflowRun.created_at >= _utc(since))
    if until: stmt = stmt.where(WorkflowRun.created_at <= _utc(until))

    # stderr first: errors live there, so hit quality survives truncation.
    streams = ("stderr", "stdout") if stream == "both" else (stream,)
    matches: list[dict] = []
    files_scanned = files_missing = bytes_scanned = truncated_files = rows_seen = 0
    stopped_by: str | None = None

    for task_run, run, workflow_name, name in db.execute(stmt):
        # Bounds are checked between files, so a timeout never returns half of
        # one file's hits.
        if len(matches) >= limit: stopped_by = "limit"; break
        if time.monotonic() > deadline: stopped_by = "timeout"; break
        if files_scanned >= max_files: stopped_by = "max_files"; break
        rows_seen += 1
        for stream_name in streams:
            path = getattr(task_run, f"{stream_name}_log_path")
            if not path or not _confined(Path(path), logs_root): continue
            file = Path(path)
            try:
                size = file.stat().st_size
                if not size: continue
                # The head, not the tail (unlike log_response): "when did this
                # first appear" lives at the head, and reading forwards keeps
                # line numbers exact so the deep link still lands.
                with file.open("rb") as handle:
                    data = handle.read(max_bytes_per_file)
            except OSError:
                files_missing += 1
                continue
            files_scanned += 1
            bytes_scanned += len(data)
            truncated = size > max_bytes_per_file
            if truncated: truncated_files += 1
            lines = data.split(b"\n")
            if truncated: lines.pop()  # the byte cap sliced the last line in half
            for index, raw in enumerate(lines):
                if not match(raw): continue
                matches.append({
                    "workflow_run_id": run.id, "workflow_id": run.workflow_id,
                    "workflow_name": workflow_name, "run_status": run.status.value,
                    "run_created_at": _aware(run.created_at),
                    "task_run_id": task_run.id, "task_id": task_run.task_id,
                    "task_name": name, "task_status": task_run.status.value,
                    "attempt": task_run.attempt, "stream": stream_name,
                    "line_number": index + 1,
                    "line": _decode(raw),
                    "context_before": [_decode(x) for x in lines[max(0, index - context):index]],
                    "context_after": [_decode(x) for x in lines[index + 1:index + 1 + context]],
                })
                if len(matches) >= limit:
                    # The one mid-file stop: past `limit` nothing more can be
                    # reported, so scanning on spends I/O for no result.
                    stopped_by = "limit"
                    break
            if stopped_by: break
        if stopped_by: break

    if stopped_by is None and rows_seen >= max_files:
        # The candidate query itself was capped, so older logs went unread.
        stopped_by = "max_files"
    oldest = min(matches, key=lambda m: m["run_created_at"], default=None)
    return {
        "query": q,
        "regex": regex,
        "matches": matches,
        "stats": {
            "files_scanned": files_scanned, "files_missing": files_missing,
            "bytes_scanned": bytes_scanned,
            "runs_matched": len({m["workflow_run_id"] for m in matches}),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "truncated_files": truncated_files,
            "complete": stopped_by is None, "stopped_by": stopped_by,
        },
        "oldest_match": None if oldest is None else {
            "workflow_run_id": oldest["workflow_run_id"],
            "run_created_at": oldest["run_created_at"],
        },
    }
