from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.api.crud import apply_update, ensure_environment_ready, get_or_404, save
from runrail.config import get_settings
from runrail.db import get_db
from runrail.environments import (
    managed_environment_path,
    remove_managed_root,
    validate_external,
    validate_package_specs,
)
from runrail.models import Environment, EnvironmentStatus, EnvironmentType, Project
from runrail.schemas import (
    EnvironmentIn,
    EnvironmentOut,
    EnvironmentRebuild,
    EnvironmentUpdate,
    ProjectIn,
    ProjectOut,
)

router = APIRouter(prefix="/api")


@router.get("/filesystem")
def browse_filesystem(
    path: str | None = None,
    mode: str = Query("all", pattern="^(all|files|directories)$"),
):
    """Browse the configured local root for project and task path selection."""
    root = get_settings().browse_root.expanduser().resolve()
    target = Path(path).expanduser().resolve() if path else root
    if target != root and root not in target.parents:
        raise HTTPException(403, f"Path must be inside {root}")
    if not target.is_dir():
        raise HTTPException(404, "Directory not found")
    try:
        children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    except PermissionError as exc:
        raise HTTPException(403, "Directory cannot be read") from exc
    entries = []
    for item in children:
        try:
            is_dir = item.is_dir()
        except OSError:
            continue
        if item.name.startswith("."):
            continue
        if mode == "directories" and not is_dir:
            continue
        entries.append({"name": item.name, "path": str(item), "is_directory": is_dir})
    return {
        "root": str(root),
        "path": str(target),
        "parent": str(target.parent) if target != root else None,
        "entries": entries,
    }


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    return db.scalars(select(Project).order_by(Project.name)).all()


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(data: ProjectIn, db: Session = Depends(get_db)):
    ensure_environment_ready(db, data.default_environment_id)
    return save(db, Project(**data.model_dump()))


@router.get("/projects/{object_id}", response_model=ProjectOut)
def get_project(object_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, Project, object_id)


@router.put("/projects/{object_id}", response_model=ProjectOut)
def update_project(object_id: int, data: ProjectIn, db: Session = Depends(get_db)):
    ensure_environment_ready(db, data.default_environment_id)
    return save(db, apply_update(get_or_404(db, Project, object_id), data.model_dump()))


@router.delete("/projects/{object_id}", status_code=204)
def delete_project(object_id: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Project, object_id)); db.commit()
    return Response(status_code=204)


@router.get("/environments", response_model=list[EnvironmentOut])
def list_environments(db: Session = Depends(get_db)):
    return db.scalars(select(Environment).order_by(Environment.name)).all()


@router.post("/environments", response_model=EnvironmentOut, status_code=201)
def create_environment(data: EnvironmentIn, db: Session = Depends(get_db)):
    if db.scalar(select(Environment.id).where(Environment.name == data.name)) is not None:
        raise HTTPException(409, "An environment with that name already exists")
    if data.create_venv and data.env_type != EnvironmentType.python:
        raise HTTPException(400, "Managed environments must use the Python environment type")
    packages = data.packages
    if data.create_venv:
        try:
            packages = validate_package_specs(data.packages)  # fail fast, not mid-build
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    values = data.model_dump(exclude={"create_venv", "packages", "base_executable"})
    environment = Environment(
        **values,
        managed=data.create_venv,
        status=EnvironmentStatus.creating if data.create_venv else EnvironmentStatus.ready,
        base_executable=data.base_executable,
        packages_json=packages,
    )
    if data.create_venv:
        return save(db, environment)
    try:
        validate_external(environment)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return save(db, environment)


@router.get("/environments/{object_id}", response_model=EnvironmentOut)
def get_environment(object_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, Environment, object_id)


@router.put("/environments/{object_id}", response_model=EnvironmentOut)
def update_environment(object_id: int, data: EnvironmentUpdate, db: Session = Depends(get_db)):
    environment = get_or_404(db, Environment, object_id)
    if environment.status in (EnvironmentStatus.creating, EnvironmentStatus.building):
        raise HTTPException(409, "Environment cannot be edited while a build is in progress")
    values = data.model_dump(exclude_unset=True)
    if values.get("env_type") is None:
        values.pop("env_type", None)
    if environment.managed:
        values = {
            key: value for key, value in values.items()
            if key in {"name", "description", "env_vars_json"}
        }
    apply_update(environment, values)
    if environment.managed:
        return save(db, environment)
    try:
        validate_external(environment)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return save(db, environment)


@router.post("/environments/{object_id}/rebuild", response_model=EnvironmentOut)
def rebuild_environment(
    object_id: int, data: EnvironmentRebuild, db: Session = Depends(get_db)
):
    environment = get_or_404(db, Environment, object_id)
    if not environment.managed:
        raise HTTPException(400, "Only managed environments can be rebuilt")
    if environment.status in (EnvironmentStatus.creating, EnvironmentStatus.building):
        raise HTTPException(409, "An environment build is already in progress")
    if data.packages is not None:
        try:
            environment.packages_json = validate_package_specs(data.packages)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if data.base_executable is not None:
        environment.base_executable = data.base_executable
    environment.status = EnvironmentStatus.creating
    environment.last_error = None
    return save(db, environment)


@router.post("/environments/{object_id}/validate", response_model=EnvironmentOut)
def validate_environment(object_id: int, db: Session = Depends(get_db)):
    environment = get_or_404(db, Environment, object_id)
    if environment.managed:
        if environment.status not in (EnvironmentStatus.ready, EnvironmentStatus.degraded) or not environment.executable:
            raise HTTPException(400, environment.last_error or "Environment is not ready")
        return environment
    try:
        validate_external(environment)
    except ValueError as exc:
        environment.status = EnvironmentStatus.failed
        environment.last_error = str(exc)
        db.commit()
        raise HTTPException(400, str(exc)) from exc
    return save(db, environment)


@router.delete("/environments/{object_id}", status_code=204)
def delete_environment(object_id: int, db: Session = Depends(get_db)):
    environment = get_or_404(db, Environment, object_id)
    if environment.status in (EnvironmentStatus.creating, EnvironmentStatus.building):
        raise HTTPException(409, "Environment cannot be removed while a build is in progress")
    # Resolve the on-disk location before the ORM object is invalidated by the delete.
    managed_root = managed_environment_path(environment) if environment.managed else None
    db.delete(environment); db.commit()
    if managed_root is not None:
        remove_managed_root(managed_root)
    return Response(status_code=204)
