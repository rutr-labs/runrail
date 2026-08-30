# RunRail

**A self-hosted job scheduler with a web UI, for the scripts you already have.** Schedule Python scripts, Jupyter notebooks, SQL files and shell commands with dependencies, retries, backfills, run history and live logs. Nothing gets rewritten. A cron alternative for people who don't want to run Airflow.

[![CI](https://github.com/rutr-labs/runrail/actions/workflows/ci.yml/badge.svg)](https://github.com/rutr-labs/runrail/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/runrail.svg)](https://pypi.org/project/runrail/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

Most people outgrow cron long before they need a distributed orchestrator. Cron gives you no history, no retries, no idea why last Tuesday's job didn't run. Airflow gives you all of that plus a platform to operate. RunRail is the bit in between: one Python process, one SQLite file, and your existing code running on a real scheduler with the observability you'd expect.

No DAG files to author. No decorators to add. No Docker, no Postgres, no broker, no paid tier.

```bash
pipx install runrail && runrail serve
```

![RunRail dashboard](docs/img/01-dashboard.png)

## What you get

**Running things**

- Point RunRail at a folder. Scripts, notebooks, SQL and shell commands run as-is, each in its own subprocess with its own logs, timeout and process-group cleanup.
- Dependency graphs with real parallelism. Independent tasks start the moment their dependencies succeed.
- Retries, per-workflow concurrency limits, and backfills that queue one run per date over a range.
- **Resume from the failed task.** A three-hour step 1 doesn't run twice because step 4 broke. The run picks up in place, keeping its id, its `ds` and its artifacts, and re-executes only what didn't succeed.
- **Approval gates.** Mark a task as needing a human and the run parks there, showing the prompt and the exact command about to execute. It releases its worker thread while it waits, so a gate left open overnight doesn't block anything else.
- **Resource locks.** Name a thing that shouldn't have two jobs in it at once, like a warehouse or a licence, and say whether a workflow needs it alone or can share. The monthly maintenance job gets its turn instead of losing to a drip of hourly ones.

**Knowing what happened**

- Live logs with ANSI colour, in-log search and tail-follow, plus a Gantt timeline and a dependency graph that light up as branches execute.
- **Log search across every run**, for when the real question is "when did this error first appear?"
- **Missed runs are history, not just an alert.** A schedule that came due while the machine was asleep shows up next to the successes and failures, and RunRail can tell you when a schedule goes quiet.
- Per-task duration trends, so a job that has quietly gone four times slower says so before it starts timing out.
- Run notes, so the reason a failure was dismissed outlives the person who dismissed it.
- Failure webhooks with sane semantics: one alert on the first failure, one on recovery, never three hundred overnight. Slack and Microsoft Teams (Power Automate) URLs work as-is.

**Notebooks and reports**

- Executed notebooks render as HTML on the run page, charts and all, with the `.ipynb` one click away.
- Every workflow gets a `/reports/<name>/latest` URL that always resolves to the newest successful run, so you can pin it somewhere and stop re-sending screenshots.
- Any run exports as one self-contained HTML file you can email to someone who doesn't have access.

**Operating it**

- Timezone-aware schedules built from dropdowns, with raw cron still there under Advanced.
- Snooze a workflow until tomorrow morning instead of disabling it and forgetting.
- Auto-pause after N consecutive failures, and a "must finish by" deadline that alerts while the run is still going.
- `runrail export` / `runrail apply` round-trip everything through YAML for version control.
- A full REST API with live OpenAPI docs at `/docs`, if you'd rather drive it yourself.

## Install

RunRail ships as one Python package with the web UI already built into it. No Node at runtime, no CDN, nothing to compile. Python 3.11 or newer is the only prerequisite.

```bash
pipx install runrail    # isolated, recommended — or: pip install runrail
runrail serve
```

The first launch asks whether you want to import an existing setup; press Enter to start fresh. Then open **http://127.0.0.1:8080**.

Cold start is about 0.6 seconds, including creating the database and applying migrations. Idle memory is around 96 MB. It is one process and roughly seven threads, and it stays that way.

Everything lives in a per-user application-data directory:

| OS | Default location |
|---|---|
| macOS | `~/Library/Application Support/RunRail` |
| Linux | `~/.local/share/RunRail` (honours `$XDG_DATA_HOME`) |
| Windows | `%LOCALAPPDATA%\RunRail` |

Set `RUNRAIL_HOME` to keep data elsewhere, e.g. `RUNRAIL_HOME=./.runrail` to put it beside a project. RunRail also reads a `.env` file from the directory you launch it in, which is worth knowing if you start it from different places.

To render executed notebooks as HTML reports, install the extra: `pipx install "runrail[notebook]"`. You don't need it to *run* notebooks; RunRail installs papermill into the task's own environment for that.

From the UI, connect a project folder, add an environment, and build your first workflow.

## A look around

A run, with independent tasks overlapping on the timeline and every attempt's logs kept:

![Run detail](docs/img/02-run-detail.png)

A run parked on an approval gate. It shows the prompt whoever built the workflow wrote, the exact command about to execute, and what already succeeded upstream:

![Approval gate](docs/img/05-approval.png)

And the wallboard, for a screen on the wall. Live runs show progress against their own median duration, and drift amber when they overrun it:

![Live wallboard](docs/img/demo.gif)

## Core concepts

**Projects** point at directories where your code lives. **Environments** define how Python runs: a managed virtualenv built from declared pip requirements, an existing interpreter, or a Conda environment. **Workflows** group **tasks** into a dependency graph with an optional schedule. Environments resolve per task, falling back to the workflow default and then the project default. Python and notebook tasks never silently run on RunRail's own interpreter, which is deliberate: your dependencies are not its dependencies.

The recommended setup is *Environments → New environment → Managed Python*. Declare requirements like `pandas==2.3.0` and RunRail builds an isolated virtualenv, records the build log and Python version, and rebuilds atomically when the requirements change. A failed rebuild keeps the previous working runtime. Note that every managed environment also gets papermill and ipykernel so notebook tasks work, which costs about 127 MB before your own packages.

## CLI

```bash
runrail serve                                    # API, UI, scheduler and worker in one process
runrail run daily-refresh --param region=ca      # queue a manual run
runrail backfill daily-refresh --from 2026-06-01 --to 2026-06-30
runrail export -o workflows.yml                  # workflows as version-controllable YAML
runrail apply workflows.yml                      # declarative upsert by name
runrail import ~/old/.runrail                    # bring a previous setup into this home
runrail cleanup --older-than-days 30 --dry-run   # prune old runs, logs, artifacts
runrail status

# or run the components separately
runrail api
runrail scheduler
runrail worker --concurrency 8
```

## Templating

Task commands and paths are Jinja templates. Every run receives:

| Variable | Meaning |
|---|---|
| `ds` | Logical date (`2026-07-08`), injected per-date during backfills |
| `ts` / `ts_nodash` | Run timestamp, ISO and compact (`20260708T141005`) |
| `run_id`, `task_run_id` | Identifiers for the current execution |
| `project_root`, `artifacts_dir` | Resolved paths for the run |
| *your parameters* | Run parameters and per-task parameters, merged |

```bash
python scripts/daily.py --date {{ ds }} --region {{ region }}
```

## Notifications

Set `RUNRAIL_NOTIFY_WEBHOOK_URL` globally, or a webhook per workflow. RunRail posts on the first failure after a success and again on recovery, so a two-minute schedule that breaks overnight produces one alert instead of three hundred. Nine event kinds are sent in total, covering failures, recoveries, auto-pause, approvals, missed schedules and SLA breaches.

Slack and custom receivers get a JSON payload whose `text` field renders directly. For Microsoft Teams, create a Workflow from the "when a webhook request is received" template (Teams → channel → Workflows, since Microsoft retired the classic Office 365 connectors) and paste the generated URL. RunRail recognises Teams URLs and sends the Adaptive Card envelope that flow expects.

## Docker

```bash
docker compose up --build
```

Compose starts RunRail with PostgreSQL and persistent volumes on port 8080. Docker is entirely optional; the point of this project is that you don't need it.

## Architecture

A FastAPI process serves the API and the prebuilt React UI. APScheduler acts purely as a clock, writing due runs to the database. While a run executes, the next scheduled iteration coalesces into a single queued run rather than piling up or being dropped, and a 60-second watchdog checks for schedules that have gone quiet or runs that have blown their deadline.

A bounded worker pool (`RUNRAIL_WORKER_CONCURRENCY`) claims queued runs atomically, so **different workflows execute concurrently** and a long job never blocks a frequent one. Runs of the *same* workflow are serialised by its `max_concurrent_runs` so a slow run can't overlap its own next iteration. Within a run, the task graph executes with real parallelism (`RUNRAIL_TASK_PARALLELISM`): **independent tasks run concurrently**, each starting as soon as its dependencies succeed.

All of that happens on one machine, with threads and subprocesses. RunRail does not distribute work across remote workers.

SQLite in WAL mode is the default store. Set `RUNRAIL_DB_URL=postgresql+psycopg://user:password@host:5432/runrail` for PostgreSQL; the `+psycopg` names the bundled psycopg 3 driver, since a bare `postgresql://` URL reaches for psycopg2, which RunRail doesn't install. CI runs the full test suite against both backends. Logs live under `$RUNRAIL_HOME/logs/`, artifacts under `$RUNRAIL_HOME/artifacts/<id>/`, with timestamped filenames so frequent runs and retries never collide.

## Configuration

| Variable | Default |
|---|---|
| `RUNRAIL_HOME` | Per-user data dir (see [Install](#install)) |
| `RUNRAIL_DB_URL` | SQLite at `$RUNRAIL_HOME/runrail.db` |
| `RUNRAIL_HOST` / `RUNRAIL_PORT` | `127.0.0.1` / `8080` |
| `RUNRAIL_WORKER_CONCURRENCY` | `4` concurrent runs |
| `RUNRAIL_TASK_PARALLELISM` | `4` concurrent tasks per run |
| `RUNRAIL_WORKER_POLL_SECONDS` | `1.0` |
| `RUNRAIL_RETENTION_DAYS` | unset; when set, finished runs older than this are deleted with their logs and artifacts |
| `RUNRAIL_NOTIFY_WEBHOOK_URL` | unset; default failure/recovery webhook |
| `RUNRAIL_PUBLIC_URL` | unset; base URL for links in exported files and reports |
| `RUNRAIL_BROWSE_ROOT` | user home; confines the UI file picker |
| `RUNRAIL_SQLITE_DB_PATH` | unset; the database SQL tasks run against, unless the task sets `sqlite_db_path` |

Schedules evaluate in each workflow's own timezone, so "daily at 9:00" means 9:00 on that workflow's wall clock across DST changes. The UI shows occurrences in your local time.

## Good to know

- **It's built for one person on one machine.** There are no accounts and no RBAC, and that's a design decision rather than a missing feature. If you expose the port beyond localhost, put it behind something that does authentication, and narrow `RUNRAIL_BROWSE_ROOT` while you're there.
- Work runs on a single machine. Different workflows and independent tasks run concurrently, but there are no remote workers.
- SQL tasks execute against SQLite only.
- Environment variables, including secrets, are stored unencrypted in the database.
- Log search is a bounded grep rather than an index. Every dimension is capped, and the response tells you which cap it hit, so a partial result never poses as a full history.
- Missed-run history is recomputed from the current schedule, so editing a crontab re-renders the past under the new one.

## Development

```bash
pip install -e '.[dev,notebook]'
pytest                                        # 277 backend tests
ruff check src tests

cd frontend
npm install
npm run build     # required from a clone: the built UI is gitignored, not committed
npm test          # 143 frontend tests
```

The frontend tests include a table of cron fire times generated by the backend's own scheduler, so if the UI ever starts previewing a run time the scheduler wouldn't fire, the build fails. `scripts/seed_demo.py` fills a throwaway home with a realistic history if you need something to look at.

Licensed under [Apache-2.0](LICENSE).
