"""Hiding secret values in what RunRail writes and shows.

Every secret's value is replaced with MASK in task and service logs, in the
notebooks papermill writes, in rendered reports and in the commands and errors
a run records. Output is masked as it is written; the places that read it back
mask again, which covers logs written before a secret existed.

Only exact values are caught (plus the form json.dumps gives them, and each
long line of a multi-line value such as a PEM key). A script that prints part
of a secret, or an encoding of it, is not.
"""

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

MASK = "••••••"
_MASK_BYTES = MASK.encode()
#: Shorter values are not masked: "1" or "yes" would blank out every log.
MIN_LENGTH = 4
#: A line of a multi-line secret is masked on its own from this length.
MIN_LINE_LENGTH = 8


def _variants(value: str) -> set[str]:
    found = {value}
    escaped = json.dumps(value, ensure_ascii=False)[1:-1]
    found.add(escaped)
    if "\n" in value:
        found.update(line.strip() for line in value.splitlines()
                     if len(line.strip()) >= MIN_LINE_LENGTH)
    return {v for v in found if len(v) >= MIN_LENGTH}


class Masker:
    """Replaces a fixed set of secret values. An empty Masker changes nothing."""

    def __init__(self, values: Iterable[str] = ()):
        patterns = sorted({variant for value in values if value
                           for variant in _variants(value)}, key=len, reverse=True)
        self._text = re.compile("|".join(map(re.escape, patterns))) if patterns else None
        encoded = [p.encode() for p in patterns]
        self._bytes = re.compile(b"|".join(map(re.escape, encoded))) if encoded else None
        self._encoded = encoded

    def __bool__(self) -> bool:
        return self._text is not None

    def text(self, value: str | None) -> str | None:
        if not value or self._text is None:
            return value
        return self._text.sub(MASK, value)

    def bytes(self, value: bytes) -> bytes:
        if not value or self._bytes is None:
            return value
        return self._bytes.sub(_MASK_BYTES, value)

    def held_back(self, data: bytes) -> int:
        """How many trailing bytes of `data` could be the start of a secret
        that the next chunk completes — the longest such suffix."""
        longest = 0
        for pattern in self._encoded:
            for size in range(min(len(pattern) - 1, len(data)), longest, -1):
                if data.endswith(pattern[:size]):
                    longest = size
                    break
        return longest

    def json(self, value: Any) -> Any:
        """Every string inside a parsed JSON document, masked."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.json(item) for item in value]
        if isinstance(value, dict):
            return {key: self.json(item) for key, item in value.items()}
        return value


class StreamMasker:
    """Masks output that arrives in chunks, where a secret can be split
    between two of them: the tail that could still become a secret is held
    back until the next chunk shows whether it does.

    `source` is asked for the current Masker on every chunk, so a secret added
    while a service is running is masked from then on."""

    def __init__(self, source: Callable[[], Masker]):
        self._source = source
        self._held = b""

    def feed(self, chunk: bytes) -> bytes:
        masker = self._source()
        data = self._held + chunk
        if not masker:
            self._held = b""
            return data
        data = masker.bytes(data)
        keep = masker.held_back(data)
        self._held = data[len(data) - keep:] if keep else b""
        return data[:len(data) - keep]

    def flush(self) -> bytes:
        # A held tail is never a whole secret: whole ones were already replaced.
        held, self._held = self._held, b""
        return held


def masked_chunks(path: Path, masker: Masker, size: int = 64 * 1024) -> Iterator[bytes]:
    """A file's bytes, masked, without reading it whole."""
    stream = StreamMasker(lambda: masker)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(size), b""):
            out = stream.feed(chunk)
            if out:
                yield out
    tail = stream.flush()
    if tail:
        yield tail


def mask_notebook(path: Path, masker: Masker) -> bool:
    """Mask a notebook file in place: every string in it — cell outputs,
    sources, the parameters papermill injected, metadata. Written back only
    when something changed, and atomically, so a reader never sees half a
    file. Returns whether it changed."""
    if not masker:
        return False
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    masked = masker.json(original)
    if masked == original:
        return False
    # The layout nbformat writes, so the file stays a normal notebook.
    body = json.dumps(masked, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, scratch = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            out.write(body)
        os.replace(scratch, path)
    except OSError:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        return False
    return True


def notebook_bytes(path: Path, masker: Masker) -> bytes:
    """A notebook's content as served for download: masked, unchanged if it
    does not parse."""
    raw = path.read_bytes()
    if not masker:
        return raw
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return masker.bytes(raw)
    masked = masker.json(document)
    if masked == document:
        return raw
    return (json.dumps(masked, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()
