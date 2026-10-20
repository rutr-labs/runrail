"""What changed between two runs of a workflow — usually a failed run and the
last one that succeeded.

Recorded as each task starts (`capture_inputs`): the code it runs, by
runrail.codetrack (its project, or a loose script and what it imports); and
its environment's Python and installed packages, from `pip freeze`, listed in
the background and cached a few minutes per environment.

Compared on request (`compare`): parameters, task outcomes and timings, the
commands as run, the code file by file, package versions, the log lines
that are new, gone or changed, and for notebooks the cells whose code or
output changed, the cell that failed and the cells that got much slower.

Everything is deterministic: no guessing at causes, only what differs. Values
pass through the secrets masker before they leave.
"""

import difflib
import functools
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail import codetrack, variables
from runrail.config import get_settings
from runrail.masking import Masker
from runrail.models import (
    Artifact,
    ArtifactType,
    Environment,
    RunStatus,
    Task,
    TaskRun,
    WorkflowRun,
    _aware,
)
from runrail.paths import confined

log = logging.getLogger("runrail.rundiff")

FREEZE_CACHE_SECONDS = 600.0
FREEZE_TIMEOUT_SECONDS = 30

#: How much of each log stream is read for the comparison: the end, where
#: a failure is.
LOG_BYTES = 1024 * 1024
MAX_NEW_LINES = 200
MAX_GONE_LINES = 60
MAX_CHANGED_LINES = 60
MAX_DIFF_LINES = 300
MAX_OUTPUT_LINES = 15
#: A notebook cell is reported as slower past both of these.
SLOWER_FACTOR = 2.0
SLOWER_SECONDS = 5.0


# ─── Recording what a task ran with ─────────────────────────────────────


_freeze_lock = threading.Lock()
_freezes: dict[tuple, tuple[float, dict[str, Any]]] = {}


def _freeze_key(environment: Environment, python_command: list[str]) -> tuple:
    return (environment.id, tuple(python_command), str(environment.last_built_at))


def _cached_freeze(key: tuple) -> dict[str, Any] | None:
    with _freeze_lock:
        cached = _freezes.get(key)
    return cached[1] if cached and time.monotonic() - cached[0] < FREEZE_CACHE_SECONDS else None


def _freeze(key: tuple, python_command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run([*python_command, "-m", "pip", "freeze", "--disable-pip-version-check"],
                                   capture_output=True, text=True, timeout=FREEZE_TIMEOUT_SECONDS,
                                   check=False)
        packages = sorted(line.strip() for line in completed.stdout.splitlines()
                          if line.strip() and not line.startswith("#"))
        result: dict[str, Any] = ({"packages": packages} if completed.returncode == 0
                                  else {"packages_error": (completed.stderr or "").strip()[-300:]})
    except (OSError, subprocess.SubprocessError) as exc:
        result = {"packages_error": str(exc)[:300]}
    with _freeze_lock:
        if len(_freezes) > 64:
            _freezes.clear()
        _freezes[key] = (time.monotonic(), result)
    return result


#: Listings in flight, with the environment each runs a Python from.
_pending: dict[threading.Thread, int | None] = {}


def _packages_later(task_run_id: int, key: tuple, python_command: list[str]) -> None:
    """List the packages off the task's clock — a cold `pip freeze` can take
    seconds, which were counted in the task's duration — and add them to the
    attempt's record when they arrive."""
    def work() -> None:
        from runrail.db import SessionLocal
        try:
            result = _freeze(key, python_command)
            with SessionLocal() as db:
                row = db.get(TaskRun, task_run_id)
                if row is not None:
                    inputs = dict(row.inputs_json or {})
                    inputs["environment"] = {**(inputs.get("environment") or {}), **result}
                    row.inputs_json = inputs
                    db.commit()
        except Exception:
            log.warning("could not list packages for task run %s", task_run_id, exc_info=True)
        finally:
            with _freeze_lock:
                _pending.pop(thread, None)

    thread = threading.Thread(target=work, daemon=True, name=f"runrail-packages-{task_run_id}")
    with _freeze_lock:
        _pending[thread] = key[0]
    thread.start()


def wait_for_package_lists(timeout: float = 60.0, environment_id: int | None = None) -> None:
    """Block until package listings in flight are recorded: all of them, or
    those of one environment. A listing runs that environment's own Python,
    and on Windows a running program's files cannot be deleted, so removing
    or rebuilding an environment waits for it first."""
    deadline = time.monotonic() + timeout
    while True:
        with _freeze_lock:
            threads = [t for t, env in _pending.items() if environment_id is None or env == environment_id]
        if not threads or time.monotonic() > deadline:
            return
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))


