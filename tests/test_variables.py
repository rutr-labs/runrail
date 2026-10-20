"""Variables and secrets: stored encrypted, handed to tasks and services as
environment variables, and masked wherever RunRail writes or shows output."""

import contextlib
import io
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import TEST_DB_URL
from cryptography.fernet import Fernet

from runrail import system, variables
from runrail.config import Settings, get_settings
from runrail.db import SessionLocal
from runrail.masking import MASK, Masker, StreamMasker, mask_notebook
from runrail.models import Variable
from runrail.services import process
from runrail.worker.queue import claim_next_run
from runrail.worker.service import execute_workflow_run

SECRET = "hunter2-Sup3r-s3cret"


def _python(code: str) -> str:
    """A command both shells run the same way: python -c with no double quotes inside."""
    assert '"' not in code
    return f'"{sys.executable}" -c "{code}"'


def add(client, name, value, secret=False, **extra):
    response = client.post("/api/variables", json={"name": name, "value": value, "secret": secret, **extra})
    assert response.status_code == 201, response.text
    return response.json()["saved"]


def run_shell(client, command, name="wf", environment_id=None):
    workflow = client.post("/api/workflows", json={
        "name": name, "enabled": True, "max_concurrent_runs": 1,
        **({"default_environment_id": environment_id} if environment_id else {}),
    }).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": command,
        "depends_on_json": [], "retries": 0, "retry_delay_seconds": 0,
    })
    run_id = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()["id"]
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    return client.get(f"/api/runs/{run_id}").json()


# ─── Masking ────────────────────────────────────────────────────────────


def test_a_masker_hides_values_their_json_form_and_long_lines_of_a_multiline_one():
    pem = "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEF\n-----END KEY-----"
    masker = Masker([SECRET, 'pa"ss\\word', pem, "abc"])
    assert masker.text(f"login {SECRET} ok") == f"login {MASK} ok"
    assert masker.text(json.dumps({"p": 'pa"ss\\word'})) == '{"p": "' + MASK + '"}'
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEF" not in masker.text("line MIIEvQIBADANBgkqhkiG9w0BAQEF\n")
    # Too short to hide without hiding everything else.
    assert masker.text("abc abc") == "abc abc"
    assert not Masker([]) and Masker([]).text("x") == "x"


def test_a_secret_split_between_two_chunks_is_still_masked():
    stream = StreamMasker(lambda: Masker([SECRET]))
    half = len(SECRET) // 2
    out = stream.feed(b"before " + SECRET[:half].encode())
    assert out == b"before ", "the start of the secret must wait for the rest"
    out += stream.feed(SECRET[half:].encode() + b" after\n")
    out += stream.flush()
    assert out == f"before {MASK} after\n".encode()
    # Output that cannot be the start of a secret is never held back.
    assert stream.feed(b"progress 50%\r") == b"progress 50%\r"


def test_a_notebook_is_masked_in_place_and_left_alone_when_clean(tmp_path):
    notebook = tmp_path / "out.ipynb"
    document = {"cells": [{"cell_type": "code", "source": "print(pw)",
                           "outputs": [{"output_type": "stream", "text": [f"pw={SECRET}\n"]}]}],
                "metadata": {"papermill": {"parameters": {"token": SECRET}}}, "nbformat": 4,
                "nbformat_minor": 5}
    notebook.write_text(json.dumps(document))
    assert mask_notebook(notebook, Masker([SECRET]))
    text = notebook.read_text()
    assert SECRET not in text and MASK in text
    assert json.loads(text)["nbformat"] == 4
    before = notebook.stat().st_mtime_ns
    assert not mask_notebook(notebook, Masker([SECRET]))
    assert notebook.stat().st_mtime_ns == before


# ─── Storing ────────────────────────────────────────────────────────────


def test_a_secret_is_encrypted_at_rest_and_never_sent_back(client):
    saved = add(client, "DB_PASSWORD", SECRET, secret=True, description="warehouse login")
    assert saved["value"] is None and saved["secret"] and saved["problem"] is None
    add(client, "DATA_DIR", "/srv/data")
    listing = client.get("/api/variables").json()
    assert SECRET not in json.dumps(listing)
    assert [v["name"] for v in listing["variables"]] == ["DATA_DIR", "DB_PASSWORD"]
    assert listing["key"]["source"] == "file" and listing["key"]["id"]
    with SessionLocal() as db:
        row = db.query(Variable).filter_by(name="DB_PASSWORD").one()
        assert SECRET not in row.value
        assert Fernet(variables.load_key()).decrypt(row.value.encode()).decode() == SECRET
    key_file = variables.key_path()
    if os.name != "nt":
        assert key_file.stat().st_mode & 0o777 == 0o600
    else:
        assert key_file.read_bytes().startswith(b"dpapi:")


