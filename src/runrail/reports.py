"""Rendered notebook reports, the stable /latest resolver, and the run export.

One module because the three surfaces are one pipeline: the security headers,
the on-disk cache, and the renderer must not drift between the run page, a
pasted /latest link, and a file someone emails outside the company.

Rendering is lazy and cached as a second Artifact row beside the .ipynb. That
is structural, not a policy: the renderer runs inside a request handler, in a
different process phase from _run_task, so a task cannot fail because
rendering failed.
"""

import importlib.util
import os
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi.responses import FileResponse
from jinja2 import Environment
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runrail import __version__
from runrail.config import get_settings
from runrail.models import (
    Artifact,
    ArtifactType,
    RunStatus,
    Task,
    TaskRun,
    TaskRunStatus,
    Workflow,
    WorkflowRun,
    _aware,
    now,
)
from runrail.worker.runners import safe_filename

_MAX_SOURCE_BYTES = 50 * 1024 * 1024
_INSTALL_HINT = ("Notebook reports need nbconvert. "
                 "Install it with: pip install 'runrail[notebook]'")

#: A rendered report is arbitrary notebook-authored HTML and JS, and RunRail has
#: no auth — anything that reaches the API succeeds. Three directives carry the
#: weight:
#:   * `sandbox` as a CSP DIRECTIVE, not just an iframe attribute, because a
#:     pasted /latest link is a top-level navigation where the attribute does
#:     nothing. No allow-same-origin: with allow-scripts that pair is equivalent
#:     to no sandbox at all, so the document gets an opaque origin and cannot
#:     read RunRail's cookies, storage, or DOM.
#:   * `connect-src 'none'` / `form-action 'none'`: an opaque origin stops a
#:     script READING the API, but a CORS-simple POST is still SENT, and
#:     /api/runs/{id}/cancel takes no body.
#:   * `img-src data: blob:` under `default-src 'none'`: matplotlib output is
#:     already base64 in the .ipynb, while <img src="https://evil/?leak"> — the
#:     classic no-JS exfiltration channel — is blocked.
#: 'unsafe-inline'/'unsafe-eval' are required by plotly/bokeh/altair and are
#: safe only because of the two directives above: the script runs, and has
#: nowhere to go.
REPORT_CSP = (
    "sandbox allow-scripts allow-popups allow-downloads; "
    "default-src 'none'; img-src data: blob:; media-src data: blob:; font-src data:; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline' 'unsafe-eval' blob:; "
    "worker-src blob:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
    "frame-ancestors 'self'"
)

REPORT_HEADERS = {
    "Content-Security-Policy": REPORT_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
}

#: Appended after nbconvert so a notebook cannot suppress it; an opaque-origin
#: frame cannot be measured by its parent, so it has to volunteer its height.
#:
#: It must measure the CONTENT, never the document element. Inside a frame,
#: documentElement.scrollHeight is at least the viewport height, so a frame
#: taller than its notebook measures its own height back: the parent sizes to
#: that, the observer sees the taller viewport, reports it again, and the frame
#: creeps upward a few pixels at a time forever. A zero-height sentinel at the
#: end of the body is placed by content flow alone and cannot see the viewport.
#: The parent keeps an echo guard of its own, because reports rendered before
#: this fix are cached on disk and still carry the old script.
_HEIGHT_REPORTER = (
    '<script>(function(){'
    'var end=document.createElement("div");'
    'end.setAttribute("aria-hidden","true");'
    'end.style.cssText="height:0;margin:0;padding:0;border:0;clear:both";'
    'var last=-1;'
    'var measure=function(){'
    'var cs=getComputedStyle(document.body);'
    'return Math.ceil(end.getBoundingClientRect().top+(window.scrollY||0)'
    '+(parseFloat(cs.paddingBottom)||0)+(parseFloat(cs.marginBottom)||0))};'
    # Post only real changes, so frame and document cannot trade rounding
    # errors back and forth.
    'var send=function(){var h=measure();'
    'if(h>0&&Math.abs(h-last)>1){last=h;'
    'parent.postMessage({runrailReportHeight:h},"*")}};'
    'var start=function(){document.body.appendChild(end);send();'
    'addEventListener("resize",send);'
    'var obs=new ResizeObserver(send);'
    'obs.observe(document.body);obs.observe(document.documentElement);'
    # Plotly, MathJax and late images settle after load. Re-measuring is cheap,
    # and a measurement that has not moved posts nothing.
    '[60,250,800,2000].forEach(function(t){setTimeout(send,t)})};'
    'if(document.readyState==="loading")addEventListener("DOMContentLoaded",start);'
    'else start();'
    'addEventListener("load",send)})()</script>'
)


