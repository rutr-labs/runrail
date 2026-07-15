# RunRail

**A self-hosted workflow orchestrator for the scripts you already have.** Schedule Python scripts, Jupyter notebooks, SQL files, and shell commands with dependencies, retries, logs, backfills, and a fast web UI — without rewriting anything.

[![CI](https://github.com/rutr-labs/runrail/actions/workflows/ci.yml/badge.svg)](https://github.com/rutr-labs/runrail/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

Most teams outgrow cron long before they need a distributed orchestrator. RunRail covers that middle ground: one process, one SQLite file, and your existing code running on a real scheduler with full observability. No DAG files to author, no decorators to add, no platform to operate.

## Features

- **Bring your own code** — point RunRail at a folder; scripts, notebooks, SQL, and shell commands run as-is in isolated subprocesses
- **Dependency graphs with parallel execution** — independent tasks run concurrently; each task starts the moment its dependencies succeed
- **Cron scheduling built for high frequency** — minute-level schedules with per-workflow concurrency limits, coalescing, and missed-run tolerance
- **Managed Python environments** — declare pip requirements in the UI; RunRail builds and atomically swaps isolated virtualenvs, keeping the last working build on failure
- **Failure webhooks with sane semantics** — one alert on the first failure, one on recovery; Slack and Teams incoming webhooks work as-is
- **Auto-pause** — optionally disable a workflow after N consecutive failures instead of failing hundreds of times overnight
- **Backfills and retries** — queue one run per date over a range; re-run any finished run with identical parameters in one click
- **Workflows as code** — `runrail export` / `runrail apply` round-trip definitions through YAML for version control and code review
- **Live dependency graph** — every run renders its task DAG with statuses updating in place as branches execute
- **Live logs and artifacts** — streamed stdout/stderr with ANSI colors, search, tail-follow, timestamped notebook outputs, automatic retention cleanup
- **A UI you'll actually use** — command palette (⌘K), Gantt timelines, activity heatmaps, a full-screen wallboard for the team TV, dark and light themes

## Install

RunRail ships as a single Python package with the web UI bundled in — **no Node required to run it**. Python 3.11+ is the only prerequisite.

```bash
pipx install runrail    # isolated, recommended — or: pip install runrail
runrail serve
```

Then open **http://127.0.0.1:8080**. That's the whole setup.

On first launch RunRail creates its database, logs, and artifact store automatically and starts the API, web UI, scheduler, and worker in a single process — no separate `init` step, no migrations to run by hand. Everything lives in a per-user application-data directory:

| OS | Default location |
|---|---|
| macOS | `~/Library/Application Support/RunRail` |
| Linux | `~/.local/share/RunRail` (honours `$XDG_DATA_HOME`) |
| Windows | `%LOCALAPPDATA%\RunRail` |

Set `RUNRAIL_HOME` to store everything somewhere else (e.g. `RUNRAIL_HOME=./.runrail` to keep data beside a project). For notebook tasks, install the extra: `pipx install "runrail[notebook]"`.

From the UI, connect a project folder, add an environment, and build your first workflow.

## Core concepts

**Projects** point at directories where your code lives. **Environments** define how Python runs — a managed virtualenv built from declared pip requirements, an existing interpreter, or a Conda environment. **Workflows** group **tasks** into a dependency graph with an optional cron schedule. Environments resolve per task, falling back to the workflow default, then the project default; Python and notebook tasks never silently run on RunRail's own interpreter.

The recommended environment setup is *Environments → New environment → Managed Python*: declare requirements such as `pandas==2.3.0`, and RunRail builds an isolated virtualenv, records the build log and Python version, and rebuilds atomically when the requirements change. A failed rebuild preserves the previous working runtime.

## CLI

```bash
runrail run daily-refresh --param region=ca      # queue a manual run
runrail backfill daily-refresh --from 2026-06-01 --to 2026-06-30
runrail export -o workflows.yml                  # workflows as version-controllable YAML
runrail apply workflows.yml                      # declarative upsert by name
runrail import ~/old/.runrail                    # bring a previous setup into this home
runrail cleanup --older-than-days 30 --dry-run   # prune old runs, logs, artifacts
runrail status

# run components as separate processes
runrail api
runrail scheduler
runrail worker --concurrency 8
```

## Templating

Task commands and paths are Jinja templates. Every run receives:

| Variable | Meaning |
|---|---|
| `ds` | Logical date (`2026-07-08`); injected per-date during backfills |
| `ts` / `ts_nodash` | Run timestamp, ISO and compact (`20260708T141005`) |
| `run_id`, `task_run_id` | Identifiers for the current execution |
| `project_root`, `artifacts_dir` | Resolved paths for the run |
| *your parameters* | Run parameters and per-task parameters, merged |

```bash
python scripts/daily.py --date {{ ds }} --region {{ region }}
```

## Notifications

Set `RUNRAIL_NOTIFY_WEBHOOK_URL` globally or a webhook per workflow. RunRail posts on the first failure after a success and again on recovery — never once per red run, so a broken two-minute schedule produces one alert rather than three hundred. The payload's `text` field renders directly in Slack and Microsoft Teams; structured fields are included for custom receivers. Pair with per-workflow auto-pause to stop repeat failures entirely.

## Docker

```bash
docker compose up --build
```

Compose starts RunRail with PostgreSQL and persistent volumes on port 8080. Docker is optional for local use.

## Architecture

A FastAPI process serves the API and the prebuilt React UI. APScheduler acts purely as a clock, writing due runs to the database; while a run executes, the next scheduled iteration coalesces to a single queued run instead of piling up or being dropped. A bounded worker pool (`RUNRAIL_WORKER_CONCURRENCY`) claims queued runs atomically, so **different workflows execute concurrently** and a long job never blocks a frequent one. Runs of the *same* workflow are serialized by its `max_concurrent_runs` (default 1) so a slow run can't overlap its own next iteration. Within a run, the task graph executes with real parallelism (`RUNRAIL_TASK_PARALLELISM`): **independent tasks run concurrently**, each starting the moment its dependencies succeed, and every task is a subprocess with its own logs, timeout, and process-group cleanup.

All of that concurrency happens on one machine (threads and subprocesses); RunRail does not yet distribute work across remote worker nodes.

SQLite in WAL mode is the default store; set `RUNRAIL_DB_URL` for PostgreSQL. Logs live under `$RUNRAIL_HOME/logs/run_<id>/`, artifacts under `$RUNRAIL_HOME/artifacts/<id>/` with timestamped filenames so frequent runs and retries never collide.

## Configuration

| Variable | Default |
|---|---|
| `RUNRAIL_HOME` | Per-user data dir (see [Install](#install)) |
| `RUNRAIL_DB_URL` | SQLite at `$RUNRAIL_HOME/runrail.db` |
| `RUNRAIL_HOST` / `RUNRAIL_PORT` | `127.0.0.1` / `8080` |
| `RUNRAIL_WORKER_CONCURRENCY` | `4` concurrent runs |
| `RUNRAIL_TASK_PARALLELISM` | `4` concurrent tasks per run |
| `RUNRAIL_RETENTION_DAYS` | unset; when set, finished runs older than this are deleted with their logs and artifacts |
| `RUNRAIL_NOTIFY_WEBHOOK_URL` | unset; default failure/recovery webhook |
| `RUNRAIL_BROWSE_ROOT` | user home; confines the UI file picker |

Schedules evaluate in UTC; the UI displays them in your local timezone. For deployments reachable beyond localhost, set `RUNRAIL_BROWSE_ROOT` to a narrow directory and place the server behind authentication — RunRail does not yet ship its own.

## Current limitations

- Runs on a single machine — workflows and independent tasks run concurrently there, but work is not distributed across remote worker nodes yet
- No built-in authentication or RBAC
- SQL tasks execute against SQLite only
- Secrets are plain environment variables

## Roadmap

Warehouse and database adapters, artifact previews, secrets management, API tokens and authentication, and richer run analytics. See open issues for details.

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check .
cd frontend && npm install && npm run build   # writes the bundle into src/runrail/web/static
```

Licensed under [Apache-2.0](LICENSE).
