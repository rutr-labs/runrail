"""Variables and secrets: the list on the Settings page, and changes to it.

A secret's value never leaves the server once saved: the list says whether it
is set and whether it can still be decrypted, never what it is.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail import variables
from runrail.api.crud import get_or_404
from runrail.config import get_settings
from runrail.db import get_db
from runrail.models import Environment, Service, Variable, _aware

router = APIRouter(prefix="/api", tags=["variables"])


class VariableIn(BaseModel):
    name: str
    value: str
    secret: bool = False
    description: str | None = None


class VariableChange(BaseModel):
    name: str | None = None
    value: str | None = None
    secret: bool | None = None
    description: str | None = None


def _overrides(db: Session) -> dict[str, list[str]]:
    """Where a more specific value takes over a name, by upper-cased name."""
    found: dict[str, list[str]] = {}
    for kind, model in (("environment", Environment), ("service", Service)):
        for name, env_vars in db.execute(select(model.name, model.env_vars_json)):
            for key in env_vars or {}:
                found.setdefault(str(key).upper(), []).append(f"{kind} {name}")
    return found


def _key_state() -> dict:
    settings = get_settings()
    if settings.secrets_key:
        source = "environment"
    elif variables.key_path(settings).exists():
        source = "file"
    else:
        source = None
    state = {"source": source, "variable": variables.KEY_ENV,
             "path": str(variables.key_path(settings).resolve()),
             "sealed_to_account": source == "file" and os.name == "nt",
             "id": None, "problem": None}
    if source:
        try:
            state["id"] = variables.key_id(variables.load_key(settings))
        except variables.SecretsKeyError as exc:
            state["problem"] = str(exc)
    return state


def _out(row: Variable, unreadable: dict[str, str], overrides: dict[str, list[str]]) -> dict:
    return {
        "id": row.id, "name": row.name, "secret": row.secret,
        "value": None if row.secret else row.value,
        "description": row.description,
        "problem": unreadable.get(row.name) if row.secret else None,
        "overridden_in": overrides.get(row.name.upper(), []),
        "created_at": _aware(row.created_at), "updated_at": _aware(row.updated_at),
    }


def _listing(db: Session) -> dict:
    snapshot = variables.load(db)
    overrides = _overrides(db)
    rows = db.scalars(select(Variable).order_by(Variable.secret, Variable.name))
    return {"variables": [_out(row, snapshot.unreadable, overrides) for row in rows],
            "key": _key_state()}


@router.get("/variables")
def list_variables(db: Session = Depends(get_db)):
    return _listing(db)


def _refusal(exc: Exception) -> HTTPException:
    if isinstance(exc, variables.SecretsKeyError):
        return HTTPException(409, f"Secrets cannot be saved: {exc}")
    return HTTPException(422, str(exc))


@router.post("/variables", status_code=201)
def create_variable(body: VariableIn, db: Session = Depends(get_db)):
    try:
        row = variables.create(db, body.name, body.value, secret=body.secret,
                               description=body.description)
    except (variables.VariableError, variables.SecretsKeyError) as exc:
        db.rollback()
        raise _refusal(exc) from None
    listing = _listing(db)
    return {**listing, "saved": next(v for v in listing["variables"] if v["id"] == row.id)}


@router.patch("/variables/{object_id}")
def update_variable(object_id: int, body: VariableChange, db: Session = Depends(get_db)):
    row = get_or_404(db, Variable, object_id)
    try:
        variables.update(db, row, name=body.name, value=body.value, secret=body.secret,
                         description=body.description)
    except (variables.VariableError, variables.SecretsKeyError) as exc:
        db.rollback()
        raise _refusal(exc) from None
    return _listing(db)


@router.delete("/variables/{object_id}")
def delete_variable(object_id: int, db: Session = Depends(get_db)):
    variables.delete(db, get_or_404(db, Variable, object_id))
    return _listing(db)
