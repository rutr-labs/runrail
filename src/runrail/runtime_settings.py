"""Settings a person changes from the Settings page, without a restart.

Three layers, in a fixed order. An environment variable (`RUNRAIL_X`, or the
same line in a `.env`) pins a value: the page shows it locked and says which
variable, so a deployment that sets things in its unit file stays exactly
what the unit file says. Otherwise the value last saved from the page; failing
that, the default.

Readers — the worker about to claim a run, the scheduler's retention pass, an
alert about to go out — ask `current(key)`. It is served from a per-process
cache a couple of seconds old, so a hot loop costs nothing and a separate
`runrail worker` process sees a change within that. A save from the API drops
the cache in its own process at once.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.config import get_settings
from runrail.models import AppSetting, _aware, now

#: Every kind of message RunRail can post to a webhook, in the order the
#: Settings page lists them. Alert sources tag their posts with one of these.
EVENT_KINDS = (
    "run_failed", "run_recovered", "workflow_paused",
    "approval_requested", "approval_rejected",
    "run_missed", "run_missed_recovered",
    "sla_breached", "sla_finished_late",
    "service_crash_loop",
)

_CACHE_SECONDS = 2.0


@dataclass(frozen=True)
class Definition:
    key: str
    #: The Settings field (and RUNRAIL_ variable) that pins it, if any.
    env: str | None
    default: Any
    check: Callable[[Any], Any]
    title: str
    help: str
    #: How an environment value is read when it differs from a saved one.
    from_env: Callable[[Any], Any] | None = None

    @property
    def env_var(self) -> str | None:
        return f"RUNRAIL_{self.env.upper()}" if self.env else None


def _optional_url(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("must be an http(s) URL")
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http(s) URL with a host")
    return value.rstrip("/")


def _optional_days(value: Any) -> int | None:
    """A positive number of days, or None for never. Zero is refused: the
    cleanup reads a falsy value as "off", so 0 would preview a wipe and do
    nothing."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("must be a whole number of days (at least 1), or empty for never")
    return value


def _count(low: int, high: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"must be a whole number from {low} to {high}")
        return value
    return check


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def _events(value: Any) -> list[str]:
    if not isinstance(value, list) or any(kind not in EVENT_KINDS for kind in value):
        raise ValueError(f"must be a list drawn from: {', '.join(EVENT_KINDS)}")
    return [kind for kind in EVENT_KINDS if kind in value]


def _quiet_hours(value: Any) -> dict | None:
    """{"start": "22:00", "end": "07:00", "timezone": "Europe/London"} or None.
    An end at or before the start crosses midnight, as a service window does."""
    if value in (None, {}):
        return None
    from runrail.services.windows import parse_time
    if not isinstance(value, dict):
        raise ValueError("must be an object with start, end and timezone")
    try:
        start, end = parse_time(value.get("start")), parse_time(value.get("end"))
    except ValueError as exc:
        raise ValueError(f"quiet hours: {exc}") from None
    if start == end:
        raise ValueError("quiet hours must not start and end at the same time")
    zone = value.get("timezone") or "UTC"
    if not isinstance(zone, str):
        raise ValueError("timezone must be a name like Europe/London")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(f"unknown timezone {zone!r}") from None
    return {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "timezone": zone}


DEFINITIONS: tuple[Definition, ...] = (
    Definition("notify_webhook_url", "notify_webhook_url", None, _optional_url,
               "Default webhook",
               "Where alerts go unless a workflow or service names its own. Slack and "
               "Teams incoming webhooks work as they are."),
    Definition("notify_events", None, list(EVENT_KINDS), _events, "Events to send",
               "Kinds of alert that reach the webhook. Everything still shows in Activity."),
    Definition("quiet_hours", None, None, _quiet_hours, "Quiet hours",
               "A daily span during which nothing is posted. Not deferred: what happens "
               "overnight is in Activity in the morning."),
    Definition("retention_days", "retention_days", None, _optional_days, "Keep runs for",
               "Finished runs older than this are deleted with their logs and artifacts, "
               "every six hours. The newest successful run of each workflow is always kept.",
               from_env=lambda days: days or None),  # RUNRAIL_RETENTION_DAYS=0 has always meant off
    Definition("worker_concurrency", "worker_concurrency", 4, _count(1, 64), "Concurrent runs",
               "How many runs execute at once across all workflows. Applies from the next "
               "claim; running work is never interrupted."),
    Definition("task_parallelism", "task_parallelism", 4, _count(1, 64), "Tasks per run",
               "Independent tasks of one run executing at once. Applies to runs that start "
               "after the change."),
    Definition("paused", None, False, _flag, "Paused",
               "While paused nothing new starts: queued runs wait, services are not "
               "started, and alerts about missed schedules and deadlines are held. "
               "What is executing finishes."),
)
_BY_KEY = {definition.key: definition for definition in DEFINITIONS}