def capture_inputs(db: Session, task: Task, render, cwd: Path, environment: Environment | None,
                   python_command: list[str] | None, task_run_id: int | None = None,
                   project_root: str | None = None):
    """The record of what this task attempt is about to run with, and a
    callable to start once that record is committed (it lists the packages in
    the background when they are not cached), or None. Never raises: a diff
    that cannot be made must not fail a run."""
    inputs: dict[str, Any] = {}
    later = None
    from runrail import runtime_settings
    code = codetrack.capture(db, task, render, cwd, project_root,
                             mode=runtime_settings.current("code_tracking") or "auto")
    if code:
        inputs["code"] = code
    if environment is not None:
        env: dict[str, Any] = {"id": environment.id, "name": environment.name,
                               "python": environment.python_version,
                               "built_at": (_aware(environment.last_built_at).isoformat()
                                            if environment.last_built_at else None)}
        if python_command:
            key = _freeze_key(environment, python_command)
            cached = _cached_freeze(key)
            if cached is not None:
                env.update(cached)
            elif task_run_id is not None:
                later = functools.partial(_packages_later, task_run_id, key, list(python_command))
        inputs["environment"] = env
    return inputs, later


# ─── Comparing ──────────────────────────────────────────────────────────


def baseline_for(db: Session, run: WorkflowRun) -> WorkflowRun | None:
    """The last run of the same workflow before this one that succeeded."""
    return db.scalar(select(WorkflowRun).where(
        WorkflowRun.workflow_id == run.workflow_id, WorkflowRun.id < run.id,
        WorkflowRun.status == RunStatus.success).order_by(WorkflowRun.id.desc()).limit(1))


def _final_attempts(db: Session, run: WorkflowRun) -> dict[int, tuple[TaskRun, int]]:
    """Each task's last attempt in the run, and how many attempts it took."""
    rows = list(db.scalars(select(TaskRun).where(TaskRun.workflow_run_id == run.id)
                           .order_by(TaskRun.id)))
    final: dict[int, tuple[TaskRun, int]] = {}
    for row in rows:
        count = final[row.task_id][1] + 1 if row.task_id in final else 1
        final[row.task_id] = (row, count)
    return final