_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

#: Runs whose snapshot would be a lie by the time it is read.
IN_PROGRESS = (RunStatus.queued, RunStatus.running, RunStatus.waiting_approval)

EXPORT_MIN_BYTES = 64 * 1024
EXPORT_DEFAULT_BYTES = 20 * 1024 * 1024
#: Gmail's attachment limit — the number this feature is designed against.
EXPORT_MAX_BYTES = 25 * 1024 * 1024
_LOG_STREAM_BYTES = 128 * 1024
_LOG_HEAD_BYTES = 32 * 1024
_LOG_TOTAL_BYTES = 2 * 1024 * 1024
#: Rough cost of the export's CSS, markup and timeline; reserved before logs
#: and the report are allowed to spend the caller's max_bytes.
_SHELL_BYTES = 16 * 1024
#: Measured floor of an nbconvert "lab" render — JupyterLab's inlined CSS, paid
#: even by a two-cell notebook. Only used to estimate an unrendered report.
_RENDER_FLOOR_BYTES = 280 * 1024

_TEMPLATE_PATH = Path(__file__).parent / "web" / "run_export.html.j2"

#: A success without a report is normal (a stale link is not), so staleness is
#: deliberately coarse: any failure since, or a day old. The missed-run watchdog
#: is what catches "the scheduler stopped firing".
_STALE_AFTER_SECONDS = 24 * 60 * 60


class ReportError(Exception):
    """A typed failure the SPA branches on.

    Not an HTTPException: reports.py is also called from the export path, where
    a missing renderer is a note in the file rather than a status code.
    """

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def renderer_available() -> bool:
    return importlib.util.find_spec("nbconvert") is not None


