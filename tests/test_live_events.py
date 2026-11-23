"""Live updates from processes that have no browsers attached.

A worker or scheduler started on its own announces changes through the
live_events table; the API process tails it and broadcasts each row to the
open WebSockets. See runrail.api.ws.
"""

import asyncio

from sqlalchemy import func, select

from runrail.api.ws import WebSocketManager


def _events():
    import runrail.db as database
    from runrail.models import LiveEvent
    with database.SessionLocal() as db:
        return [row.payload for row in db.scalars(select(LiveEvent).order_by(LiveEvent.id))]


def test_a_process_without_sockets_relays_through_the_table(client):
    relay = WebSocketManager()          # no event loop registered: a worker
    relay.notify({"type": "run_updated", "id": 7})
    relay.notify({"type": "task_run_updated", "id": 3, "run_id": 7})
    assert relay.flush() == 2
    assert _events() == [{"type": "run_updated", "id": 7}, {"type": "task_run_updated", "id": 3, "run_id": 7}]
    assert relay.flush() == 0           # each event is written once


def test_the_api_process_never_writes_to_the_table(client):
    api = WebSocketManager()
    api.set_loop(asyncio.new_event_loop())
    api.notify({"type": "run_updated", "id": 7})
    assert api.flush() == 0
    assert _events() == []


def test_a_failed_write_is_kept_for_the_next_flush(client, monkeypatch):
    relay = WebSocketManager()
    relay.notify({"type": "run_updated", "id": 1})
    import runrail.db as database
    real = database.SessionLocal
    monkeypatch.setattr(database, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("locked")))
    assert relay.flush() == 0
    monkeypatch.setattr(database, "SessionLocal", real)
    assert relay.flush() == 1
    assert _events() == [{"type": "run_updated", "id": 1}]


def test_ids_are_never_reused_after_pruning(client):
    """The tailer remembers the last id it sent, so an emptied table must not
    hand out id 1 again (SQLite does without AUTOINCREMENT)."""
    import runrail.db as database
    from runrail.models import LiveEvent
    relay = WebSocketManager()
    relay.notify({"type": "a"}); relay.flush()
    with database.SessionLocal() as db:
        first = db.scalar(select(func.max(LiveEvent.id)))
        db.query(LiveEvent).delete(); db.commit()
    relay.notify({"type": "b"}); relay.flush()
    with database.SessionLocal() as db:
        assert db.scalar(select(func.max(LiveEvent.id))) > first


def test_the_socket_says_hello_answers_pings_and_carries_relayed_events(client):
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json() == {"type": "hello"}
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}
        relay = WebSocketManager()     # a worker in another process
        relay.notify({"type": "run_updated", "id": 42})
        relay.flush()
        assert ws.receive_json() == {"type": "run_updated", "id": 42}


def test_editing_a_workflow_is_announced(client):
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        created = client.post("/api/workflows", json={"name": "announced", "enabled": True}).json()
        assert ws.receive_json() == {"type": "workflow_updated", "id": created["id"]}
        client.post(f"/api/workflows/{created['id']}/tasks", json={
            "name": "t", "task_type": "shell", "command": "true", "depends_on_json": []})
        assert ws.receive_json() == {"type": "workflow_updated", "id": created["id"]}


def test_pausing_is_announced(client):
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        client.post("/api/system/pause", json={"paused": True})
        assert ws.receive_json() == {"type": "system_updated"}
        client.post("/api/system/pause", json={"paused": False})