class _Volatile:
    """Rewrites what differs between any two runs — times, ids, durations —
    so only real differences are left. Whole numbers are kept: "loaded 0
    rows" against "loaded 48210 rows" is the point. A second, looser key
    ignores them too, to pair such lines up as one changed line."""

    PATTERNS = [
        (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "‹time›"),
        (re.compile(r"\b\d{8}T\d{6}\b"), "‹time›"),
        (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "‹date›"),
        (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "‹time›"),
        (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "‹id›"),
        (re.compile(r"\b0x[0-9a-f]+\b", re.I), "‹addr›"),
        (re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{12,}\b", re.I), "‹hash›"),
        (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|µs|us|ns|s|sec|secs|seconds?|m|min|mins|minutes?|h|hrs?|hours?)\b"), "‹duration›"),
        (re.compile(r"\b\d+\.\d+\b"), "‹decimal›"),
        (re.compile(r"\b\d{1,3}(?:\.\d+)?%"), "‹percent›"),
    ]
    INTEGER = re.compile(r"\d+")

    def __init__(self, ids: list[int]):
        # A run's own ids show up in its paths (artifacts/57/report_…_123.ipynb)
        # and as "#57"; they differ by design. Only there: a bare 57 elsewhere
        # in a line may be a count that matters.
        alternatives = "|".join(str(i) for i in sorted(set(ids), reverse=True))
        self.ids = re.compile(rf"(?:(?<=[/\\_#-])|(?<=run ))(?:{alternatives})(?=[/\\_.\-]|\b)") \
            if ids else None

    def exact(self, line: str) -> str:
        text = line.strip()
        if self.ids is not None:
            text = self.ids.sub("‹run›", text)
        for pattern, token in self.PATTERNS:
            text = pattern.sub(token, text)
        return re.sub(r"\s+", " ", text)

    def loose(self, key: str) -> str:
        return self.INTEGER.sub("‹n›", key)


_ERROR = re.compile(r"error|exception|traceback|failed|fatal|refused|denied|not found|no such|timed? ?out",
                    re.I)


def _tail_lines(path: str | None, masker: Masker) -> tuple[list[str], bool]:
    if not path or not confined(path, get_settings().logs_dir):
        return [], False
    file = Path(path)
    try:
        size = file.stat().st_size
        with file.open("rb") as handle:
            if size > LOG_BYTES:
                handle.seek(size - LOG_BYTES)
            data = handle.read()
    except OSError:
        return [], False
    lines = masker.text(data.decode("utf-8", errors="replace")).splitlines()
    if size > LOG_BYTES and lines:
        lines = lines[1:]  # the cut sliced the first line
    return lines, size > LOG_BYTES


def _weight(before: str, after: str) -> int:
    """How telling a changed number is: 3 to or from zero, 2 off by ten
    times or more, 1 otherwise."""
    weight = 0
    for x, y in zip(map(int, _Volatile.INTEGER.findall(before)), map(int, _Volatile.INTEGER.findall(after)),
                    strict=False):
        if x == y:
            continue
        if 0 in (x, y):
            return 3
        weight = max(weight, 2 if max(x, y) >= 10 * min(x, y) else 1)
    return weight


def _log_changes(after: TaskRun, before: TaskRun, volatile: _Volatile, masker: Masker) -> dict[str, Any]:
    def read(task_run: TaskRun) -> tuple[list[tuple[str, int, str]], bool]:
        out, cut = [], False
        for stream in ("stdout", "stderr"):
            lines, truncated = _tail_lines(getattr(task_run, f"{stream}_log_path"), masker)
            cut = cut or truncated
            out += [(stream, number, line) for number, line in enumerate(lines, 1) if line.strip()]
        return out, cut

    new_rows, new_cut = read(after)
    old_rows, old_cut = read(before)
    old_keys = {volatile.exact(line) for _, _, line in old_rows}
    new_keys = {volatile.exact(line) for _, _, line in new_rows}
    fresh, seen = [], set()
    for stream, number, line in new_rows:
        key = volatile.exact(line)
        if key not in old_keys and key not in seen:
            seen.add(key)
            fresh.append({"stream": stream, "line": number, "text": line, "key": key})
    gone, seen = [], set()
    for stream, number, line in old_rows:
        key = volatile.exact(line)
        if key not in new_keys and key not in seen:
            seen.add(key)
            gone.append({"stream": stream, "line": number, "text": line, "key": key})
    # Pair lines that differ only in their whole numbers: one changed line.
    # Many lines share a shape ("step 12 processed 523 rows"), so each shape
    # holds a queue and pairs in order, line for line.
    by_loose: dict[str, list[dict]] = {}
    for row in gone:
        by_loose.setdefault(volatile.loose(row["key"]), []).append(row)
    changed, still_new = [], []
    for row in fresh:
        queue = by_loose.get(volatile.loose(row["key"]))
        if queue:
            match = queue.pop(0)
            changed.append({"stream": row["stream"], "before": match["text"], "after": row["text"],
                            "line": row["line"]})
            match["paired"] = True
        else:
            still_new.append(row)
    # Shown: the changes that say most — a count to or from zero, then one
    # off by ten times or more — in log order.
    shown = sorted(changed, key=lambda c: (-_weight(c["before"], c["after"]), c["line"]))[:MAX_CHANGED_LINES]
    shown.sort(key=lambda c: (c["stream"], c["line"]))
    still_gone = [row for row in gone if not row.get("paired")]
    first_error = next((row["text"] for row in still_new if _ERROR.search(row["text"])), None)
    def strip(rows: list[dict], cap: int) -> list[dict]:
        return [{k: v for k, v in r.items() if k not in ("key", "paired")} for r in rows[:cap]]

    return {
        "new": strip(still_new, MAX_NEW_LINES), "new_total": len(still_new),
        "gone": strip(still_gone, MAX_GONE_LINES), "gone_total": len(still_gone),
        "changed": shown, "changed_total": len(changed),
        "first_error": first_error, "truncated": new_cut or old_cut,
    }


def _unified(before: str, after: str, label_before: str, label_after: str) -> list[str]:
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                      label_before, label_after, n=2, lineterm=""))
    return lines[:MAX_DIFF_LINES]


def _package_map(packages: list[str]) -> dict[str, str]:
    found = {}
    for line in packages:
        name, sep, version = line.partition("==")
        if not sep:
            name, _, version = line.partition(" @ ")
        found[re.sub(r"[-_.]+", "-", name.strip()).lower()] = line.strip()
    return found


