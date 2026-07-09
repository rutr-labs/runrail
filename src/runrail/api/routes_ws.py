import asyncio
import codecs
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from runrail.api.ws import manager
from runrail.db import SessionLocal
from runrail.models import TaskRun, TaskRunStatus

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


@router.websocket("/api/ws/task-runs/{task_run_id}/logs")
async def stream_logs(ws: WebSocket, task_run_id: int, stream: str = "stdout") -> None:
    """Tail the stdout or stderr of a running task run, then close when it finishes."""
    if stream not in ("stdout", "stderr"):
        await ws.close(code=1008)
        return
    await ws.accept()
    attr = "stdout_log_path" if stream == "stdout" else "stderr_log_path"
    offset = 0
    # Incremental decoder: a chunk boundary can split a multibyte UTF-8
    # character, which a plain decode() would mangle into replacement chars.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            with SessionLocal() as db:
                task_run = db.get(TaskRun, task_run_id)
            if task_run is None:
                break
            log_path = getattr(task_run, attr)
            if log_path:
                path = Path(log_path)
                if path.is_file():
                    # Incremental byte-offset read so long logs are not re-read every poll.
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                    if chunk:
                        offset += len(chunk)
                        text = decoder.decode(chunk)
                        if text:
                            await ws.send_text(text)
            if task_run.status in _TERMINAL:
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
