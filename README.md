# RunRail

Schedule the scripts you already have. Python, Jupyter notebooks, SQL and shell commands, with dependencies, retries, backfills and a full record of every run.

[![CI](https://github.com/rutr-labs/runrail/actions/workflows/ci.yml/badge.svg)](https://github.com/rutr-labs/runrail/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/runrail.svg)](https://pypi.org/project/runrail/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

RunRail is one Python process and one SQLite file. You point it at a folder, describe when things should run and what depends on what, and it takes care of the rest: running them, keeping the logs, retrying what fails, and telling you when something breaks.

Your code stays yours. No DAG files, no decorators, no imports. If it runs in a terminal today, it runs here.

```bash
pipx install runrail && runrail serve
```

![RunRail dashboard](docs/img/01-dashboard.png)

## What it does

**Runs your work**

- Scripts, notebooks, SQL files and shell commands, each in its own subprocess with its own logs, timeout and cleanup.
- Task graphs run with real parallelism. Independent tasks start as soon as their dependencies finish.
- Retries, per-workflow concurrency limits, and backfills that queue one run per date across a range.
- Resume a failed run from the task that broke. Everything that already succeeded is kept, so a three-hour first step doesn't run twice.
- Approval gates. A task can wait for a person, showing the prompt and the exact command it's about to run. The run steps aside while it waits, so nothing else is held up.
- Resource locks. Name something that can't take two jobs at once, like a warehouse or a licence, and say whether a workflow needs it alone or can share.

**Tells you what happened**

- Live logs with colour, search and tail-follow, next to a timeline and a dependency graph that update as the run moves.
- Search the logs of every run at once, for when the question is really "when did this start happening?"
- Schedules that came due while the machine was asleep are shown as missed, alongside the runs that did happen.
- Per-task duration history, with a flag when something has quietly got slower.
- Notes on a run, so the reason a failure was ignored is still there next month.
- Webhooks on the first failure and again on recovery, not once per red run. Slack and Microsoft Teams work as-is.

**Turns notebooks into reports**

- Executed notebooks render as HTML on the run page, charts and tables included, with the `.ipynb` still one click away.
- `/reports/<workflow>/latest` always points at the newest successful run, so the link you shared stays current.
- Any run exports as a single HTML file you can send to someone who doesn't have access.

**Stays out of your way**

- Schedules built from dropdowns, in the workflow's own timezone. Raw cron is still there under Advanced.
- Snooze a workflow until tomorrow instead of disabling it and forgetting.
- Auto-pause after repeated failures, and a deadline that alerts while a run is still going.
- `runrail export` and `runrail apply` round-trip everything through YAML.
- A REST API with live docs at `/docs`.

## Install

One package, with the web UI already built in. Python 3.11 or newer, nothing else.

```bash
pipx install runrail    # isolated, recommended. Or: pip install runrail
runrail serve
```

The first launch offers to import an existing setup. Press Enter to start fresh, then open **http://127.0.0.1:8080**.

Startup takes about 0.6 seconds, including creating the database and applying migrations. It sits at roughly 96 MB of memory as a single process, and stays there.

Data lives in a per-user directory:

| OS | Location |
|---|---|
| macOS | `~/Library/Application Support/RunRail` |
| Linux | `~/.local/share/RunRail` (honours `$XDG_DATA_HOME`) |
| Windows | `%LOCALAPPDATA%\RunRail` |

`RUNRAIL_HOME` moves it, for example `RUNRAIL_HOME=./.runrail` to keep it beside a project. RunRail also reads a `.env` from the directory you start it in, which matters if you launch it from more than one place.

Rendering notebooks as HTML reports needs the extra: `pipx install "runrail[notebook]"`. Running notebooks does not; papermill is installed into the task's own environment for that.

## A look around

A run in progress. Independent tasks overlap on the timeline, and every attempt keeps its own logs.

![Run detail](docs/img/02-run-detail.png)

A run waiting on approval. It shows the prompt from whoever built the workflow, the command about to execute, and what already succeeded.

![Approval gate](docs/img/05-approval.png)

The wallboard, for a screen on the wall. Progress is measured against each workflow's own median duration, and turns amber when a run goes over it.

![Live wallboard](docs/img/demo.gif)

## How it fits together

**Projects** point at the directories your code lives in. **Environments** say how Python runs: a managed virtualenv built from pip requirements, an interpreter you already have, or a Conda environment. **Workflows** group **tasks** into a dependency graph with an optional schedule.

Environments resolve per task, then per workflow, then per project. Python and notebook tasks will not fall back to RunRail's own interpreter, which is deliberate. Your dependencies are not its dependencies.

For most setups, *Environments → New environment → Managed Python* is the one to use. Declare `pandas==2.3.0` and RunRail builds an isolated virtualenv, records the build log and Python version, and rebuilds atomically when the requirements change. A failed rebuild keeps the last working one. Each managed environment also carries papermill and ipykernel so notebook tasks work, which is about 127 MB before your own packages.

## CLI

```bash
runrail serve                                    # API, UI, scheduler and worker together
runrail run daily-refresh --param region=ca      # queue a run now
runrail backfill daily-refresh --from 2026-06-01 --to 2026-06-30
runrail export -o workflows.yml                  # workflows as YAML you can commit
runrail apply workflows.yml                      # declarative upsert by name
runrail import ~/old/.runrail                    # bring an existing setup into this home
runrail cleanup --older-than-days 30 --dry-run   # prune old runs, logs and artifacts
runrail status

# or split the process up
runrail api
runrail scheduler
runrail worker --concurrency 8
```

## Templating

Commands and paths are Jinja templates. Every run gets:

| Variable | Meaning |
|---|---|
| `ds` | Logical date (`2026-07-08`), set per-date during backfills |
| `ts` / `ts_nodash` | Run timestamp, ISO and compact (`20260708T141005`) |
| `run_id`, `task_run_id` | Identifiers for this execution |
| `project_root`, `artifacts_dir` | Resolved paths for the run |
| *your parameters* | Run and per-task parameters, merged |

```bash
python scripts/daily.py --date {{ ds }} --region {{ region }}
```

## Notifications

Set `RUNRAIL_NOTIFY_WEBHOOK_URL`, or a webhook per workflow. RunRail posts on the first failure after a success, and again when it recovers. A schedule that breaks at midnight produces one message, not three hundred. Nine event kinds are sent in all, covering failures, recoveries, auto-pause, approvals, missed schedules and deadlines.

Slack and custom receivers get JSON whose `text` field renders directly. For Microsoft Teams, make a Workflow from the "when a webhook request is received" template (Teams → channel → Workflows) and paste the URL. RunRail recognises Teams URLs and sends the Adaptive Card envelope that flow expects.

## Architecture

FastAPI serves the API and the prebuilt React UI. APScheduler is used purely as a clock, writing due runs into the database. While a run executes, the next scheduled iteration collapses into a single queued run rather than stacking up or disappearing, and a 60-second watchdog looks for schedules that have gone quiet and runs that have missed a deadline.

A bounded worker pool (`RUNRAIL_WORKER_CONCURRENCY`) claims queued runs atomically, so different workflows execute at the same time and a long job never blocks a frequent one. Runs of the same workflow are serialised by its `max_concurrent_runs`. Inside a run, the task graph executes with real parallelism (`RUNRAIL_TASK_PARALLELISM`).

All of it happens on one machine, using threads and subprocesses. There are no remote workers.

SQLite in WAL mode is the default. For PostgreSQL, set `RUNRAIL_DB_URL=postgresql+psycopg://user:password@host:5432/runrail`; the `+psycopg` matters, because a bare `postgresql://` looks for psycopg2, which isn't installed. CI runs the whole suite against both. Logs live under `$RUNRAIL_HOME/logs/` and artifacts under `$RUNRAIL_HOME/artifacts/<id>/`, timestamped so retries never overwrite each other.

## Configuration

| Variable | Default |
|---|---|
| `RUNRAIL_HOME` | Per-user data directory (see [Install](#install)) |
| `RUNRAIL_DB_URL` | SQLite at `$RUNRAIL_HOME/runrail.db` |
| `RUNRAIL_HOST` / `RUNRAIL_PORT` | `127.0.0.1` / `8080` |
| `RUNRAIL_WORKER_CONCURRENCY` | `4` concurrent runs |
| `RUNRAIL_TASK_PARALLELISM` | `4` concurrent tasks per run |
| `RUNRAIL_WORKER_POLL_SECONDS` | `1.0` |
| `RUNRAIL_RETENTION_DAYS` | unset; when set, finished runs older than this are deleted with their logs and artifacts |
| `RUNRAIL_NOTIFY_WEBHOOK_URL` | unset; default webhook |
| `RUNRAIL_PUBLIC_URL` | unset; base URL for links inside exported files and reports |
| `RUNRAIL_BROWSE_ROOT` | user home; limits the UI file picker |
| `RUNRAIL_SQLITE_DB_PATH` | unset; the database SQL tasks run against, unless the task sets `sqlite_db_path` |

Schedules evaluate in each workflow's own timezone, so "daily at 9:00" stays 9:00 on that workflow's clock across DST. The UI shows occurrences in yours.

## Docker

```bash
docker compose up --build
```

Starts RunRail with PostgreSQL and persistent volumes on port 8080. It's optional; the point of the project is that you don't need it.

## Worth knowing

- It's built for one person on one machine. There are no accounts and no roles, by choice rather than omission. If you expose the port beyond localhost, put something in front of it that handles authentication, and narrow `RUNRAIL_BROWSE_ROOT` while you're there.
- Work runs on a single machine. Plenty happens in parallel there, but there are no remote workers.
- SQL tasks run against SQLite only.
- Environment variables, including anything secret, are stored unencrypted in the database.
- Log search is a bounded grep rather than an index. Every limit is capped and the response says which cap it hit, so a partial answer never looks like a complete one.
- Missed-run history is recomputed from the current schedule, so editing a cron expression redraws the past under the new one.

## Development

```bash
pip install -e '.[dev,notebook]'
pytest                                        # 277 backend tests
ruff check src tests

cd frontend
npm install
npm run build     # needed from a clone; the built UI is gitignored, not committed
npm test          # 143 frontend tests
```

The frontend tests check cron previews against fire times generated by the backend's own scheduler, so if the UI ever promises a run time the scheduler wouldn't produce, the build fails. `scripts/seed_demo.py` fills a throwaway home with a realistic history if you want something to look at.

Licensed under [Apache-2.0](LICENSE).