def _environment_changes(after: dict, before: dict) -> dict[str, Any] | None:
    a, b = after.get("environment"), before.get("environment")
    if not a and not b:
        return None
    if not a or not b:
        return {"before": b and b.get("name"), "after": a and a.get("name"), "packages": []}
    result: dict[str, Any] = {"name": a.get("name")}
    if a.get("name") != b.get("name"):
        result["renamed"] = {"before": b.get("name"), "after": a.get("name")}
    if a.get("python") != b.get("python"):
        result["python"] = {"before": b.get("python"), "after": a.get("python")}
    if a.get("built_at") != b.get("built_at"):
        result["rebuilt_at"] = a.get("built_at")
    old, new = _package_map(b.get("packages") or []), _package_map(a.get("packages") or [])
    packages = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            packages.append({"name": key, "before": old.get(key), "after": new.get(key)})
    result["packages"] = packages
    if "packages" not in a or "packages" not in b:
        result["packages_unknown"] = True
    meaningful = ("renamed" in result or "python" in result or packages)
    return result if meaningful else None


# ─── Notebooks ──────────────────────────────────────────────────────────


def _notebook_for(db: Session, task_run: TaskRun) -> dict | None:
    path = db.scalar(select(Artifact.path).where(Artifact.task_run_id == task_run.id,
                                                 Artifact.artifact_type == ArtifactType.notebook))
    if not path or not confined(path, get_settings().artifacts_dir):
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _text(value: Any) -> str:
    return "".join(value) if isinstance(value, list) else str(value or "")


def _cell_output(cell: dict) -> tuple[str, str | None]:
    parts, error = [], None
    for output in cell.get("outputs") or []:
        kind = output.get("output_type")
        if kind == "stream":
            parts.append(_text(output.get("text")))
        elif kind in ("execute_result", "display_data"):
            data = output.get("data") or {}
            if "text/plain" in data:
                parts.append(_text(data["text/plain"]))
            elif data:
                parts.append(f"[{', '.join(sorted(data))}]")
        elif kind == "error":
            error = f"{output.get('ename')}: {output.get('evalue')}"
            parts.append(error)
    return "".join(parts), error


def _code_cells(notebook: dict) -> list[dict]:
    cells = []
    for cell in notebook.get("cells") or []:
        if cell.get("cell_type") != "code":
            continue
        tags = (cell.get("metadata") or {}).get("tags") or []
        if "injected-parameters" in tags:
            continue  # the run's parameters, compared on their own
        meta = (cell.get("metadata") or {}).get("papermill") or {}
        output, error = _cell_output(cell)
        cells.append({"source": _text(cell.get("source")), "output": output, "error": error,
                      "duration": meta.get("duration"),
                      "failed": bool(error) or meta.get("status") == "failed" or bool(meta.get("exception"))})
    return cells


def _normalised(text: str, volatile: _Volatile) -> str:
    return "\n".join(volatile.exact(line) for line in text.splitlines())


def _notebook_changes(db: Session, after: TaskRun, before: TaskRun, volatile: _Volatile,
                      masker: Masker) -> dict[str, Any] | None:
    a, b = _notebook_for(db, after), _notebook_for(db, before)
    if a is None or b is None:
        return None if a is None and b is None else {"missing": "this run" if a is None else "the last good run"}
    new, old = _code_cells(masker.json(a)), _code_cells(masker.json(b))
    matcher = difflib.SequenceMatcher(a=[c["source"].strip() for c in old],
                                      b=[c["source"].strip() for c in new], autojunk=False)
    pairs: list[tuple[int | None, int | None]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or tag == "replace":
            for k in range(max(i2 - i1, j2 - j1)):
                pairs.append((i1 + k if i1 + k < i2 else None, j1 + k if j1 + k < j2 else None))
        elif tag == "delete":
            pairs += [(i, None) for i in range(i1, i2)]
        else:
            pairs += [(None, j) for j in range(j1, j2)]

    def lines(text: str) -> list[str]:
        rows = text.splitlines()
        return rows[:MAX_OUTPUT_LINES] + ([f"… {len(rows) - MAX_OUTPUT_LINES} more lines"]
                                          if len(rows) > MAX_OUTPUT_LINES else [])

    cells = []
    for i, j in pairs:
        before_cell = old[i] if i is not None else None
        after_cell = new[j] if j is not None else None
        entry: dict[str, Any] = {"before_index": None if i is None else i + 1,
                                 "after_index": None if j is None else j + 1}
        if before_cell is None:
            entry.update(change="added", source=after_cell["source"])
        elif after_cell is None:
            entry.update(change="removed", source=before_cell["source"])
        else:
            reasons = []
            if before_cell["source"].strip() != after_cell["source"].strip():
                reasons.append("code")
                entry["source_diff"] = _unified(before_cell["source"], after_cell["source"],
                                                "last good run", "this run")
            if after_cell["failed"] and not before_cell["failed"]:
                reasons.append("failed")
            if _normalised(before_cell["output"], volatile) != _normalised(after_cell["output"], volatile):
                reasons.append("output")
            d0, d1 = before_cell["duration"], after_cell["duration"]
            if (isinstance(d0, (int, float)) and isinstance(d1, (int, float)) and d0 > 0
                    and d1 >= d0 * SLOWER_FACTOR and d1 - d0 >= SLOWER_SECONDS):
                reasons.append("slower")
            if not reasons:
                continue
            entry.update(change="changed", reasons=reasons, source=after_cell["source"],
                         duration_before=d0, duration_after=d1)
            if "output" in reasons or "failed" in reasons:
                entry["output_before"] = lines(before_cell["output"])
                entry["output_after"] = lines(after_cell["output"])
        if after_cell and after_cell["error"]:
            entry["error"] = after_cell["error"]
        cells.append(entry)
    failed = next((c["after_index"] for c in cells if c.get("error")), None)
    return {"cells": cells, "cells_before": len(old), "cells_after": len(new), "failed_cell": failed}


# ─── The comparison ─────────────────────────────────────────────────────


def _summary(row: TaskRun | None, attempts: int) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"task_run_id": row.id, "status": row.status.value, "duration": row.duration_seconds,
            "exit_code": row.exit_code, "attempts": attempts, "error": row.error_message}