@pytest.mark.parametrize("body, reason", [
    ({"name": "PATH", "value": "x"}, "cannot be overridden"),
    ({"name": "RUNRAIL_HOME", "value": "x"}, "kept for RunRail"),
    ({"name": "2FA", "value": "x"}, "letters, digits"),
    ({"name": "TOKEN", "value": "abc", "secret": True}, "at least 4"),
])
def test_names_and_values_that_cannot_work_are_refused(client, body, reason):
    response = client.post("/api/variables", json=body)
    assert response.status_code == 422 and reason in response.json()["detail"]


def test_a_name_is_taken_whatever_its_case(client):
    add(client, "Db_Host", "a")
    response = client.post("/api/variables", json={"name": "DB_HOST", "value": "b"})
    assert response.status_code == 422 and "already" in response.json()["detail"]


def test_a_variable_can_become_a_secret_but_not_back(client):
    row = add(client, "API_TOKEN", SECRET)
    listing = client.patch(f"/api/variables/{row['id']}", json={"secret": True}).json()
    assert listing["variables"][0]["secret"] and listing["variables"][0]["value"] is None
    back = client.patch(f"/api/variables/{row['id']}", json={"secret": False})
    assert back.status_code == 422 and "cannot be turned back" in back.json()["detail"]
    replaced = client.patch(f"/api/variables/{row['id']}", json={"value": "new-value-1"})
    assert replaced.status_code == 200
    with SessionLocal() as db:
        assert variables.load(db).env["API_TOKEN"] == "new-value-1"


def test_a_secret_from_another_key_says_so_and_is_not_set(client):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    key_file = variables.key_path()
    key_file.unlink()
    variables.write_key_file(key_file, Fernet.generate_key())
    variables.invalidate()
    entry = client.get("/api/variables").json()["variables"][0]
    assert entry["problem"] == "it was encrypted with a different key"
    assert "DB_PASSWORD" not in variables.current().env


