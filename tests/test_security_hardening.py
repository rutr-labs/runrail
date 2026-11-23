"""Browser-facing and on-disk hardening beyond the host and origin checks.

- A GET the browser marks as cross-site is refused on the API, except following
  a link to a report page. Such a request's response cannot be read, but it
  still runs: a log search with a pathological pattern holds the process.
- Every response says it must not be framed by another site.
- Webhook URLs are http(s) only, when saved and when sent.
- RunRail's own folders and its SQLite file are readable by its user only.
"""

import os
import stat

import pytest

from runrail import notify
from runrail.api.origin import SECURITY_HEADERS, _with_security_headers

CROSS_SITE = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}


def test_a_cross_site_get_to_the_api_is_refused(client):
    response = client.get("/api/logs/search", params={"q": "(.|.)*x", "regex": "true"}, headers=CROSS_SITE)
    assert response.status_code == 403
    assert "Cross-site" in response.json()["detail"]
    # The backup, with the secrets key, cannot be dropped into Downloads by
    # another site navigating to it either.
    navigate = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert client.get("/api/system/backup", params={"include_key": "true"}, headers=navigate).status_code == 403


def test_the_app_itself_and_scripts_still_read_the_api(client):
    assert client.get("/api/workflows", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert client.get("/api/workflows", headers={"Sec-Fetch-Site": "none"}).status_code == 200   # typed URL
    assert client.get("/api/workflows").status_code == 200                                      # curl, the CLI


def test_a_report_link_followed_from_another_site_still_opens(client):
    navigate = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    for path in ("/api/workflows/nightly/latest-report/html", "/api/runs/1/report"):
        response = client.get(path, headers=navigate)
        assert response.status_code != 403, path          # 404 here: no such report, but not refused
    # Fetched by another site's script rather than followed as a link, it is refused.
    assert client.get("/api/runs/1/report", headers=CROSS_SITE).status_code == 403


@pytest.mark.parametrize("path", ["/api/health", "/"])
def test_responses_refuse_to_be_framed_by_other_sites(client, path):
    response = client.get(path)
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"


def test_a_response_with_its_own_policy_keeps_it():
    """Report pages send a stricter sandboxing CSP, which must not be replaced."""
    import asyncio
    sent = []

    async def send(message):
        sent.append(message)

    wrapped = _with_security_headers(send)
    asyncio.run(wrapped({"type": "http.response.start", "status": 200,
                         "headers": [(b"content-security-policy", b"sandbox allow-scripts; frame-ancestors 'self'")]}))
    headers = dict(sent[0]["headers"])
    assert headers[b"content-security-policy"] == b"sandbox allow-scripts; frame-ancestors 'self'"
    assert headers[b"x-frame-options"] == SECURITY_HEADERS[b"x-frame-options"]


@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://example.com/x", "not a url", "http://"])
def test_a_webhook_must_be_http(client, url):
    created = client.post("/api/workflows", json={"name": "hooked", "notify_webhook_url": url})
    assert created.status_code == 422, created.text
    service = client.post("/api/services", json={"name": "svc", "command": "true", "notify_webhook_url": url})
    assert service.status_code == 422, service.text


def test_an_http_webhook_is_kept_as_written(client):
    url = "https://hooks.example.com/services/T000/B000/"
    created = client.post("/api/workflows", json={"name": "hooked", "notify_webhook_url": url})
    assert created.status_code == 201
    assert created.json()["notify_webhook_url"] == url


def test_a_stored_non_http_webhook_is_never_opened(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    error = notify.deliver(secret.as_uri(), "hello")
    assert error and "http(s)" in error


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_runrail_keeps_its_folders_and_database_to_its_own_user(client):
    from runrail.config import get_settings
    settings = get_settings()
    for folder in (settings.logs_dir, settings.artifacts_dir, settings.environments_dir, settings.home / "backups"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    database = settings.database_url.removeprefix("sqlite:///")
    if settings.database_url.startswith("sqlite:///"):
        assert stat.S_IMODE(os.stat(database).st_mode) == 0o600


def test_listening_beyond_this_machine_is_said_out_loud(capsys):
    from runrail.cli import _warn_if_exposed
    _warn_if_exposed("127.0.0.1")
    assert capsys.readouterr().err == ""
    _warn_if_exposed("0.0.0.0")
    assert "anyone who can reach this machine" in capsys.readouterr().err


def test_a_webhook_saved_before_the_http_rule_still_lists(client):
    """0.8.2 stored any text as a workflow's webhook. Reading such a row must
    not fail (it took the workflows list down in 0.9.0); sending to it is
    refused with a reason, and saving asks for a real URL."""
    from runrail.db import SessionLocal
    from runrail.models import Workflow
    from runrail.notify import deliver

    created = client.post("/api/workflows", json={"name": "old-webhook"}).json()
    with SessionLocal() as db:
        db.get(Workflow, created["id"]).notify_webhook_url = "hooks.slack.com/services/T/B/x"
        db.commit()
    listed = client.get("/api/workflows")
    assert listed.status_code == 200
    assert listed.json()[0]["notify_webhook_url"] == "hooks.slack.com/services/T/B/x"
    assert client.get(f"/api/workflows/{created['id']}").status_code == 200
    assert deliver("hooks.slack.com/services/T/B/x", "boom")
    resaved = client.put(f"/api/workflows/{created['id']}",
                         json={"name": "old-webhook", "notify_webhook_url": "hooks.slack.com/services/T/B/x"})
    assert resaved.status_code == 422


def test_applying_a_file_with_an_old_webhook_names_the_workflow(client):
    """A 0.8.2 export or backup can carry such a webhook; applying it is
    refused, in one line that says which workflow and what to fix."""
    from runrail.db import SessionLocal
    from runrail.workflow_io import apply_workflows

    data = {"workflows": [{"name": "nightly", "notify_webhook_url": "hooks.slack.com/x",
                           "tasks": [{"name": "t", "task_type": "shell", "command": "true"}]}]}
    with SessionLocal() as db, pytest.raises(ValueError) as refused:
        apply_workflows(db, data)
    assert str(refused.value) == "Workflow 'nightly': notify_webhook_url must be an http(s) URL with a host"
