"""Which code a task ran, kept so two runs can be compared file by file.

Scripts live in two kinds of place, and each is tracked the way it is used:

- **A project**: a RunRail project, a git repository, or a folder marked by
  pyproject.toml / setup.py / setup.cfg. Every code and config file in it is
  tracked, because a fix lands as often in a helper or a config as in the
  script that runs. In a git repository the file list is git's own, so
  ignored files and virtualenvs stay out.
- **A loose script** in a shared folder: the script and the local files it
  imports or names (`open("settings.yaml")`), followed from file to file —
  not its unrelated neighbours.

In both, the files the task actually uses are worked out the same way, so a
comparison can put them first and fold away an edit somewhere else in a big
repository.

Storage is content-addressed, like git's objects. A file's text is a blob,
stored once, compressed, however many runs read it; a manifest lists every
tracked file with its blob, and is itself stored once; a task run points at a
manifest. A project nobody touched between two runs costs nothing. A blob or
manifest lives exactly as long as some run that is still kept points at it
(`collect_garbage`, run after retention), never by age.

Secrets stay out: .env files, keys and credential files are never read, and
anything shown is masked.
"""

import ast
import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runrail.models import CodeBlob, CodeManifest, TaskRun

log = logging.getLogger("runrail.codetrack")

#: What counts as code or configuration.
CODE_SUFFIXES = frozenset({
    ".py", ".sql", ".ipynb", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".r",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".json",
})
CODE_NAMES = frozenset({"requirements.txt", "dockerfile", "makefile", "pipfile"})
#: Never read, whatever the folder: they hold credentials, not code.
SECRET_PATTERNS = (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
                   "id_rsa*", "id_ed25519*", "id_ecdsa*", "*credential*", "*secret*",
                   "*.kdbx", ".netrc", ".pgpass", "*token*")
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "node_modules", "__pycache__",
    ".ipynb_checkpoints", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
    "build", "dist", ".idea", ".vscode", "site-packages", ".runrail", "logs",
})
PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")
#: A file larger than this is tracked by fingerprint only: "changed", no lines.
MAX_FILE_BYTES = 512 * 1024
#: A folder with more tracked files than this is not a project to snapshot
#: whole (a home directory, a shared drive); the task's own files are tracked.
MAX_PROJECT_FILES = 5000
MAX_FOLLOWED = 300
GIT_TIMEOUT = 5


# ─── Reading files, cheaply ─────────────────────────────────────────────


def _secret(name: str) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, pattern) for pattern in SECRET_PATTERNS)


def _code_file(path: Path) -> bool:
    name = path.name.lower()
    return (path.suffix.lower() in CODE_SUFFIXES or name in CODE_NAMES) and not _secret(name)


def _notebook_code(data: bytes) -> str:
    """A notebook's code cells as text: what changes when its code does, not
    when it was last run and saved with fresh outputs."""
    try:
        cells = json.loads(data.decode("utf-8")).get("cells") or []
    except (UnicodeDecodeError, ValueError, AttributeError):
        return data.decode("utf-8", errors="replace")
    parts = []
    for number, cell in enumerate((c for c in cells if c.get("cell_type") == "code"), 1):
        source = cell.get("source")
        source = "".join(source) if isinstance(source, list) else str(source or "")
        parts.append(f"# ── cell {number} ──\n{source.rstrip()}\n")
    return "\n".join(parts)


@dataclass
class _Read:
    sha: str
    size: int
    #: Small enough and text: its lines can be kept and shown.
    textual: bool
    #: The text, when it was read just now (not served from the stat cache).
    text: str | None = None


class _StatCache:
    """A file whose size and modified time are unchanged is not read again:
    an untouched project costs a stat per file."""

    def __init__(self, limit: int = 50_000):
        self.limit = limit
        self.entries: dict[str, tuple[int, int, str, int, bool]] = {}
        self.lock = threading.Lock()

    def read(self, path: Path) -> _Read | None:
        try:
            info = path.stat()
        except OSError:
            return None
        key = str(path)
        with self.lock:
            hit = self.entries.get(key)
        if hit and hit[0] == info.st_size and hit[1] == info.st_mtime_ns:
            return _Read(hit[2], hit[3], hit[4])
        try:
            data = path.read_bytes()
        except OSError:
            return None
        text: str | None = None
        if len(data) <= MAX_FILE_BYTES:
            if path.suffix.lower() == ".ipynb":
                text = _notebook_code(data)
            else:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
        body = text.encode() if text is not None else data
        sha = hashlib.sha256(body).hexdigest()
        with self.lock:
            if len(self.entries) >= self.limit:
                self.entries.clear()
            self.entries[key] = (info.st_size, info.st_mtime_ns, sha, len(body), text is not None)
        return _Read(sha, len(body), text is not None, text)


