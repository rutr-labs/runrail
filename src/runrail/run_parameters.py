"""A workflow's run form: the parameters it takes, their types and defaults.

A workflow can list its parameters — a date, a choice, a number, yes/no, or
text — each with an optional default and a line of help. "Run with…" then
shows a form instead of a JSON box, and every way a run starts (the button,
the API, the CLI, the schedule, a backfill) goes through `resolve`: values
given are checked and converted, missing ones take their default, and the
run records exactly what it ran with.

Date defaults can be relative: "today" or "yesterday", counted on the
workflow's own calendar — or, in a backfill, from the day being backfilled.

Parameters nobody declared still pass through untouched, so a workflow with
no form, or a caller sending extra values, works exactly as before.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TYPES = ("text", "number", "date", "choice", "bool")
RELATIVE_DATES = {"today": 0, "yesterday": -1}
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Template variables RunRail sets itself. `ds` is not here: declaring it as
#: a date is how a workflow asks "which day?" up front.
RESERVED = frozenset({"ds_utc", "ts", "ts_nodash", "parameters", "run_id", "workflow_run_id",
                      "task_run_id", "project_root", "artifacts_dir"})
MAX_PARAMETERS = 50
MAX_CHOICES = 100
MAX_TEXT = 2000
_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off"}


class ParameterError(ValueError):
    pass


# ─── Definitions ────────────────────────────────────────────────────────


def _text(value: Any, field: str, name: str, limit: int = 500) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ParameterError(f"{name}: {field} must be text")
    value = value.strip()
    if len(value) > limit:
        raise ParameterError(f"{name}: {field} is at most {limit} characters")
    return value or None


def check_definitions(raw: Any) -> list[dict[str, Any]] | None:
    """The run form as saved: a list of parameters, each checked, with its
    default converted to its type. None or [] is no form."""
    if raw in (None, []):
        return None
    if not isinstance(raw, list):
        raise ParameterError("The run form must be a list of parameters")
    if len(raw) > MAX_PARAMETERS:
        raise ParameterError(f"A run form has at most {MAX_PARAMETERS} parameters")
    seen: set[str] = set()
    checked = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ParameterError("Each parameter must be a mapping with a name and a type")
        name = entry.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.match(name.strip()):
            raise ParameterError(f"Parameter name {name!r}: use letters, digits and underscores, "
                                 "not starting with a digit (templates refer to it as {{ name }})")
        name = name.strip()
        if name in RESERVED:
            raise ParameterError(f"{name} is set by RunRail for every run and cannot be a parameter")
        if name in seen:
            raise ParameterError(f"{name} is in the run form twice")
        seen.add(name)
        kind = entry.get("type", "text")
        if kind not in TYPES:
            raise ParameterError(f"{name}: type must be one of {', '.join(TYPES)}")
        if name == "ds" and kind != "date":
            raise ParameterError("ds is the run's date; declared in the form it must be a date")
        item: dict[str, Any] = {"name": name, "type": kind}
        for field in ("label", "help"):
            text = _text(entry.get(field), field, name)
            if text:
                item[field] = text
        if kind == "choice":
            choices = entry.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ParameterError(f"{name}: a choice needs at least one option")
            options = [str(c).strip() for c in choices if str(c).strip()]
            if len(options) != len(set(options)):
                raise ParameterError(f"{name}: an option is listed twice")
            if not options or len(options) > MAX_CHOICES:
                raise ParameterError(f"{name}: a choice has between 1 and {MAX_CHOICES} options")
            item["choices"] = options
        if entry.get("required"):
            item["required"] = True
        default = entry.get("default")
        if default not in (None, ""):
            if kind == "date" and isinstance(default, str) and default.strip().lower() in RELATIVE_DATES:
                item["default"] = default.strip().lower()
            else:
                item["default"] = _convert(item, default)
        checked.append(item)
    return checked or None


def needs_value(definitions: list[dict[str, Any]] | None) -> list[str]:
    """Required parameters with no default: a run cannot start without being
    told them, so a schedule cannot start one at all."""
    return [d["name"] for d in definitions or () if d.get("required") and "default" not in d]


# ─── Values ─────────────────────────────────────────────────────────────


def _convert(definition: dict[str, Any], value: Any) -> Any:
    name, kind = definition["name"], definition["type"]
    if kind == "text":
        if isinstance(value, (dict, list)):
            raise ParameterError(f"{name}: must be text")
        text = str(value)
        if len(text) > MAX_TEXT:
            raise ParameterError(f"{name}: at most {MAX_TEXT} characters")
        return text
    if kind == "number":
        if isinstance(value, bool):
            raise ParameterError(f"{name}: must be a number")
        try:
            number = float(value) if not isinstance(value, (int, float)) else value
        except (TypeError, ValueError):
            raise ParameterError(f"{name}: {value!r} is not a number") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ParameterError(f"{name}: must be a finite number")
        return int(number) if float(number).is_integer() else float(number)
    if kind == "bool":
        if isinstance(value, bool):
            return value
        word = str(value).strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        raise ParameterError(f"{name}: must be yes or no")
    if kind == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError:
            raise ParameterError(f"{name}: {value!r} is not a date (YYYY-MM-DD)") from None
    if kind == "choice":
        option = str(value).strip()
        if option not in definition["choices"]:
            raise ParameterError(f"{name}: must be one of {', '.join(definition['choices'])}")
        return option
    raise ParameterError(f"{name}: unknown type {kind}")


def local_today(zone: str | None, at: datetime | None = None) -> date:
    moment = at or datetime.now(timezone.utc)
    try:
        return moment.astimezone(ZoneInfo(zone or "UTC")).date()
    except (ZoneInfoNotFoundError, ValueError):
        return moment.astimezone(timezone.utc).date()


def _default(definition: dict[str, Any], day: date) -> Any:
    default = definition["default"]
    if definition["type"] == "date" and default in RELATIVE_DATES:
        return (day + timedelta(days=RELATIVE_DATES[default])).isoformat()
    return default


def resolve(definitions: list[dict[str, Any]] | None, given: dict[str, Any] | None, *,
            day: date, strict: bool = True) -> dict[str, Any]:
    """The values a run starts with. `day` anchors relative date defaults.
    Declared parameters are converted to their type, missing ones take their
    default; a required one with neither is refused unless not `strict`
    (the schedule, which has nobody to ask). Undeclared values pass through."""
    values = dict(given or {})
    for definition in definitions or ():
        name = definition["name"]
        if name in values and values[name] not in (None, ""):
            values[name] = _convert(definition, values[name])
            continue
        values.pop(name, None)
        if "default" in definition:
            values[name] = _default(definition, day)
        elif definition.get("required") and strict:
            raise ParameterError(f"{definition.get('label') or name} is required")
    return values
