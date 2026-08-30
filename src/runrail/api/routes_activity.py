"""The notification bell: recent noteworthy events and the badge counts."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from runrail import activity
from runrail.db import get_db

router = APIRouter(prefix="/api", tags=["activity"])


@router.get("/activity")
def activity_feed(
    limit: int = Query(activity.DEFAULT_LIMIT, ge=1, le=activity.MAX_LIMIT),
    window_hours: int = Query(activity.DEFAULT_WINDOW_HOURS, ge=1, le=activity.MAX_WINDOW_HOURS),
    read_at: datetime | None = None,
    db: Session = Depends(get_db),
):
    """Events newest first, each carrying the run or workflow it links to.

    `read_at` is the client's own last-read instant — it lives in the browser,
    because unread is a per-viewer notion and this is a single-user app, and
    sending it up costs one query parameter instead of a column and a write on
    every poll. `unread` counts the in-window events newer than it, so the badge
    never has to fetch the list; with no `read_at` the whole window is unread,
    and the client stamps `generated_at` the first time the panel is opened.
    """
    return activity.recent_events(db, limit=limit, window_hours=window_hours, read_at=read_at)