_stats = _StatCache()


# ─── Where the code is ──────────────────────────────────────────────────


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


_toplevels: dict[str, tuple[float, str | None]] = {}


def _git_root(start: Path) -> Path | None:
    key = str(start)
    hit = _toplevels.get(key)
    if hit and time.monotonic() - hit[0] < 300:
        return Path(hit[1]) if hit[1] else None
    out = _git(start, "rev-parse", "--show-toplevel")
    top = out.strip() if out else None
    if len(_toplevels) > 1000:
        _toplevels.clear()
    _toplevels[key] = (time.monotonic(), top)
    return Path(top).resolve() if top else None


def _marker_root(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in PROJECT_MARKERS):
            return directory
    return None


def _too_broad(root: Path) -> bool:
    """A home directory or a drive root is a place, not a project."""
    try:
        return root == Path.home().resolve() or root.parent == root
    except RuntimeError:
        return root.parent == root


@dataclass
class Scope:
    root: Path
    mode: str  # "project" or "script"
    git_root: Path | None = None
    note: str | None = None


def find_scope(entries: list[Path], cwd: Path, project_root: str | None) -> Scope:
    start = entries[0].parent if entries else cwd
    git_root = _git_root(start)
    if project_root:
        root = Path(project_root).expanduser().resolve()
        return Scope(root, "project", git_root if git_root and _within(root, git_root) else None)
    candidates = [r for r in (git_root, _marker_root(start)) if r is not None and not _too_broad(r)]
    if candidates:
        # The nearest: a package inside a monorepo is the project, not the repo.
        root = max(candidates, key=lambda r: len(r.parts))
        return Scope(root, "project", git_root)
    return Scope(start, "script", git_root)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _project_files(scope: Scope) -> list[Path] | None:
    """Every code file in the project, or None when it is too big to be one."""
    found: list[Path] = []
    if scope.git_root is not None and _within(scope.root, scope.git_root):
        out = _git(scope.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", ".")
        if out is not None:
            for relative in filter(None, out.split("\0")):
                path = scope.root / relative
                if (not any(part in SKIP_DIRS for part in Path(relative).parts[:-1])
                        and _code_file(path) and path.is_file()):
                    found.append(path)
                    if len(found) > MAX_PROJECT_FILES:
                        return None
            return found
    for directory, dirs, files in os.walk(scope.root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            path = Path(directory) / name
            if _code_file(path):
                found.append(path)
                if len(found) > MAX_PROJECT_FILES:
                    return None
    return found


# ─── What the task uses ─────────────────────────────────────────────────


_FILE_LITERAL = re.compile(r"""['"]([^'"\n]{1,200}\.(?:py|sql|ya?ml|toml|ini|cfg|json|sh|ipynb))['"]""", re.I)


def _python_text(path: Path, text: str) -> str:
    if path.suffix.lower() != ".ipynb":
        return text
    # Notebook cells may hold IPython magics, which are not Python.
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("%", "!")))


