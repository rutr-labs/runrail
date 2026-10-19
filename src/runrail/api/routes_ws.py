import asyncio
import codecs
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from runrail import variables
from runrail.api.ws import manager
from runrail.config import get_settings
from runrail.db import SessionLocal
from runrail.masking import StreamMasker
from runrail.models import TaskRun, TaskRunStatus
from runrail.paths import confined

router = APIRouter()

_TERMINAL = frozenset({
    TaskRunStatus.success, TaskRunStatus.failed,
    TaskRunStatus.cancelled, TaskRunStatus.skipped,
})


@router.websocket("/api/ws")
async def global_ws(ws: WebSocket) -> None:
    """Global notification channel – broadcasts run, task-run, and environment events."""
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keeps the connection alive; client may send pings
    except (WebSocketDisconnect, asyncio.CancelledError):
        # Client left or the server is shutting down — nothing to report.
        manager.disconnect(ws)


#: A first connection starts this far from the end, not at the beginning: a
#: tail is what a live view is for, and the full file is a click away.
FIRST_READ_BYTES = 256 * 1024
#: Most bytes one poll sends; the next poll continues from there.
CHUNK_BYTES = 1024 * 1024


def _task_state(task_run_id: int, attr: str) -> tuple[TaskRunStatus | None, str | None]:
    with SessionLocal() as db:
        task_run = db.get(TaskRun, task_run_id)
        if task_run is None:
            return None, None
        return task_run.status, getattr(task_run, attr)


def _read_from(path: Path, offset: int | None) -> tuple[bytes, int | None]:
    """The next chunk of the file from `offset` (the tail on a first read),
    and where the following read starts. A file shorter than the offset was
    truncated or replaced: start again from its beginning."""
    try:
        size = path.stat().st_size
    except OSError:
        return b"", offset
    if offset is None:
        offset = max(0, size - FIRST_READ_BYTES)
    elif size < offset:
        offset = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(CHUNK_BYTES)
    return chunk, offset + len(chunk)


@router.websocket("/api/ws/task-runs/{task_run_id}/logs")
async def stream_logs(ws: WebSocket, task_run_id: int, stream: str = "stdout") -> None:
    """Tail the stdout or stderr of a running task run, then close when it finishes."""
    if stream not in ("stdout", "stderr"):
        await ws.close(code=1008)
        return
    await ws.accept()
    attr = "stdout_log_path" if stream == "stdout" else "stderr_log_path"
    logs_root = get_settings().logs_dir
    offset: int | None = None  # None until the first read picks where to start
    # Incremental decoder: a chunk boundary can split a multibyte UTF-8
    # character, which a plain decode() would mangle into replacement chars.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # Logs are masked as they are written; this catches one written before
    # a secret was added. Fetched once per connection, off the loop.
    masker = await asyncio.to_thread(variables.masker)
    hide = StreamMasker(lambda: masker)
    try:
        while True:
            # Off the event loop: the database read and the file read are
            # blocking, and a poll every quarter second per open tab used to do
            # both on the loop — a tab on a task hours into a multi-gigabyte
            # log read all of it there, freezing every other request.
            status, log_path = await asyncio.to_thread(_task_state, task_run_id, attr)
            if status is None:
                break
            if log_path and confined(log_path, logs_root):
                chunk, offset = await asyncio.to_thread(_read_from, Path(log_path), offset)
                chunk = hide.feed(chunk) if chunk else chunk
                if chunk:
                    text = decoder.decode(chunk)
                    if text:
                        await ws.send_text(text)
            if status in _TERMINAL:
                tail = decoder.decode(hide.flush(), final=True)
                if tail:
                    await ws.send_text(tail)
                break
            # Wait on the socket instead of sleeping blind: this raises
            # WebSocketDisconnect the moment the client leaves or the server
            # closes connections during shutdown — a bare sleep loop kept these
            # handlers alive forever and hung graceful shutdown.
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=0.25)
            except TimeoutError:
                pass
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass  # client gone or server shutting down — just clean up quietly
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
