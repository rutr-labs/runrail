"""One answer to "may this file be served?" for every endpoint that opens a
path it read from the database.

Only RunRail writes those rows, but a hand-edited row, or one imported from
another home, must not turn a log or artifact endpoint into a reader of
arbitrary files. Log search always checked; the run page, artifact download,
live log tail, shared export and service log did not.
"""

from pathlib import Path


def confined(path: Path | str | None, root: Path) -> bool:
    """True when `path` resolves to somewhere under `root`."""
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except OSError:
        return False
