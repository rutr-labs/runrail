"""Outputs: rendered notebook reports, stable /latest URLs, and run exports."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from runrail import reports
from runrail.api.crud import get_or_404
from runrail.db import get_db
from runrail.models import Artifact, Workflow, WorkflowRun

router = APIRouter(prefix="/api", tags=["reports"])


def _fail(exc: reports.ReportError) -> JSONResponse:
    """ReportError carries a machine code the SPA branches on; HTTPException can
    only put `detail` in the body, so these are rendered by hand."""
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status)


@router.get("/runs/{run_id}/outputs")
def run_outputs(run_id: int, db: Session = Depends(get_db)):
    """What this run produced. Cheap on purpose — it never renders."""
    return reports.run_outputs(db, get_or_404(db, WorkflowRun, run_id))


@router.get("/runs/{run_id}/report")
def run_report(run_id: int, task: str | None = None, db: Session = Depends(get_db)):
    """The rendered notebook, rendering it on first view. Also the per-run
    permalink: run ids are never reused, so this URL survives a rename."""
    get_or_404(db, WorkflowRun, run_id)
    try:
        report, _ = reports.resolve_report(db, run_id, task)
    except reports.ReportError as exc:
        return _fail(exc)
    return reports.report_response(Path(report.path))


@router.post("/artifacts/{artifact_id}/render")
def rerender_artifact(artifact_id: int, db: Session = Depends(get_db)):
    """Bust the cache and render again — after upgrading nbconvert, or after a
    render that failed."""
    source = get_or_404(db, Artifact, artifact_id)
    try:
        report = reports.render_report(db, source, force=True)
    except reports.ReportError as exc:
        return _fail(exc)
    return {"id": report.id, "name": report.name, "artifact_type": report.artifact_type.value,
            "size_bytes": report.size_bytes, "created_at": report.created_at}


@router.get("/workflows/{workflow}/latest-report")
def latest_report(workflow: str, db: Session = Depends(get_db)):
    """Metadata for the pinnable link, including how stale it is."""
    try:
        return reports.latest_report_meta(db, reports.resolve_workflow(db, workflow))
    except reports.ReportError as exc:
        return _fail(exc)


@router.get("/workflows/{workflow}/latest-report/html")
def latest_report_html(workflow: str, db: Session = Depends(get_db)):
    """The raw pasteable URL. Same builder as the run report, so the two sets of
    security headers cannot drift apart."""
    try:
        meta = reports.latest_report_meta(db, reports.resolve_workflow(db, workflow))
        report, _ = reports.resolve_report(db, meta["run_id"], meta["task_name"])
    except reports.ReportError as exc:
        return _fail(exc)
    return reports.report_response(Path(report.path))


@router.get("/runs/{run_id}/export")
def export_run(run_id: int, logs: Literal["tail", "full", "none"] = "tail",
               report: bool = True,
               max_bytes: int = Query(reports.EXPORT_DEFAULT_BYTES,
                                      ge=reports.EXPORT_MIN_BYTES, le=reports.EXPORT_MAX_BYTES),
               db: Session = Depends(get_db)):
    """One self-contained HTML file, for a recipient who should not get a URL
    into the orchestrator."""
    run = get_or_404(db, WorkflowRun, run_id)
    if run.status in reports.IN_PROGRESS:
        raise HTTPException(409, "Wait for the run to finish before sharing it")
    body = reports.export_run_html(db, run, logs=logs, include_report=report,
                                   max_bytes=max_bytes)
    filename = reports.export_filename(db.get(Workflow, run.workflow_id), run)
    # attachment, never inline: the file embeds notebook-authored markup, and
    # rendering it inline would hand that a fully privileged same-origin page.
    return Response(body, media_type="text/html; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