def test_the_key_can_come_from_the_environment_and_never_reaches_a_task(client, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("RUNRAIL_SECRETS_KEY", key)
    get_settings.cache_clear()
    add(client, "DB_PASSWORD", SECRET, secret=True)
    assert not variables.key_path().exists(), "no key file when the environment holds the key"
    assert client.get("/api/variables").json()["key"]["source"] == "environment"
    env = variables.child_environment()
    assert "RUNRAIL_SECRETS_KEY" not in env and env["DB_PASSWORD"] == SECRET


# ─── Tasks ──────────────────────────────────────────────────────────────


def test_a_task_gets_variables_and_secrets_and_its_logs_hide_the_secret(client):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    add(client, "REGION", "eu-west")
    run = run_shell(client, _python(
        "import os,sys; print('pw=' + os.environ['DB_PASSWORD']); "
        "print('region=' + os.environ['REGION']); "
        "sys.stderr.write('err ' + os.environ['DB_PASSWORD'])"))
    assert run["status"] == "success"
    task_run = run["task_runs"][0]
    with SessionLocal() as db:
        from runrail.models import TaskRun
        row = db.get(TaskRun, task_run["id"])
        on_disk = Path(row.stdout_log_path).read_text() + Path(row.stderr_log_path).read_text()
    assert SECRET not in on_disk, "the secret was written to the log file"
    assert f"pw={MASK}" in on_disk and "region=eu-west" in on_disk and f"err {MASK}" in on_disk
    assert f"pw={MASK}" in client.get(f"/api/task-runs/{task_run['id']}/stdout").text


def test_a_secret_typed_into_a_command_is_masked_in_the_recorded_command(client):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    run = run_shell(client, _python(f"print('{SECRET}')"))
    task_run = run["task_runs"][0]
    assert SECRET not in (task_run["rendered_command"] or "")
    assert MASK in client.get(f"/api/task-runs/{task_run['id']}/stdout").text


def test_an_environment_variable_wins_over_a_global_one(client, tmp_path):
    add(client, "REGION", "global")
    environment = client.post("/api/environments", json={
        "name": "shell-env", "env_type": "system", "executable": sys.executable,
        "env_vars_json": {"REGION": "from-environment"}}).json()
    run = run_shell(client, _python("import os; print('region=' + os.environ['REGION'])"),
                    environment_id=environment["id"])
    stdout = client.get(f"/api/task-runs/{run['task_runs'][0]['id']}/stdout").text
    assert "region=from-environment" in stdout
    listing = client.get("/api/variables").json()["variables"]
    assert listing[0]["overridden_in"] == ["environment shell-env"]


def test_a_log_written_before_the_secret_existed_is_masked_when_read(client):
    run = run_shell(client, _python(f"print('token {SECRET} in an old log')"))
    task_run_id = run["task_runs"][0]["id"]
    assert SECRET in client.get(f"/api/task-runs/{task_run_id}/stdout").text
    add(client, "OLD_TOKEN", SECRET, secret=True)
    full = client.get(f"/api/task-runs/{task_run_id}/stdout").text
    tail = client.get(f"/api/task-runs/{task_run_id}/stdout?tail_bytes=4096").text
    assert SECRET not in full and MASK in full
    assert SECRET not in tail and MASK in tail
    with client.websocket_connect(f"/api/ws/task-runs/{task_run_id}/logs?stream=stdout") as ws:
        streamed = ws.receive_text()
    assert SECRET not in streamed and MASK in streamed
    found = client.get("/api/logs/search", params={"q": SECRET}).json()
    assert found["matches"] == [], "searching for a secret's value found it"
    found = client.get("/api/logs/search", params={"q": "in an old log"}).json()
    assert found["matches"] and SECRET not in json.dumps(found)


def test_a_failing_sql_task_does_not_leak_a_secret_in_its_error(client, tmp_path):
    secret_sql = SECRET.replace("-", "_")
    add(client, "SQL_SECRET", secret_sql, secret=True)
    script = tmp_path / "broken.sql"
    script.write_text(f"SELECT * FROM missing_{secret_sql};")
    workflow = client.post("/api/workflows", json={"name": "sql", "enabled": True}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "q", "task_type": "sql", "sql_path": str(script), "depends_on_json": [],
        "parameters_json": {"sqlite_db_path": str(tmp_path / "w.db")}})
    run_id = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {}}).json()["id"]
    with SessionLocal() as db:
        execute_workflow_run(db, claim_next_run(db))
    task_run = client.get(f"/api/runs/{run_id}").json()["task_runs"][0]
    assert task_run["status"] == "failed"
    assert secret_sql not in (task_run["error_message"] or "")
    assert secret_sql not in client.get(f"/api/task-runs/{task_run['id']}/stderr").text


# ─── Services ───────────────────────────────────────────────────────────


def test_a_service_log_is_masked_even_when_a_read_splits_the_secret(client, tmp_path):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    raw = f"starting\nconnected with {SECRET}\n".encode()

    class Trickle(io.RawIOBase):
        """Hands out a few bytes per read, as a pipe can."""
        def __init__(self):
            self.data = raw
        def readable(self):
            return True
        def readinto(self, buffer):
            piece, self.data = self.data[:5], self.data[5:]
            buffer[:len(piece)] = piece
            return len(piece)

    log = tmp_path / "service.log"
    process._pump(io.BufferedReader(Trickle(), buffer_size=5), log)
    assert log.read_text() == f"starting\nconnected with {MASK}\n"


def test_a_service_starts_with_the_variables(client):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    service = client.post("/api/services", json={"name": "api", "command": "echo hi"}).json()
    with SessionLocal() as db:
        from runrail.models import Service
        spec = process.launch_spec(db, db.get(Service, service["id"]))
    assert spec.env["DB_PASSWORD"] == SECRET


# ─── Notebooks and reports ──────────────────────────────────────────────