def _run_summary(run: WorkflowRun) -> dict[str, Any]:
    return {"id": run.id, "status": run.status.value, "trigger": run.trigger_type.value,
            "created_at": _aware(run.created_at), "started_at": _aware(run.started_at),
            "duration": run.duration_seconds}


def _parameters(after: dict, before: dict) -> list[dict[str, Any]]:
    changes = []
    for name in sorted(set(after) | set(before)):
        if name not in before:
            changes.append({"name": name, "change": "added", "after": after[name]})
        elif name not in after:
            changes.append({"name": name, "change": "removed", "before": before[name]})
        elif after[name] != before[name]:
            changes.append({"name": name, "change": "changed", "before": before[name], "after": after[name]})
    return changes


def compare(db: Session, run: WorkflowRun, baseline: WorkflowRun | None) -> dict[str, Any]:
    result: dict[str, Any] = {"run": _run_summary(run), "baseline": None}
    if baseline is None:
        result["note"] = "There is no successful run of this workflow before this one to compare with"
        return result
    masker = variables.masker()
    result["baseline"] = _run_summary(baseline)
    result["parameters"] = [
        {k: (masker.text(v) if isinstance(v, str) else v) for k, v in change.items()}
        for change in _parameters(run.parameters_json or {}, baseline.parameters_json or {})]
    after, before = _final_attempts(db, run), _final_attempts(db, baseline)
    ids = [run.id, baseline.id] + [row.id for row, _ in [*after.values(), *before.values()]]
    volatile = _Volatile(ids)
    tasks = []
    names = {task.id: task for task in db.scalars(select(Task).where(Task.id.in_(set(after) | set(before))))}
    for task_id in sorted(set(after) | set(before), key=lambda t: (t not in after, t)):
        task = names.get(task_id)
        a_row, a_count = after.get(task_id, (None, 0))
        b_row, b_count = before.get(task_id, (None, 0))
        entry: dict[str, Any] = {"task_id": task_id, "name": task.name if task else f"task {task_id}",
                                 "type": task.task_type.value if task else None,
                                 "after": _summary(a_row, a_count), "before": _summary(b_row, b_count)}
        # A task that never started (skipped behind a failure, cancelled in the
        # queue) has nothing to compare but its status.
        if a_row is not None and b_row is not None and a_row.started_at and b_row.started_at:
            a_cmd, b_cmd = a_row.rendered_command or "", b_row.rendered_command or ""
            if volatile.exact(a_cmd) != volatile.exact(b_cmd):
                entry["command"] = {"before": masker.text(b_cmd), "after": masker.text(a_cmd)}
            a_in, b_in = a_row.inputs_json, b_row.inputs_json
            if a_in is not None and b_in is not None:
                code = codetrack.compare(db, a_in.get("code"), b_in.get("code"), _unified, masker)
                if code:
                    entry["code"] = code
                env = _environment_changes(a_in, b_in)
                if env:
                    entry["environment"] = env
            else:
                entry["inputs_unknown"] = True
            entry["logs"] = _log_changes(a_row, b_row, volatile, masker)
            if task and task.task_type.value == "notebook":
                notebook = _notebook_changes(db, a_row, b_row, volatile, masker)
                if notebook:
                    entry["notebook"] = notebook
        tasks.append(entry)
    result["tasks"] = tasks
    return result
