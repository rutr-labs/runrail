"""Live updates for the browser.

Every change a page shows is announced as a small event ({"type": "run_updated",
"id": 7}) on the global WebSocket, and pages reload what changed instead of
polling for it.

An event can start in two kinds of process:

- One that serves the WebSockets (the API: `runrail serve` or `runrail api`).
  `notify` hands it straight to the open connections.
- One that does not (a `runrail worker` or `runrail scheduler` started on its
  own, possibly on another host). `notify` queues it, a background thread
  writes it to the live_events table, and the API process, which tails that
  table, broadcasts it. Without this those events were lost, and the pages
  could only stay current by polling.

The difference is whether this process has an event loop registered, which
only the API's startup does.
"""

import asyncio
import logging
import queue
import threading
from datetime import timedelta
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)

#: How often the API process looks for relayed events, and how often a
#: relaying process writes what it has queued.
TAIL_SECONDS = 0.5
FLUSH_SECONDS = 0.25
#: Relayed events are only for browsers that are open now.
KEEP_EVENTS = timedelta(minutes=10)
#: A relaying process that cannot reach the database keeps at most this many.
MAX_QUEUED = 2000


class WebSocketManager:
    """Tracks all open WebSocket connections and broadcasts events to them."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self._writer: threading.Thread | None = None
        self._writer_lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead: set[WebSocket] = set()
        # Snapshot: clients can connect/disconnect while we await sends,
        # and mutating the live set mid-iteration raises RuntimeError.
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    def notify(self, message: dict) -> None:
        """Announce a change. Safe from any thread."""
        if self._loop is not None:
            if self._connections:
                asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)
            return
        self._relay(message)

    # ── relaying process side ─────────────────────────────────

    def _relay(self, message: dict) -> None:
        """Queue an event for the API process. Never blocks the caller: the
        worker announces changes from its own hot path, and a database write
        there could wait on a lock."""
        if self._outbox.qsize() >= MAX_QUEUED:
            try:
                self._outbox.get_nowait()   # drop the oldest; a page resyncs anyway
            except queue.Empty:
                pass
        self._outbox.put(message)
        with self._writer_lock:
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(target=self._write_forever, name="runrail-live-relay", daemon=True)
                self._writer.start()

    def _write_forever(self) -> None:
        import time
        flushes = 0
        while True:
            time.sleep(FLUSH_SECONDS)
            self.flush()
            flushes += 1
            # A worker with no API running beside it would otherwise only add.
            if flushes % 480 == 0:   # about every two minutes
                _prune_events()

    def flush(self) -> int:
        """Write everything queued, in one transaction. Returns how many."""
        batch: list[dict[str, Any]] = []
        while True:
            try:
                batch.append(self._outbox.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        from runrail.db import SessionLocal
        from runrail.models import LiveEvent
        try:
            with SessionLocal() as db:
                db.add_all(LiveEvent(payload=message) for message in batch)
                db.commit()
        except Exception:
            log.debug("could not relay %d live events; will retry", len(batch), exc_info=True)
            for message in batch[-MAX_QUEUED:]:
                self._outbox.put(message)
            return 0
        return len(batch)

    # ── API process side ──────────────────────────────────────

    async def tail_relayed(self) -> None:
        """Broadcast events other processes relayed, for as long as the API
        runs. Starts from the newest row: events from before this process
        started are for pages that have since reloaded."""
        last = await asyncio.to_thread(_newest_event_id)
        ticks = 0
        while True:
            await asyncio.sleep(TAIL_SECONDS)
            try:
                rows = await asyncio.to_thread(_events_after, last)
            except Exception:
                log.debug("could not read relayed live events", exc_info=True)
                continue
            for event_id, payload in rows:
                last = event_id
                if self._connections:
                    await self.broadcast(payload)
            ticks += 1
            if ticks % 240 == 0:   # about every two minutes
                await asyncio.to_thread(_prune_events)


def _newest_event_id() -> int:
    from sqlalchemy import func, select

    from runrail.db import SessionLocal
    from runrail.models import LiveEvent
    try:
        with SessionLocal() as db:
            return db.scalar(select(func.max(LiveEvent.id))) or 0
    except Exception:
        return 0


def _events_after(last: int, limit: int = 500) -> list[tuple[int, dict[str, Any]]]:
    from sqlalchemy import select

    from runrail.db import SessionLocal
    from runrail.models import LiveEvent
    with SessionLocal() as db:
        return [(row.id, row.payload) for row in db.execute(
            select(LiveEvent.id, LiveEvent.payload).where(LiveEvent.id > last).order_by(LiveEvent.id).limit(limit))]


def _prune_events() -> None:
    from sqlalchemy import delete

    from runrail.db import SessionLocal
    from runrail.models import LiveEvent, now
    try:
        with SessionLocal() as db:
            db.execute(delete(LiveEvent).where(LiveEvent.created_at < now() - KEEP_EVENTS))
            db.commit()
    except Exception:
        log.debug("could not prune live events", exc_info=True)


manager = WebSocketManager()