def _lock_for(artifact_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(artifact_id, threading.Lock())


def _report_path(source: Artifact) -> Path:
    # Beside the .ipynb, so it lands in artifacts/<run_id>/ and retention
    # collects it with the run — no change to cleanup, listing, or download.
    return Path(source.path).with_suffix(".html")


def notebook_outputs(db: Session, run_id: int) -> list[tuple[Artifact, TaskRun]]:
    """Every executed-notebook artifact of a run, in task order."""
    stmt = (select(Artifact, TaskRun)
            .join(TaskRun, Artifact.task_run_id == TaskRun.id)
            .join(Task, TaskRun.task_id == Task.id)
            .where(Artifact.workflow_run_id == run_id,
                   Artifact.artifact_type == ArtifactType.notebook)
            .order_by(Task.id, Artifact.id))
    return [(artifact, task_run) for artifact, task_run in db.execute(stmt)]


def pick_notebook(rows: list[tuple[Artifact, TaskRun]],
                  task: str | None = None) -> tuple[Artifact, TaskRun]:
    """Default to the lowest-Task.id notebook, matching topological_tasks' tiebreak."""
    if not rows:
        raise ReportError(404, "no_notebook", "This run produced no notebook output")
    if task is None:
        return rows[0]
    for artifact, task_run in rows:
        if task_run.task_name == task:
            return artifact, task_run
    raise ReportError(404, "no_notebook", f"No notebook output for task {task!r}")


def report_for(db: Session, source: Artifact) -> Artifact | None:
    """The cached render of *source*, if one was ever registered.

    Keyed on path rather than task_run_id: an artifact's task_run_id is
    nullable, and `== None` would match every orphan row.
    """
    return db.scalar(select(Artifact)
                     .where(Artifact.path == str(_report_path(source)),
                            Artifact.artifact_type == ArtifactType.html)
                     .order_by(Artifact.id.desc()).limit(1))


def _render_html(notebook: Path) -> str:
    # Imported inside the function, never at module scope: nbconvert is an
    # optional extra and costs ~0.3s to import, which would tax `runrail serve`
    # startup and every unrelated request.
    try:
        import nbformat
        from nbconvert import HTMLExporter
    except ImportError as exc:
        raise ReportError(503, "renderer_missing", _INSTALL_HINT) from exc
    try:
        exporter = HTMLExporter(
            # "basic" is a 1 KB fragment, but it would make RunRail own a Jupyter
            # stylesheet forever; "lab" inlines JupyterLab's own CSS (~278 KB).
            template_name="lab",
            # Measured on 7.17: the defaults emit <script src="https://cdnjs…">
            # for require.js, MathJax and jQuery. Blanking them leaves a document
            # that makes no network request the CSP would have to block.
            mathjax_url="", require_js_url="", jquery_url="",
            # Inlines ![](attachment:…) images. It reads local files relative to
            # resources.metadata.path, which is why that is pinned to the run's
            # own artifact directory rather than the server's cwd.
            embed_images=True,
            # Sanitizing runs bleach over outputs and strips the inline <script>
            # plotly/bokeh/altair emit — i.e. the interactive charts. The response
            # CSP is what makes keeping them safe.
            sanitize_html=False,
        )
        body, _ = exporter.from_notebook_node(
            nbformat.read(str(notebook), as_version=4),
            resources={"metadata": {"path": str(notebook.parent)}})
    except Exception as exc:
        raise ReportError(422, "render_failed", f"{type(exc).__name__}: {exc}") from exc
    head, marker, tail = body.rpartition("</body>")
    return f"{head}{_HEIGHT_REPORTER}{marker}{tail}" if marker else body + _HEIGHT_REPORTER


def _write_atomically(target: Path, text: str) -> None:
    """Stage then swap, so no reader ever sees a half-written report."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(f".{uuid4().hex[:8]}.part")
    try:
        staged.write_text(text, encoding="utf-8")
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def render_report(db: Session, source: Artifact, force: bool = False) -> Artifact:
    """The cached report for *source*, rendering it first if it is not on disk."""
    if source.artifact_type != ArtifactType.notebook:
        raise ReportError(409, "not_a_notebook", "Only notebook artifacts render to a report")
    notebook, target = Path(source.path), _report_path(source)
    with _lock_for(source.id):
        # Re-checked inside the lock: a concurrent request may have just finished.
        cached = report_for(db, source)
        if cached and not force and Path(cached.path).is_file():
            return cached
        if not notebook.is_file():
            raise ReportError(410, "source_removed",
                              "The notebook this report came from is no longer on disk")
        size = source.size_bytes or notebook.stat().st_size
        if size > _MAX_SOURCE_BYTES:
            raise ReportError(413, "notebook_too_large",
                              f"Notebook is {_human(size)}; the render cap is "
                              f"{_human(_MAX_SOURCE_BYTES)}")
        # Checked after the per-notebook problems, so retention deleting a source
        # does not report itself as a missing dependency. _render_html still
        # catches ImportError as the belt — find_spec succeeds on a broken install.
        if not renderer_available():
            raise ReportError(503, "renderer_missing", _INSTALL_HINT)
        _write_atomically(target, _render_html(notebook))
        report = cached or Artifact(
            task_run_id=source.task_run_id, workflow_run_id=source.workflow_run_id,
            name=target.name, artifact_type=ArtifactType.html, path=str(target))
        report.size_bytes = target.stat().st_size
        db.add(report); db.commit(); db.refresh(report)
        return report


def resolve_report(db: Session, run_id: int, task: str | None = None,
                   force: bool = False) -> tuple[Artifact, TaskRun]:
    source, task_run = pick_notebook(notebook_outputs(db, run_id), task)
    return render_report(db, source, force=force), task_run


def report_response(path: Path) -> FileResponse:
    """THE response for every rendered report, whatever resolved it.

    The per-run route, /latest and any future entry point all go through here so
    their headers cannot drift. Reports live under $RUNRAIL_HOME/artifacts, which
    is deliberately not mounted as static: the SPA catch-all serves anything
    under web/static with no CSP at all.
    """
    return FileResponse(path, media_type="text/html", headers=REPORT_HEADERS)


def resolve_workflow(db: Session, reference: str) -> Workflow:
    """Accept an id or an exact name.

    All-digits resolves as an id first, so a workflow literally named "42" is
    reachable by id only — documented corner, not a bug.
    """
    workflow = db.get(Workflow, int(reference)) if reference.isdigit() else None
    workflow = workflow or db.scalar(select(Workflow).where(Workflow.name == reference))
    if not workflow:
        raise ReportError(404, "no_such_workflow", f"No workflow named {reference!r}")
    return workflow


def latest_report_meta(db: Session, workflow: Workflow) -> dict:
    """Metadata for the newest successful run that actually HAS a notebook.

    Not simply the newest successful run: a run whose notebook task was skipped,
    or that predates the notebook task being added, would resolve to an empty
    page — a pinned link breaking for reasons the reader cannot see. When the
    chosen run is not the newest success, newer_successful_run_id says so.
    """
    run = db.scalar(select(WorkflowRun)
                    .join(Artifact, Artifact.workflow_run_id == WorkflowRun.id)
                    .where(WorkflowRun.workflow_id == workflow.id,
                           WorkflowRun.status == RunStatus.success,
                           Artifact.artifact_type == ArtifactType.notebook)
                    .order_by(WorkflowRun.id.desc()).limit(1))
    if run is None:
        succeeded = db.scalar(select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow.id,
            WorkflowRun.status == RunStatus.success)) or 0
        if succeeded:
            raise ReportError(404, "no_report_in_any_successful_run",
                              f"{workflow.name} has succeeded, but no successful run "
                              "produced a notebook output")
        raise ReportError(404, "no_successful_run", f"{workflow.name} has not succeeded yet")

    source, task_run = pick_notebook(notebook_outputs(db, run.id))
    finished = _aware(run.finished_at) or _aware(run.created_at)
    age = int((now() - finished).total_seconds())
    failed_since = db.scalar(select(func.count()).select_from(WorkflowRun).where(
        WorkflowRun.workflow_id == workflow.id, WorkflowRun.id > run.id,
        WorkflowRun.status == RunStatus.failed)) or 0
    newer = db.scalar(select(WorkflowRun.id).where(
        WorkflowRun.workflow_id == workflow.id, WorkflowRun.id > run.id,
        WorkflowRun.status == RunStatus.success).order_by(WorkflowRun.id.desc()).limit(1))
    return {
        "workflow_id": workflow.id, "workflow_name": workflow.name,
        "run_id": run.id, "run_finished_at": finished, "trigger_type": run.trigger_type.value,
        "task_name": task_run.task_name,
        "report_url": f"/api/workflows/{workflow.id}/latest-report/html",
        # The API permalink, not an SPA route: run ids are never reused, so this
        # URL is stable for the life of the row and survives a workflow rename.
        "permalink": f"/api/runs/{run.id}/report",
        "notebook_artifact_id": source.id,
        # A /latest link showing month-old numbers with no signal is worse than a
        # broken one, so staleness is part of the payload, not a UI nicety.
        "stale": failed_since > 0 or age > _STALE_AFTER_SECONDS,
        "age_seconds": age, "failed_since": failed_since,
        "newer_successful_run_id": newer, "workflow_enabled": workflow.enabled,
    }


def _human(size: float) -> str:
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GB"


def _file_size(path: str | None) -> int:
    if not path:
        return 0
    file = Path(path)
    return file.stat().st_size if file.is_file() else 0


def _read_log(path: str | None, limit: int, link: str) -> tuple[str, int]:
    """Return (text, bytes charged to the budget), tail-weighted.

    The traceback is at the end, but "what did it start doing" is the other
    question you ask, so a head slice is kept. Read by seek rather than
    slurp — the same technique log_response uses, so a 4 GB log never lands in
    API memory.
    """
    if not path:
        return "", 0
    file = Path(path)
    if not file.is_file():
        return "(log file no longer available)", 0
    size = file.stat().st_size
    if size == 0:
        return "", 0
    if limit <= 0:
        return f"… {_human(size)} elided — no size budget left. Full log at {link} …", 0
    if size <= limit:
        return file.read_text(errors="replace"), size
    head_bytes = min(_LOG_HEAD_BYTES, limit // 4)
    tail_bytes = limit - head_bytes
    with file.open("rb") as handle:
        head = handle.read(head_bytes)
        handle.seek(size - tail_bytes)
        tail = handle.read()
    marker = (f"\n\n… {_human(size - head_bytes - tail_bytes)} elided "
              f"— full log at {link} …\n\n")
    return (head.decode("utf-8", "replace") + marker + tail.decode("utf-8", "replace"),
            head_bytes + tail_bytes)


def _log_sections(task_runs, mode: str, budget: int) -> dict[int, dict[str, str]]:
    """Budget allocated to failed tasks first, then run order.

    You share a run because something broke, so the failed task's output is the
    payload. `full` lifts the per-stream cap; the total budget still applies.
    """
    if mode == "none":
        return {}
    base = get_settings().base_url
    remaining, sections = budget, {}
    for task_run in sorted(task_runs, key=lambda tr: (tr.status != TaskRunStatus.failed, tr.id)):
        streams = {}
        for label, attr in (("stdout", "stdout_log_path"), ("stderr", "stderr_log_path")):
            cap = remaining if mode == "full" else min(_LOG_STREAM_BYTES, remaining)
            text, used = _read_log(getattr(task_run, attr), cap,
                                   f"{base}/api/task-runs/{task_run.id}/{label}")
            remaining -= used
            if text:
                streams[label] = text
        if streams:
            sections[task_run.id] = streams
    return sections


def _timeline(run: WorkflowRun, task_runs) -> dict:
    """Geometry for the frozen dot-rail: one row per task run, precomputed.

    The live comet is deliberately absent — it means *executing*, and an export
    is a snapshot of something already finished.
    """
    stamps = [_aware(tr.started_at) for tr in task_runs if tr.started_at]
    origin = min(stamps) if stamps else _aware(run.created_at)
    ends = [_aware(tr.finished_at) for tr in task_runs if tr.finished_at] or [origin]
    span = max((max(ends) - origin).total_seconds(), 0.001)
    rows = []
    for index, task_run in enumerate(task_runs):
        started = _aware(task_run.started_at) or origin
        finished = _aware(task_run.finished_at) or started
        left = 260 + (started - origin).total_seconds() / span * 720
        width = max((finished - started).total_seconds() / span * 720, 6)
        width = min(width, 980 - left)
        dots = [round(left + 5 + step * 13, 1)
                for step in range(max(1, min(int((width - 6) // 13) + 1, 55)))]
        rows.append({
            "y": 18 + index * 26, "label": task_run.task_name or f"task {task_run.task_id}",
            "status": task_run.status.value, "x": round(left, 1), "width": round(width, 1),
            "dots": [d for d in dots if d <= left + width - 3] or [round(left + 3, 1)],
        })
    return {"rows": rows, "height": 24 + len(rows) * 26}


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest}s"


def _stamp(value: datetime | None) -> str:
    value = _aware(value)
    return value.strftime("%d %b %Y %H:%M:%S UTC") if value else "—"


def run_outputs(db: Session, run: WorkflowRun) -> dict:
    """Cheap inventory of what a run produced. Never renders."""
    workflow = db.get(Workflow, run.workflow_id)
    task_runs = db.scalars(select(TaskRun).where(TaskRun.workflow_run_id == run.id)
                           .order_by(TaskRun.id)).all()
    reports, report_bytes = [], 0
    for source, task_run in notebook_outputs(db, run.id):
        cached = report_for(db, source)
        rendered = bool(cached and Path(cached.path).is_file())
        # An unrendered report costs at least the lab template's inlined CSS,
        # plus roughly whatever the notebook's own outputs weigh.
        report_bytes += ((cached.size_bytes or 0) if rendered
                         else _RENDER_FLOOR_BYTES + (source.size_bytes or 0))
        reports.append({
            "task_run_id": task_run.id, "task_name": task_run.task_name,
            "notebook_artifact_id": source.id, "notebook_name": source.name,
            "notebook_bytes": source.size_bytes,
            "report_artifact_id": cached.id if cached else None,
            "report_bytes": cached.size_bytes if rendered else None,
            "rendered": rendered,
            "report_url": (f"/api/runs/{run.id}/report"
                           f"?task={quote(task_run.task_name or '')}"),
        })
    logs = [{"task_run_id": tr.id, "task_name": tr.task_name,
             "stdout_bytes": _file_size(tr.stdout_log_path),
             "stderr_bytes": _file_size(tr.stderr_log_path)} for tr in task_runs]
    log_bytes = min(_LOG_TOTAL_BYTES, sum(entry["stdout_bytes"] + entry["stderr_bytes"]
                                          for entry in logs))
    return {
        "run_id": run.id, "workflow_name": workflow.name if workflow else None,
        "status": run.status.value, "reports": reports, "logs": logs,
        "renderer_available": renderer_available(),
        "estimated_export_bytes": {
            "with_report": _SHELL_BYTES + log_bytes + report_bytes,
            "without_report": _SHELL_BYTES + log_bytes,
            "logs_none": _SHELL_BYTES + report_bytes,
        },
    }


@lru_cache(maxsize=1)
def _export_template():
    # autoescape is EXPLICIT because Jinja's default is False and every value
    # interpolated here is attacker-influenced: task names, rendered commands,
    # error messages, and log text full of </script>.
    return Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True).from_string(
        _TEMPLATE_PATH.read_text(encoding="utf-8"))


def export_filename(workflow: Workflow | None, run: WorkflowRun) -> str:
    # safe_filename keeps a workflow named ../../etc/passwd from shaping the
    # Content-Disposition header.
    return f"runrail-{safe_filename(workflow.name if workflow else 'run')}-run-{run.id}.html"


def export_run_html(db: Session, run: WorkflowRun, logs: str = "tail",
                    include_report: bool = True,
                    max_bytes: int = EXPORT_DEFAULT_BYTES) -> str:
    """One self-contained HTML file: no stylesheet, script, font or image from
    the network, and nothing that needs RunRail to be reachable."""
    workflow = db.get(Workflow, run.workflow_id)
    task_runs = db.scalars(select(TaskRun).where(TaskRun.workflow_run_id == run.id)
                           .order_by(TaskRun.id)).all()
    settings = get_settings()
    run_url = f"{settings.base_url}/runs/{run.id}"

    budget = max(0, min(_LOG_TOTAL_BYTES, max_bytes - _SHELL_BYTES))
    sections = _log_sections(task_runs, logs, budget)
    spent = sum(len(text) for streams in sections.values() for text in streams.values())

    report_html, report_note = None, None
    if include_report:
        report_html, report_note = _export_report(
            db, run, max_bytes - _SHELL_BYTES - spent, max_bytes, run_url)

    return _export_template().render(
        run=run, workflow=workflow, task_runs=task_runs, sections=sections,
        timeline=_timeline(run, task_runs), report_html=report_html, report_note=report_note,
        exported_at=_stamp(now()), run_url=run_url, version=__version__,
        started=_stamp(run.started_at), finished=_stamp(run.finished_at),
        duration=_duration(run.duration_seconds), stamp=_stamp, fmt_duration=_duration)


def _export_report(db: Session, run: WorkflowRun, room: int, max_bytes: int,
                   run_url: str) -> tuple[str | None, str | None]:
    """The rendered report, or a note saying honestly why it is not here.

    Dropped whole rather than truncated: half a notebook is invalid HTML, and a
    recipient cannot tell the difference until it renders wrong.
    """
    try:
        report, _ = resolve_report(db, run.id)
    except ReportError as exc:
        return None, None if exc.code == "no_notebook" else exc.detail
    text = Path(report.path).read_text(encoding="utf-8", errors="replace")
    # srcdoc escaping inflates by roughly 2%; charge for it before committing.
    if len(text) * 1.05 > room:
        return None, (f"The notebook report ({_human(len(text))}) was left out to keep this "
                      f"file under {_human(max_bytes)}. Open it at {run_url}.")
    return text, None
