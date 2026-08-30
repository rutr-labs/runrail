"""RunRail workflow control plane."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: pyproject's version, read from the installed
    # distribution. Three hand-maintained copies had already drifted to
    # 0.1.0 / 0.3.1 / 0.4.0, and this one is stamped into every exported run.
    __version__ = version("runrail")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