def definition(key: str) -> Definition:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(f"No setting named {key!r}") from None


def pinned(definition: Definition) -> tuple[bool, Any]:
    """Whether the environment (or a .env) sets this one, and to what — run
    through the same check as a saved value, so RUNRAIL_RETENTION_DAYS=0 means
    "never" here as it does in the cleanup."""
    if definition.env is None:
        return False, None
    settings = get_settings()
    if definition.env in settings.model_fields_set:
        value = getattr(settings, definition.env)
        return True, definition.from_env(value) if definition.from_env else value
    return False, None


def describe(db: Session) -> list[dict]:
    """Every setting with its effective value and where it comes from, for the
    page: `source` is environment, saved or default; `locked` says the page
    cannot change it and `env_var` names what does."""
    saved = {row.key: row for row in db.scalars(select(AppSetting))}
    out = []
    for item in DEFINITIONS:
        locked, env_value = pinned(item)
        row = saved.get(item.key)
        if locked:
            value, source = env_value, "environment"
        elif row is not None:
            value, source = row.value, "saved"
        else:
            value, source = item.default, "default"
        out.append({"key": item.key, "title": item.title, "help": item.help, "value": value,
                    "default": item.default, "source": source, "locked": locked,
                    "env_var": item.env_var,
                    "updated_at": _aware(row.updated_at) if row is not None else None})
    return out


class SettingLocked(ValueError):
    pass


def save(db: Session, values: dict[str, Any]) -> list[dict]:
    """Validate and store the given settings; commits. ValueError names the
    first thing wrong, SettingLocked the first key the environment pins — and
    nothing is written in either case."""
    checked: dict[str, Any] = {}
    for key, value in values.items():
        item = definition(key)
        locked, _ = pinned(item)
        if locked:
            raise SettingLocked(f"{item.title} is set by {item.env_var} in the environment")
        try:
            checked[key] = item.check(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{item.title}: {exc}") from None
    stamp = now()
    for key, value in checked.items():
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value, updated_at=stamp))
        else:
            row.value, row.updated_at = value, stamp
    db.commit()
    invalidate()
    return describe(db)


# ── the hot path ───────────────────────────────────────────────────────────
_lock = threading.Lock()
_cached: dict[str, Any] = {}
_cached_at = 0.0


def current(key: str) -> Any:
    """The effective value, from a cache at most a couple of seconds old. The
    database is read outside the lock — the supervisor's tick and every alert
    pass through here, and must not queue behind a slow query — and a read
    that fails keeps the last good values rather than falling back to defaults
    (which would un-pause the worker for two seconds)."""
    global _cached, _cached_at
    with _lock:
        stale = time.monotonic() - _cached_at > _CACHE_SECONDS
    if stale:
        fresh = _load(previous=dict(_cached))
        with _lock:
            _cached, _cached_at = fresh, time.monotonic()
    with _lock:
        return _cached[key] if key in _cached else definition(key).default



def invalidate() -> None:
    global _cached_at
    with _lock:
        _cached_at = 0.0


def _load(previous: dict[str, Any]) -> dict[str, Any]:
    from runrail.db import SessionLocal
    values = {item.key: item.default for item in DEFINITIONS}
    try:
        with SessionLocal() as db:
            for row in db.scalars(select(AppSetting)):
                if row.key in _BY_KEY:
                    values[row.key] = row.value
    except Exception:  # the table is not there yet (first migration), or the DB is away
        if previous:
            values = previous
    for item in DEFINITIONS:
        locked, env_value = pinned(item)
        if locked:
            values[item.key] = env_value
    return values


def alerts_allowed(event: str | None, at: datetime | None = None) -> bool:
    """Whether a message of this kind may go out now: its kind is enabled and
    the clock is outside quiet hours. A message with no kind (a test) always may."""
    if event is None:
        return True
    if event not in current("notify_events"):
        return False
    quiet = current("quiet_hours")
    if not quiet:
        return True
    from runrail.services.windows import parse_time
    local = (at or now()).astimezone(ZoneInfo(quiet["timezone"])).time().replace(second=0,
                                                                                microsecond=0)
    start, end = parse_time(quiet["start"]), parse_time(quiet["end"])
    inside = start <= local < end if start < end else local >= start or local < end
    return not inside
