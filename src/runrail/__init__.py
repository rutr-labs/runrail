"""RunRail workflow control plane."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _resolve_version() -> str:
    """The version this code actually is.

    pyproject.toml wins whenever it is present, because installed metadata is a
    snapshot taken at install time and an editable install never refreshes it.
    A checkout installed with `pip install -e .` months ago therefore reported
    the version it had *then* — and that stale number was stamped into every
    exported run file and into the OpenAPI schema. Reading three hardcoded
    copies was the bug before this; reading a stale one is the same bug wearing
    a different hat.

    Falls back to the distribution metadata, which is authoritative for a wheel
    (where there is no pyproject.toml to find, and the metadata was written
    from it anyway).
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
            # Only trust a file that says it describes THIS package: an
            # installed copy could sit two levels below some unrelated project.
            if project.get("name") == "runrail" and isinstance(project.get("version"), str):
                return project["version"]
        except Exception:  # unreadable or malformed — metadata is still worth a try
            pass
    try:
        return version("runrail")
    except PackageNotFoundError:  # a source tree that was never installed
        return "0.0.0+unknown"


__version__ = _resolve_version()
