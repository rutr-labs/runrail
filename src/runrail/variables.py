"""Variables and secrets that every task and service gets in its environment.

Set on the Settings page. A variable is a plain value, shown and editable. A
secret is shown once, when it is entered: it is stored encrypted, reaches
tasks and services only as an environment variable, and its value is masked
wherever RunRail writes or shows output (see runrail.masking).

An environment's own variables, then a service's, are applied after these, so
the most specific value wins.

The key is a Fernet key (AES with an HMAC) in <home>/secrets.key, readable by
this user only; on Windows it is also sealed with DPAPI to this Windows
account, so a copy of the folder on another machine or account cannot open it.
RUNRAIL_SECRETS_KEY supplies the key instead. Encryption protects the database
file and its backups, which leave the key out unless asked; it does not
protect against code running as the same user, which can read the key too.
"""

import base64
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runrail.config import Settings, get_settings
from runrail.masking import MIN_LENGTH, Masker
from runrail.models import Variable

log = logging.getLogger("runrail.variables")

KEY_FILE = "secrets.key"
KEY_ENV = "RUNRAIL_SECRETS_KEY"
_DPAPI_PREFIX = b"dpapi:"

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_NAME_LENGTH = 128
MAX_VALUE_BYTES = 64 * 1024
#: Names RunRail sets itself, or that would break how tasks start.
RESERVED_NAMES = frozenset({
    "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE", "PYTHONUNBUFFERED",
    "VIRTUAL_ENV", "JUPYTER_PATH", "COMSPEC", "SYSTEMROOT",
})
RESERVED_PREFIX = "RUNRAIL_"

_CACHE_SECONDS = 2.0


class VariableError(ValueError):
    pass


class SecretsKeyError(RuntimeError):
    """The key cannot be read: a damaged file, a DPAPI seal from another
    Windows account, or a RUNRAIL_SECRETS_KEY that is not a Fernet key."""


# ─── The key ────────────────────────────────────────────────────────────


def key_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).home / KEY_FILE


def key_id(key: bytes) -> str:
    """A short name for a key, stored beside each secret, so a secret that no
    longer opens can say it was made with a different key."""
    return hashlib.sha256(key).hexdigest()[:12]


def validate_key(key: bytes, where: str) -> bytes:
    try:
        Fernet(key)
    except (ValueError, TypeError):
        raise SecretsKeyError(f"{where} is not a valid secrets key") from None
    return key


def _seal(key: bytes) -> bytes:
    """What goes in the key file: DPAPI-sealed on Windows, the key elsewhere."""
    if os.name == "nt":
        return _DPAPI_PREFIX + base64.b64encode(_dpapi(key, protect=True)) + b"\n"
    return key + b"\n"


def _unseal(raw: bytes, where: str) -> bytes:
    raw = raw.strip()
    if raw.startswith(_DPAPI_PREFIX):
        if os.name != "nt":
            raise SecretsKeyError(f"{where} was sealed to a Windows account and cannot be read here")
        try:
            raw = _dpapi(base64.b64decode(raw[len(_DPAPI_PREFIX):]), protect=False)
        except (OSError, ValueError) as exc:
            raise SecretsKeyError(f"{where} was sealed to another Windows account or machine ({exc})") from None
    return validate_key(raw, where)


