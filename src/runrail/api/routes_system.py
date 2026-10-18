"""Settings and system: what the Settings page reads and changes.

Settings are validated and stored by runrail.runtime_settings, which also
tells the page which ones the environment pins. The system endpoints never
block on anything slow — storage figures and the update check come from
background measurements with their age attached.
"""

import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from runrail import notify, runtime_settings, system
from runrail.db import get_db
from runrail.models import now

router = APIRouter(prefix="/api", tags=["system"])

#: A backup upload larger than this is refused before it is read in full.
MAX_RESTORE_BYTES = 512 * 1024 * 1024


@router.get("/settings")
def list_settings(db: Session = Depends(get_db)):
    return {"settings": runtime_settings.describe(db),
            "event_kinds": list(runtime_settings.EVENT_KINDS)}


@router.put("/settings")
def update_settings(values: dict[str, Any], db: Session = Depends(get_db)):
    """Save the given settings. All or nothing: the first invalid value (422)
    or environment-pinned key (409) stops the whole save."""
    try:
        for key in values:
            runtime_settings.definition(key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None
    try:
        saved = runtime_settings.save(db, values)
    except runtime_settings.SettingLocked as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"settings": saved, "event_kinds": list(runtime_settings.EVENT_KINDS)}


@router.post("/settings/notify-test")
def test_default_webhook(db: Session = Depends(get_db)):
    """Post a test message to the default webhook now, policy or no policy."""
    url = runtime_settings.current("notify_webhook_url")
    if not url:
        raise HTTPException(400, "No default webhook is set")
    error = notify.deliver(url, "🔔 RunRail: test message from Settings.", event="test")
    return {"ok": error is None, "error": error, "url_host": urlsplit(url).hostname}


@router.get("/system/status")
def system_status(refresh: bool = False, db: Session = Depends(get_db)):
    return system.status(db, refresh=refresh)


class PauseIn(BaseModel):
    paused: bool


@router.post("/system/pause")
def set_paused(data: PauseIn, db: Session = Depends(get_db)):
    """Pause (or resume) everything new: queued runs wait and services are not
    started until resumed; what is executing finishes. The status reply says
    how much is still draining."""
    try:
        runtime_settings.save(db, {"paused": data.paused})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return system.status(db)


@router.get("/system/retention-preview")
def retention_preview(days: int = Query(..., ge=0), db: Session = Depends(get_db)):
    return system.retention_preview(db, days)


@router.get("/system/backup")
def download_backup(background: BackgroundTasks, db: Session = Depends(get_db)):
    """A zip of the configuration (YAML), the SQLite database and a manifest."""
    handle = tempfile.NamedTemporaryFile(prefix="runrail-backup-", suffix=".zip", delete=False)
    handle.close()
    path = Path(handle.name)
    try:
        system.write_backup(db, path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    background.add_task(path.unlink, missing_ok=True)
    stamp = now().strftime("%Y%m%d-%H%M%S")
    return FileResponse(path, media_type="application/zip",
                        filename=f"runrail-backup-{stamp}.zip")


@router.get("/system/restore-points")
def restore_points():
    return {"points": system.list_restore_points(), "directory": str(system.backups_dir())}


async def _upload(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length and int(length) > MAX_RESTORE_BYTES:
        raise HTTPException(413, "That backup is too large to restore through the browser; "
                                 "use `runrail restore` on the machine")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Send the backup zip as the request body")
    return data


@router.post("/system/restore/preview")
async def restore_preview(request: Request):
    data = await _upload(request)
    try:
        return system.inspect_backup(data)
    except system.BackupError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/system/restore")
async def restore(request: Request, db: Session = Depends(get_db)):
    """Apply the backup's configuration. The database file inside it is not
    touched from here: that is `runrail restore <zip>` with the server stopped."""
    data = await _upload(request)
    try:
        summary = system.restore_configuration(db, data)
    except system.BackupError as exc:
        raise HTTPException(422, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, f"The configuration could not be applied: {exc}") from None
    return {"summary": summary}