def test_a_notebook_download_is_masked(client, tmp_path):
    from runrail.models import Artifact, ArtifactType
    run = run_shell(client, _python("print('x')"))
    folder = get_settings().artifacts_dir.resolve() / str(run["id"])
    folder.mkdir(parents=True, exist_ok=True)
    notebook = folder / "report.ipynb"
    notebook.write_text(json.dumps({"cells": [{"cell_type": "code", "source": "",
                                               "outputs": [{"output_type": "stream",
                                                            "text": f"key={SECRET}"}]}],
                                    "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))
    with SessionLocal() as db:
        artifact = Artifact(task_run_id=run["task_runs"][0]["id"], workflow_run_id=run["id"],
                            name="report.ipynb", artifact_type=ArtifactType.notebook,
                            path=str(notebook), size_bytes=notebook.stat().st_size)
        db.add(artifact); db.commit(); artifact_id = artifact.id
    add(client, "NB_KEY", SECRET, secret=True)
    body = client.get(f"/api/artifacts/{artifact_id}/download").text
    assert SECRET not in body and MASK in body
    assert json.loads(body)["nbformat"] == 4


# ─── Backup, restore, export ────────────────────────────────────────────


def test_a_backup_leaves_the_key_out_unless_asked(client, tmp_path):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    plain = tmp_path / "plain.zip"
    keyed = tmp_path / "keyed.zip"
    with SessionLocal() as db:
        manifest = system.write_backup(db, plain)
        system.write_backup(db, keyed, include_key=True)
    assert manifest["counts"]["secrets"] == 1 and not manifest["includes_secrets_key"]
    with zipfile.ZipFile(plain) as archive:
        assert system.BACKUP_KEY_MEMBER not in archive.namelist()
        assert SECRET.encode() not in b"".join(archive.read(n) for n in archive.namelist())
    with zipfile.ZipFile(keyed) as archive:
        assert archive.read(system.BACKUP_KEY_MEMBER).strip() == variables.load_key()
    assert system.inspect_backup(keyed)["includes_secrets_key"]
    response = client.get("/api/system/backup?include_key=true")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert system.BACKUP_KEY_MEMBER in archive.namelist()


@pytest.mark.skipif(bool(TEST_DB_URL), reason="database restore is for SQLite")
def test_restoring_on_a_new_home_brings_the_key_when_the_backup_has_it(client, tmp_path):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    keyed, plain = tmp_path / "keyed.zip", tmp_path / "plain.zip"
    with SessionLocal() as db:
        system.write_backup(db, keyed, include_key=True)
        system.write_backup(db, plain)

    fresh = Settings(home=tmp_path / "new-machine", db_url=None)
    lines = system.restore_database(plain, fresh)
    assert any("1 secret(s) were encrypted with a key this home does not have" in line
               for line in lines)

    other = Settings(home=tmp_path / "other-machine", db_url=None)
    lines = system.restore_database(keyed, other)
    assert "Installed the backup's secrets key" in lines
    with contextlib.closing(sqlite3.connect(system._sqlite_path(other))) as conn:
        token = conn.execute("SELECT value FROM variables").fetchone()[0]
    assert Fernet(variables.load_key(other)).decrypt(token.encode()).decode() == SECRET

    # Over a home that has a key of its own: that one is kept beside it.
    lines = system.restore_database(keyed, fresh)
    assert "Installed the backup's secrets key" in lines
    mine = Settings(home=tmp_path / "keyed-machine", db_url=None)
    variables.write_key_file(variables.key_path(mine), Fernet.generate_key())
    lines = system.restore_database(keyed, mine)
    assert any("the previous one is kept as" in line for line in lines)
    assert variables.key_id(variables.load_key(mine)) == variables.key_id(variables.load_key())
    assert system.restore_database(keyed, mine)[-1] == "The backup's secrets key is the one already here"


def test_an_export_carries_variables_but_never_secrets(client):
    add(client, "REGION", "eu")
    add(client, "DB_PASSWORD", SECRET, secret=True)
    from runrail.workflow_io import apply_workflows, export_workflows
    with SessionLocal() as db:
        exported = export_workflows(db)
    assert exported["variables"] == [{"name": "REGION", "value": "eu"}]
    assert SECRET not in json.dumps(exported)
    with SessionLocal() as db:
        summary = apply_workflows(db, {"variables": [{"name": "REGION", "value": "us"},
                                                     {"name": "NEW_ONE", "value": "1"},
                                                     {"name": "db_password", "value": "plain"}]})
    assert summary["updated"] == ["variable REGION"] and summary["created"] == ["variable NEW_ONE"]
    assert summary["skipped"] == ["variable db_password (a secret has that name)"]
    assert variables.current().env["DB_PASSWORD"] == SECRET


def test_a_secret_cannot_be_typed_in_as_a_run_parameter(client):
    add(client, "DB_PASSWORD", SECRET, secret=True)
    workflow = client.post("/api/workflows", json={"name": "p", "enabled": True}).json()
    client.post(f"/api/workflows/{workflow['id']}/tasks", json={
        "name": "t", "task_type": "shell", "command": _python("print(1)"), "depends_on_json": []})
    response = client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {"pw": SECRET}})
    assert response.status_code == 422 and "value of a saved secret" in response.json()["detail"]
    assert client.get("/api/runs").json() == []
    assert client.post(f"/api/workflows/{workflow['id']}/run", json={"parameters": {"pw": "fine"}}).status_code == 201
