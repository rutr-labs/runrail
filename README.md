# RunRail

RunRail is a lightweight, open-source workflow control plane for the Python scripts, notebooks, SQL files, and shell commands you already have. It adds schedules, dependency-ordered workflows, logs, retries, backfills, artifacts, and a practical web UI without requiring application rewrites.

RunRail is **not** an enterprise orchestrator, a hosted compute platform, or a replacement for Kubernetes. The alpha is intentionally local/self-hosted, subprocess-based, and easy to understand.

## Why it exists

Teams often outgrow cron long before they need Airflow, Dagster, or Prefect. RunRail occupies that useful middle: a local Databricks Workflows-like experience whose first principle is “bring your existing code.”

## Install and quickstart

Python 3.11 or newer is required. Node is only needed when changing the frontend.

```bash
pip install -e .
runrail init
runrail serve
```

Open http://127.0.0.1:8080. Create a project pointing at your code directory, create a separate Python environment, then create a workflow and add its tasks. `runrail serve` starts the API, bundled UI, scheduler, and local worker together—no npm, separate worker, or separate scheduler.

Python and notebook tasks never fall back to RunRail's own interpreter. Select an environment on the task, workflow, or project (in that precedence order).

The recommended flow is **Environments → New environment → Managed Python**. A managed environment is RunRail's local equivalent of a Databricks compute runtime:

- RunRail creates an isolated virtual environment under `RUNRAIL_HOME/environments`.
- You declare pip requirements (for example `pandas==2.3.0` or `sqlalchemy>=2,<3`) in the UI.
- Build status, Python version, errors, and the latest pip build log are retained.
- Editing libraries performs an atomic rebuild. A failed rebuild preserves the last working runtime.
- The environment can be assigned at project, workflow, or task level.

Managed environments work with the selected base Python on Windows, macOS, and Linux. You can alternatively register an existing Python/virtualenv executable or a Conda environment; external runtimes are validated before they can be assigned. For repeatable production jobs, pin package versions or bounded version ranges.

The equivalent external-environment setup is:

```bash
python -m venv .venv
.venv/bin/python -m pip install pandas sqlalchemy papermill
```

## CLI

```bash
runrail run daily-refresh --param region=ca
runrail backfill daily-refresh --from 2026-06-01 --to 2026-06-30
runrail status

# Advanced split processes
runrail api
runrail scheduler
runrail worker
```

Backfills are inclusive and inject `ds` into each run. Templates also receive `ts`, `ts_nodash` (e.g. `20260702T141005`, for high-frequency schedules where a date alone is ambiguous), `run_id`, `workflow_run_id`, `task_run_id`, `project_root`, `artifacts_dir`, and all task/run parameters.

Old finished runs and their log/artifact files can be pruned with `runrail cleanup --older-than-days 30` (add `--dry-run` to preview), or automatically by setting `RUNRAIL_RETENTION_DAYS` — recommended for workflows scheduled every few minutes.

## Examples

### Python script

Create a Python task with `script_path` set to `examples/simple-python/hello.py`. RunRail adds the project root, working directory, script/notebook directory, and a conventional project `src/` directory to `PYTHONPATH`; scripts inside Python packages run with `python -m`, so project, sibling, src-layout, and package-relative imports work. It intentionally does not add every recursive directory because that makes module resolution ambiguous. For arguments, use a command task:

```bash
python examples/simple-python/hello.py --date {{ ds }}
```

### Notebook with Papermill

Install Papermill in the selected task environment, then create a notebook task with its input path. The executed notebook is written under `.runrail/artifacts/<run-id>/`, named with the run timestamp and task-run id so frequent runs and retries never overwrite each other.

### Multi-task workflow

Create `extract` as a shell task, then `transform` as a Python or shell task with `depends_on_json` containing `extract` (the UI accepts comma-separated names). Tasks with no dependency between them run in parallel; a task starts once all of its dependencies succeed. Failed dependencies skip downstream tasks.

### SQL

SQL tasks execute SQLite scripts only. Set a run parameter such as `sqlite_db_path=/tmp/demo.db` and point `sql_path` to `examples/sql-task/schema.sql`.

## Docker Compose

```bash
docker compose up --build
```

Compose runs RunRail and PostgreSQL, persists both data volumes, and exposes the UI on port 8080. Local usage does not require Docker.

## Architecture

FastAPI serves the API and prebuilt React application. APScheduler is only a scheduling clock: it writes due runs into the SQL database; while a run is executing, the next scheduled iteration is queued (coalesced to one waiting run) rather than dropped. A local worker atomically claims queued workflow runs and executes them on a bounded thread pool (`RUNRAIL_WORKER_CONCURRENCY`, default 4, or `runrail worker --concurrency N`), so different workflows run in parallel — a one-hour job never blocks a five-minute schedule. Runs of the same workflow are serialized according to its `max_concurrent_runs` (default 1). Each run resolves its task graph and executes every user task in a subprocess; independent tasks in a run execute in parallel (`RUNRAIL_TASK_PARALLELISM`, default 4), and each task starts as soon as everything it depends on has succeeded. stdout/stderr live under `.runrail/logs/run_<run-id>/`; generated artifacts live under `.runrail/artifacts/<run-id>/` with timestamped filenames. SQLite is the default (WAL mode) and `RUNRAIL_DB_URL` enables PostgreSQL.

Subprocesses receive the selected runtime's `PATH`, project-root `PYTHONPATH`, environment variables, working directory, timeout, and logs. Python package scripts run as modules when needed for relative imports. Timeouts terminate the subprocess group/tree so spawned child processes are not left behind.

Frontend development uses `cd frontend && npm install && npm run build`; Vite writes the production bundle directly into `src/runrail/web/static`.

## Configuration

| Variable | Default |
|---|---|
| `RUNRAIL_HOME` | `.runrail` |
| `RUNRAIL_DB_URL` | SQLite at `.runrail/runrail.db` |
| `RUNRAIL_HOST` | `127.0.0.1` |
| `RUNRAIL_PORT` | `8080` |
| `RUNRAIL_WORKER_CONCURRENCY` | `4` |
| `RUNRAIL_TASK_PARALLELISM` | `4` (max tasks of one run executing at once) |
| `RUNRAIL_BROWSE_ROOT` | Current user's home directory |
| `RUNRAIL_RETENTION_DAYS` | Unset (keep runs forever); when set, finished runs older than this are auto-deleted with their logs and artifacts |

The UI includes a server-side file picker for project roots, task files, Python executables, and working directories. For deployments exposed beyond a trusted local network, set `RUNRAIL_BROWSE_ROOT` to a narrow mounted project directory. Paths outside that root are rejected.

## Current limitations

- No remote workers, authentication/RBAC, SSO, visual DAG editor, or Kubernetes executor
- Minimal SQLite-only task SQL support
- Basic environment-variable secrets only
- No parallel task execution inside a workflow

## Roadmap

Remote workers and worker tokens; PostgreSQL, SQL Server, Snowflake, and Databricks SQL adapters; a Databricks Jobs adapter; artifact previews; Slack, Teams, email, and webhook notifications; secrets management; RBAC; React DAG visualization; and an optional hosted control plane.

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

Licensed under Apache-2.0.
