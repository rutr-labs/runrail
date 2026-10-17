"""Services: configuration, Start/Stop/Restart, history and logs.

The API never touches a service's process. It records what a person wants —
configuration, or an override from a click — and the supervisor, wherever it
runs, makes the process match within about a second. Everything a service
reports about itself (state, detail, retry time) is what the supervisor last
wrote, so the UI shows the supervisor's view, not a guess.
"""

import shutil
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.api.crud import apply_update, get_or_404, save
from runrail.db import get_db
from runrail.models import (
    Environment,
    LockMode,
    Project,
    RestartPolicy,
    Service,
    ServiceSession,
    ServiceStartMode,
    ServiceState,
    SupervisorLease,
    _aware,
    now,
)
from runrail.schemas import ServiceIn, ServiceOut, ServiceSessionOut
from runrail.services.process import log_directory
from runrail.services.supervisor import (
    LEASE_NAME,
    LEASE_STALE_AFTER,
    desired,
    log_tail,
    set_override,
)
from runrail.services.windows import next_change

router = APIRouter(prefix="/api", tags=["services"])

#: Columns whose meaning a stored override was relative to: change one and the
#: override no longer means what the person who clicked meant.
_SCHEDULE_FIELDS = {"start_mode", "windows", "timezone"}


def _out(db: Session, service: Service) -> ServiceOut:
    at = now()
    sessions = db.scalars(select(ServiceSession).where(ServiceSession.service_id == service.id)
                          .order_by(ServiceSession.id.desc()).limit(2)).all()
    current = sessions[0] if sessions and sessions[0].stopped_at is None else None
    last = next((s for s in sessions if s.stopped_at is not None), None)
    out = ServiceOut.model_validate(service)
    out.windows = service.windows_json or []
    out.should_run = desired(service, at)[0]
    out.next_change = (next_change(service.windows_json or [], service.timezone, at)
                       if service.start_mode == ServiceStartMode.schedule else None)
    out.current_session = ServiceSessionOut.model_validate(current) if current else None
    out.last_session = ServiceSessionOut.model_validate(last) if last else None
    return out


def _check_references(db: Session, values: dict) -> None:
    if values.get("project_id") is not None and db.get(Project, values["project_id"]) is None:
        raise HTTPException(404, "Project not found")
    if values.get("environment_id") is not None and db.get(Environment,
                                                            values["environment_id"]) is None:
        raise HTTPException(404, "Environment not found")


def _check_complete(service: Service) -> None:
    if service.start_mode == ServiceStartMode.schedule and not service.windows_json:
        raise HTTPException(422, "A scheduled service needs at least one window")
    if not service.lock_resource:
        service.lock_mode = LockMode.exclusive


def _name_taken(db: Session, name: str, service_id: int | None = None) -> None:
    clash = db.scalar(select(Service.id).where(Service.name == name))
    if clash is not None and clash != service_id:
        raise HTTPException(409, f"A service named '{name}' already exists")


@router.get("/services", response_model=list[ServiceOut])
def list_services(db: Session = Depends(get_db)):
    return [_out(db, service) for service in db.scalars(select(Service).order_by(Service.name))]


@router.get("/services/supervisor")
def supervisor_status(db: Session = Depends(get_db)):
    """Whether anything is supervising services. `runrail api` alone does not:
    services then hold whatever state they were last left in."""
    lease = db.get(SupervisorLease, LEASE_NAME)
    beat = _aware(lease.heartbeat_at) if lease else None
    active = beat is not None and now() - beat < LEASE_STALE_AFTER + timedelta(seconds=5)
    return {"active": active, "heartbeat_at": beat if active else None}


@router.post("/services", response_model=ServiceOut, status_code=201)
def create_service(data: ServiceIn, db: Session = Depends(get_db)):
    values = data.model_dump(exclude={"windows"})
    _check_references(db, values)
    _name_taken(db, data.name)
    if values["restart_policy"] is None:
        values["restart_policy"] = (RestartPolicy.never if data.start_mode == ServiceStartMode.manual
                                    else RestartPolicy.always)
    service = Service(**values, windows_json=data.windows, state=ServiceState.stopped,
                      restart_requested=False)
    _check_complete(service)
    return _out(db, save(db, service))