def write_key_file(path: Path, key: bytes) -> None:
    """Create the key file, readable by this user only. Refuses to replace one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(_seal(key))


def load_key(settings: Settings | None = None, *, create: bool = False) -> bytes | None:
    """The secrets key, or None when there is none yet and `create` is False."""
    settings = settings or get_settings()
    if settings.secrets_key:
        return validate_key(settings.secrets_key.strip().encode(), KEY_ENV)
    path = key_path(settings)
    try:
        return _unseal(path.read_bytes(), str(path))
    except FileNotFoundError:
        pass
    if not create:
        return None
    try:
        write_key_file(path, Fernet.generate_key())
    except FileExistsError:
        pass  # another process made it first: use theirs
    return _unseal(path.read_bytes(), str(path))


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    call.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    ui_forbidden = 0x1
    if not call(ctypes.byref(blob_in), None, None, None, None, ui_forbidden, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


# ─── Checking what is saved ─────────────────────────────────────────────


def check_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise VariableError("A name is required")
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise VariableError(f"Names are at most {MAX_NAME_LENGTH} characters")
    if not NAME_PATTERN.match(name):
        raise VariableError("Use letters, digits and underscores, not starting with a digit "
                            "(it becomes an environment variable)")
    if name.upper() in RESERVED_NAMES:
        raise VariableError(f"{name} is set by RunRail or the system and cannot be overridden here")
    if name.upper().startswith(RESERVED_PREFIX):
        raise VariableError(f"Names starting with {RESERVED_PREFIX} are kept for RunRail's own settings")
    return name


def check_value(value: object, *, secret: bool) -> str:
    if not isinstance(value, str):
        raise VariableError("The value must be text")
    if "\x00" in value:
        raise VariableError("The value cannot contain a NUL character")
    if len(value.encode()) > MAX_VALUE_BYTES:
        raise VariableError(f"Values are at most {MAX_VALUE_BYTES // 1024} KB")
    if secret and len(value) < MIN_LENGTH:
        raise VariableError(f"A secret must be at least {MIN_LENGTH} characters, "
                            "or it could not be hidden in logs without hiding everything else")
    return value


def _taken(db: Session, name: str, exclude: int | None = None) -> bool:
    # Case-insensitive: on Windows DB_HOST and db_host are one variable.
    stmt = select(Variable.id).where(func.upper(Variable.name) == name.upper())
    if exclude is not None:
        stmt = stmt.where(Variable.id != exclude)
    return db.scalar(stmt) is not None


# ─── Saving ─────────────────────────────────────────────────────────────


def _encrypt(value: str) -> tuple[str, str]:
    key = load_key(create=True)
    return Fernet(key).encrypt(value.encode()).decode(), key_id(key)


def create(db: Session, name: object, value: object, *, secret: bool = False,
           description: str | None = None) -> Variable:
    name = check_name(name)
    value = check_value(value, secret=secret)
    if _taken(db, name):
        raise VariableError(f"There is already a variable called {name}")
    row = Variable(name=name, secret=secret, description=(description or "").strip() or None)
    if secret:
        row.value, row.key_id = _encrypt(value)
    else:
        row.value = value
    db.add(row)
    db.commit()
    db.refresh(row)
    invalidate()
    return row


def update(db: Session, row: Variable, *, name: object = None, value: object = None,
           secret: bool | None = None, description: object = None) -> Variable:
    """Rename, change the value, or turn a variable into a secret. A secret
    never turns back: that would show a value that was promised hidden."""
    if secret is False and row.secret:
        raise VariableError("A secret cannot be turned back into a plain variable; "
                            "delete it and add a variable instead")
    becomes_secret = bool(secret) and not row.secret
    if name is not None:
        name = check_name(name)
        if _taken(db, name, exclude=row.id):
            raise VariableError(f"There is already a variable called {name}")
        row.name = name
    if description is not None:
        row.description = str(description).strip() or None
    if value is not None or becomes_secret:
        plain = value if value is not None else row.value
        plain = check_value(plain, secret=row.secret or becomes_secret)
        if row.secret or becomes_secret:
            row.value, row.key_id = _encrypt(plain)
            row.secret = True
        else:
            row.value = plain
    db.commit()
    db.refresh(row)
    invalidate()
    return row


def delete(db: Session, row: Variable) -> None:
    db.delete(row)
    db.commit()
    invalidate()


# ─── Reading ────────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    #: What tasks and services get in their environment.
    env: dict[str, str] = field(default_factory=dict)
    masker: Masker = field(default_factory=Masker)
    #: Secrets that could not be decrypted, by name, with the reason.
    unreadable: dict[str, str] = field(default_factory=dict)


def _decrypt_all(rows: list[Variable], settings: Settings) -> tuple[dict[str, str], dict[str, str]]:
    plain: dict[str, str] = {}
    unreadable: dict[str, str] = {}
    if not any(row.secret for row in rows):
        return plain, unreadable
    try:
        key = load_key(settings)
    except SecretsKeyError as exc:
        key, problem = None, str(exc)
    else:
        problem = "there is no secrets key in this home folder"
    fernet = Fernet(key) if key else None
    current = key_id(key) if key else None
    for row in rows:
        if not row.secret:
            continue
        if fernet is None:
            unreadable[row.name] = problem
            continue
        try:
            plain[row.name] = fernet.decrypt(row.value.encode()).decode()
        except (InvalidToken, ValueError):
            unreadable[row.name] = ("it was encrypted with a different key"
                                    if row.key_id and row.key_id != current
                                    else "it does not decrypt with this home's key")
    return plain, unreadable


def load(db: Session, settings: Settings | None = None) -> Snapshot:
    settings = settings or get_settings()
    rows = list(db.scalars(select(Variable).order_by(Variable.name)))
    secrets, unreadable = _decrypt_all(rows, settings)
    env: dict[str, str] = {}
    for row in rows:
        if not row.secret:
            env[row.name] = row.value
        elif row.name in secrets:
            env[row.name] = secrets[row.name]
    return Snapshot(env, Masker(secrets.values()), unreadable)


_lock = threading.Lock()
_cached: tuple[float, Snapshot] | None = None
_reported: dict[str, str] = {}


def invalidate() -> None:
    global _cached
    with _lock:
        _cached = None


def current() -> Snapshot:
    """The variables as they are now, from a cache a couple of seconds old —
    so a separate `runrail worker` process sees a change within that. If they
    cannot be read, the last good snapshot is kept: a database blip must not
    stop masking."""
    global _cached
    with _lock:
        cached = _cached
    if cached is not None and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    from runrail.db import SessionLocal
    try:
        with SessionLocal() as db:
            snapshot = load(db)
    except Exception:
        log.warning("could not read variables; using the last ones read", exc_info=True)
        return cached[1] if cached is not None else Snapshot()
    global _reported
    if snapshot.unreadable != _reported:  # once per change, not every refresh
        for name, problem in snapshot.unreadable.items():
            log.warning("secret %s is not set for tasks: %s", name, problem)
        _reported = dict(snapshot.unreadable)
    with _lock:
        _cached = (time.monotonic(), snapshot)
    return snapshot


def masker() -> Masker:
    return current().masker


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a task or service starts from: RunRail's own, minus the
    secrets key, plus the variables and secrets."""
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name.upper() == KEY_ENV:
            del env[name]
    env.update(current().env)
    return env
