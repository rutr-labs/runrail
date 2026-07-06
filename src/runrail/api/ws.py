import asyncio

from fastapi import WebSocket


class WebSocketManager:
    """Tracks all open WebSocket connections and broadcasts events to them."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

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
        """Thread-safe broadcast from the synchronous worker thread."""
        if self._loop is not None and self._connections:
            asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)


manager = WebSocketManager()