@router.get("/services/{service_id}", response_model=ServiceOut)
def get_service(service_id: int, db: Session = Depends(get_db)):
    return _out(db, get_or_404(db, Service, service_id))


@router.put("/services/{service_id}", response_model=ServiceOut)
def update_service(service_id: int, data: ServiceIn, db: Session = Depends(get_db)):
    """Changes apply to the next start; the running process keeps the command
    it was started with until it is restarted."""
    service = get_or_404(db, Service, service_id)
    values = data.model_dump(exclude_unset=True)
    _check_references(db, values)
    if "name" in values:
        _name_taken(db, values["name"], service.id)
    if "windows" in values:
        values["windows_json"] = values.pop("windows")
    if values.get("restart_policy", ...) is None:
        values.pop("restart_policy")
    rescheduled = bool(_SCHEDULE_FIELDS & data.model_fields_set)
    apply_update(service, values)
    _check_complete(service)
    # A changed schedule voids an override measured against the old one; any
    # edit lifts the supervisor's own holds (crash loop, clean exit), since
    # editing is how someone fixes what caused them.
    if rescheduled or service.override_reason in ("crash_loop", "exited"):
        service.override_running = service.override_until = service.override_reason = None
    service.next_attempt_at = None
    return _out(db, save(db, service))


@router.delete("/services/{service_id}", status_code=204)
def delete_service(service_id: int, db: Session = Depends(get_db)):
    """The supervisor stops a deleted service's process within a second, with
    its stop timeout like any stop."""
    db.delete(get_or_404(db, Service, service_id))
    db.commit()
    # Its logs go with it; nothing else would ever clean them up.
    shutil.rmtree(log_directory(service_id), ignore_errors=True)
    return Response(status_code=204)


def _action(db: Session, service_id: int, running: bool | None) -> ServiceOut:
    service = get_or_404(db, Service, service_id)
    at = now()
    if running is None:
        service.override_running = service.override_until = service.override_reason = None
    else:
        set_override(service, running, "user", at)
    # A person pressing Start means now, not after the backoff a crash earned.
    service.next_attempt_at = None
    return _out(db, save(db, service))


@router.post("/services/{service_id}/start", response_model=ServiceOut)
def start_service(service_id: int, db: Session = Depends(get_db)):
    return _action(db, service_id, True)


@router.post("/services/{service_id}/stop", response_model=ServiceOut)
def stop_service(service_id: int, db: Session = Depends(get_db)):
    return _action(db, service_id, False)


@router.post("/services/{service_id}/resume", response_model=ServiceOut)
def resume_schedule(service_id: int, db: Session = Depends(get_db)):
    """Drop any override: the service goes back to its own mode and windows."""
    return _action(db, service_id, None)


@router.post("/services/{service_id}/restart", response_model=ServiceOut)
def restart_service(service_id: int, db: Session = Depends(get_db)):
    service = get_or_404(db, Service, service_id)
    if service.state in (ServiceState.running, ServiceState.stopping, ServiceState.starting):
        service.restart_requested = True
        service.next_attempt_at = None
        return _out(db, save(db, service))
    return _action(db, service_id, True)


@router.get("/services/{service_id}/sessions", response_model=list[ServiceSessionOut])
def service_sessions(service_id: int, limit: int = Query(20, ge=1, le=200),
                     db: Session = Depends(get_db)):
    get_or_404(db, Service, service_id)
    return db.scalars(select(ServiceSession).where(ServiceSession.service_id == service_id)
                      .order_by(ServiceSession.id.desc()).limit(limit)).all()


@router.get("/services/{service_id}/log", response_class=PlainTextResponse)
def service_log(service_id: int, session_id: int | None = None,
                tail_bytes: int = Query(65536, ge=1, le=4 * 1024 * 1024),
                db: Session = Depends(get_db)):
    """The end of a session's log — the latest session unless one is named."""
    get_or_404(db, Service, service_id)
    query = select(ServiceSession).where(ServiceSession.service_id == service_id)
    if session_id is not None:
        query = query.where(ServiceSession.id == session_id)
    session = db.scalar(query.order_by(ServiceSession.id.desc()).limit(1))
    if session is None:
        raise HTTPException(404, "Session not found" if session_id is not None
                            else "This service has not run yet")
    return log_tail(session.log_path, tail_bytes)