def _references(path: Path, text: str, roots: list[Path]) -> list[Path]:
    """Local files a script imports or names."""
    found: list[Path] = []
    if path.suffix.lower() in (".py", ".ipynb"):
        try:
            tree = ast.parse(_python_text(path, text))
        except (SyntaxError, ValueError):
            tree = None
        modules: list[tuple[str, int]] = []
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.Import):
                modules += [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                modules.append((base, node.level))
                modules += [(f"{base}.{alias.name}" if base else alias.name, node.level)
                            for alias in node.names]
        for module, level in modules:
            parts = [p for p in module.split(".") if p]
            bases = [path.parent.parents[level - 2] if level > 1 else path.parent] if level else roots
            for base in bases:
                for candidate in (base.joinpath(*parts).with_suffix(".py") if parts else None,
                                  base.joinpath(*parts, "__init__.py")):
                    if candidate is not None and candidate.is_file():
                        found.append(candidate)
                        # Importing a.b.c runs a/__init__.py and a/b/__init__.py first.
                        found += [init for i in range(1, len(parts))
                                  if (init := base.joinpath(*parts[:i], "__init__.py")).is_file()]
    for match in _FILE_LITERAL.finditer(text):
        name = match.group(1)
        for base in (path.parent, *roots):
            candidate = (base / name) if not Path(name).is_absolute() else Path(name)
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def _used(entries: list[Path], roots: list[Path]) -> list[Path]:
    """The entry files and every local file reached from them — inside the
    folders the task's code is looked up in, never the installed packages."""
    seen: dict[Path, None] = {}
    queue = [p.resolve() for p in entries]
    local = [r for r in roots if not _too_broad(r)]
    while queue and len(seen) < MAX_FOLLOWED:
        path = queue.pop(0)
        if (path in seen or not path.is_file() or not _code_file(path)
                or (path not in entries and not any(_within(path, r) for r in local))):
            continue
        seen[path] = None
        read = _stats.read(path)
        text = read.text if read else None
        if text is None and read is not None and read.textual:
            try:
                raw = path.read_bytes()
                text = _notebook_code(raw) if path.suffix.lower() == ".ipynb" else raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                text = None
        if text is not None:
            queue += [p.resolve() for p in _references(path, text, roots)]
    return list(seen)


def entry_files(task, render, cwd: Path) -> list[Path]:
    """The files a task runs: its script, notebook or SQL file, or the ones
    its command names (`python load.py`, `bash run.sh`)."""
    written = [render(p) for p in (task.script_path, task.notebook_path, task.sql_path) if p]
    if task.command:
        command = render(task.command)
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = command.split()
        written += [t.strip("\"'") for t in tokens if Path(t.strip("\"'")).suffix.lower() in CODE_SUFFIXES]
    paths = []
    for item in written:
        path = Path(item).expanduser()
        path = (path if path.is_absolute() else cwd / path).resolve()
        if path.is_file() and path not in paths:
            paths.append(path)
    return paths


# ─── Recording ──────────────────────────────────────────────────────────


def _insert_missing(db: Session, model, rows: list[dict]) -> None:
    """Insert what is not there yet. Two tasks starting at once on the same
    files insert the same blobs; the second must not fail its task over it."""
    if not rows:
        return
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    for start in range(0, len(rows), 200):
        db.execute(insert(model).values(rows[start:start + 200]).on_conflict_do_nothing())


def _git_state(scope: Scope) -> dict[str, Any] | None:
    if scope.git_root is None:
        return None
    commit = _git(scope.git_root, "rev-parse", "HEAD")
    if commit is None:
        return None
    status = _git(scope.root, "status", "--porcelain", "--untracked-files=no", "--", ".")
    branch = _git(scope.git_root, "rev-parse", "--abbrev-ref", "HEAD")
    return {"commit": commit.strip(), "branch": (branch or "").strip() or None,
            "dirty": bool(status and status.strip())}


def capture(db: Session, task, render, cwd: Path, project_root: str | None) -> dict[str, Any] | None:
    """Record the code this task attempt is about to run. Returns the
    record for the task run's inputs; never raises."""
    try:
        entries = entry_files(task, render, cwd)
        if not entries and not project_root and not task.cwd:
            # No script, and a folder that is only wherever RunRail was
            # started: nothing of the task's own to track.
            return None
        scope = find_scope(entries, cwd, project_root)
        roots = list(dict.fromkeys([scope.root, cwd.resolve(), *(e.parent for e in entries),
                                    scope.root / "src"]))
        files = _project_files(scope) if scope.mode == "project" else None
        if scope.mode == "project" and files is None:
            scope = Scope(scope.root, "script", scope.git_root,
                          note=f"{scope.root.name} has over {MAX_PROJECT_FILES} code files; "
                               "only the files this task uses are tracked")
        used = _used(entries, roots)
        if files is None:
            files = used
        listing: dict[str, list] = {}
        blobs: list[dict] = []
        for path in sorted(set(files) | set(used)):
            read = _stats.read(path)
            if read is None:
                continue
            relative = _relative(path, scope.root)
            listing[relative] = [read.sha, read.textual]
            if read.textual:
                blobs.append({"path": path, "sha": read.sha, "text": read.text})
        _store_blobs(db, blobs)
        body = json.dumps(sorted(listing.items()), separators=(",", ":"))
        manifest = hashlib.sha256(body.encode()).hexdigest()
        _insert_missing(db, CodeManifest, [{"sha256": manifest, "files": listing}])
        record: dict[str, Any] = {
            "manifest": manifest, "root": str(scope.root), "mode": scope.mode,
            "entries": [_relative(e, scope.root) for e in entries],
            "used": sorted(_relative(u, scope.root) for u in used),
        }
        git = _git_state(scope)
        if git:
            record["git"] = git
        if scope.note:
            record["note"] = scope.note
        return record
    except Exception:
        log.warning("could not record the code of task %s", getattr(task, "id", None), exc_info=True)
        return None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _store_blobs(db: Session, blobs: list[dict]) -> None:
    """Each blob once: those already stored are not read or written again."""
    wanted = {b["sha"]: b for b in blobs}
    if not wanted:
        return
    have: set[str] = set()
    shas = list(wanted)
    for start in range(0, len(shas), 500):
        have |= set(db.scalars(select(CodeBlob.sha256).where(CodeBlob.sha256.in_(shas[start:start + 500]))))
    rows = []
    for sha, blob in wanted.items():
        if sha in have:
            continue
        text = blob["text"]
        if text is None:  # known from the stat cache, but its blob was collected since
            try:
                data = blob["path"].read_bytes()
                text = _notebook_code(data) if blob["path"].suffix.lower() == ".ipynb" else data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if hashlib.sha256(text.encode()).hexdigest() != sha:
                continue  # changed under us; the next run records it
        rows.append({"sha256": sha, "size": len(text.encode()), "content": zlib.compress(text.encode(), 6)})
    _insert_missing(db, CodeBlob, rows)


# ─── Comparing ──────────────────────────────────────────────────────────


@dataclass
class _Side:
    record: dict[str, Any]
    files: dict[str, list] = field(default_factory=dict)


def _side(db: Session, record: dict[str, Any] | None) -> _Side | None:
    if not record or not record.get("manifest"):
        return None
    manifest = db.get(CodeManifest, record["manifest"])
    return _Side(record, dict(manifest.files) if manifest else {})


def _blob_text(db: Session, sha: str) -> str | None:
    blob = db.get(CodeBlob, sha)
    return zlib.decompress(blob.content).decode("utf-8") if blob else None


def compare(db: Session, after: dict | None, before: dict | None, unified, masker,
            max_files_with_lines: int = 20) -> dict[str, Any] | None:
    """The files that differ between two recorded code states, the ones the
    task uses first. None when nothing differs."""
    a, b = _side(db, after), _side(db, before)
    if a is None or b is None:
        return None
    if a.record["manifest"] == b.record["manifest"] and a.record.get("root") == b.record.get("root"):
        return None
    used = set(a.record.get("used", [])) | set(b.record.get("used", []))
    changes = []
    for path in sorted(set(a.files) | set(b.files)):
        new, old = a.files.get(path), b.files.get(path)
        if new and old and new[0] == old[0]:
            continue
        change = "added" if old is None else "removed" if new is None else "modified"
        changes.append({"path": path, "change": change, "used": path in used,
                        "_new": new, "_old": old})
    changes.sort(key=lambda c: (not c["used"], c["path"]))
    shown = 0
    for entry in changes:
        new, old = entry.pop("_new"), entry.pop("_old")
        if shown >= max_files_with_lines:
            continue
        before_text = _blob_text(db, old[0]) if old and old[1] else ("" if old is None else None)
        after_text = _blob_text(db, new[0]) if new and new[1] else ("" if new is None else None)
        if before_text is None or after_text is None:
            entry["note"] = "Too large, or not text: its fingerprint changed"
            continue
        name = Path(entry["path"]).name
        entry["diff"] = unified(masker.text(before_text), masker.text(after_text),
                                f"{name} (before)", f"{name} (this run)")
        shown += 1
    result: dict[str, Any] = {
        "root": a.record.get("root"), "mode": a.record.get("mode"),
        "files": changes, "used_changed": sum(1 for c in changes if c["used"]),
        "other_changed": sum(1 for c in changes if not c["used"]),
    }
    if a.record.get("root") != b.record.get("root"):
        result["moved"] = {"before": b.record.get("root"), "after": a.record.get("root")}
    git_after, git_before = a.record.get("git"), b.record.get("git")
    if git_after or git_before:
        result["git"] = {"before": git_before, "after": git_after}
    if a.record.get("note"):
        result["note"] = a.record["note"]
    return result if changes or "moved" in result else None


# ─── Keeping only what kept runs need ───────────────────────────────────


def collect_garbage(db: Session) -> tuple[int, int]:
    """Delete manifests no task run points at, then blobs no manifest lists.
    Reachability, not age: a run kept by retention keeps its code."""
    from sqlalchemy import delete
    live = set(db.scalars(select(TaskRun.code_manifest).where(TaskRun.code_manifest.is_not(None))
                          .distinct()))
    stale = [sha for sha in db.scalars(select(CodeManifest.sha256)) if sha not in live]
    for start in range(0, len(stale), 500):
        db.execute(delete(CodeManifest).where(CodeManifest.sha256.in_(stale[start:start + 500])))
    db.commit()
    needed: set[str] = set()
    for files in db.scalars(select(CodeManifest.files)):
        needed |= {entry[0] for entry in (files or {}).values() if entry and entry[1]}
    orphans = [sha for sha in db.scalars(select(CodeBlob.sha256)) if sha not in needed]
    for start in range(0, len(orphans), 500):
        db.execute(delete(CodeBlob).where(CodeBlob.sha256.in_(orphans[start:start + 500])))
    db.commit()
    return len(stale), len(orphans)
