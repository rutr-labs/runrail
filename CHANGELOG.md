# Changelog

Notable changes to RunRail. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Aggregated stats endpoints: `GET /api/stats/summary` computes the dashboard and run-history metrics (live counts, 24-hour totals, 7-day success rate, average duration) in SQL, so the numbers stay accurate no matter how much history the browser has loaded.
- Activity heatmap range selector: 4, 8, or 16 weeks or 6 months on the dashboard and workflow pages, remembered across sessions. The grid runs Monday through Sunday with every weekday labelled.
- Task timeouts can be entered in seconds, minutes, hours, or days; values are stored as seconds and shown in the friendliest unit.
- First-run import: when `runrail serve` targets a brand-new home from an interactive terminal, it offers to bring over an existing setup — either a previous RunRail data directory (database, logs, artifacts, and environments are copied after an integrity check, then upgraded by the normal startup migrations) or a workflows YAML from `runrail export`. Also available any time as `runrail import <path>`; it never overwrites an existing database.

### Changed
- Running progress bars redesigned as a comet: the fill grows with progress behind a bright twinkling head and leaves a dimmer, still-lit dot trail. Past the median duration the trail gradually shifts toward amber — fully amber at 135% — while the head holds its color until it catches up. One reusable treatment covers the wallboard, the run timeline, and indeterminate loaders.
- The run timeline renders while a run is executing; running bars grow against the wall clock instead of the timeline appearing only after the run finishes.
- Wallboard tiles are larger and clickable — each tile links to its workflow page.
- Scheduled runs no longer record a `ds` parameter they never used; `{{ ds }}` still renders (defaulting to the run date) and backfills still set it explicitly.
- Legibility pass: status chips use full-strength status colors, project cards match the workflow-card layout, and dependency-graph nodes got larger type.

### Docs
- Clarified the concurrency model to remove a wording contradiction: different workflows and independent tasks run concurrently on one machine, runs of the same workflow serialize by `max_concurrent_runs`, and distributed/remote workers are not supported yet.

## [0.3.0] — 2026-07-09

### Added
- Live dependency graph: workflow pages render the task DAG, and run pages overlay live statuses — nodes highlight while running, edges trace as branches execute.
- Gantt timeline v2: one lane per task with retry attempts as separate bars, a time axis, and detailed hover tooltips; parallel execution is visible at a glance, and running bars grow live against the wall clock.
- ANSI color rendering in the log viewer (16-color, 256-color, and truecolor), plus in-log search with match navigation and tail-follow.
- Daily activity heatmap on the dashboard and per workflow, backed by a new `GET /api/stats/daily` aggregation endpoint.
- Wallboard: a full-screen, auto-refreshing status view at `/wallboard` for a team TV — health verdict, live-run cards with ETA, urgency-sorted tiles, failure streaks, overdue-schedule detection, and a failures strip.
- Sparkle-fill progress indicator: running progress bars render as a dot-matrix that fills left-to-right with a scattered sparkle as a run advances toward its median duration; reused as a shared `LoadingBar` for environment builds and queued runs.
- Graceful shutdown: `runrail serve` reports which runs are still executing (with a median-based ETA) on the first Ctrl+C and force-quits on the second; interrupted runs are recovered as failed on the next start so a killed worker never leaves a phantom run holding a concurrency slot.

### Changed
- Dashboard reorganized: the activity heatmap moved into the hero beside the headline, removing a large half-empty panel.
- Run and dashboard views refresh reliably while live — a polling regression (the interval was torn down every render) is fixed, so the Gantt and statuses update without a manual reload.

### Fixed
- Removed a heavy visual-effects pass (film grain, cursor-tracked specular wash, and a modal depth-of-field blur) that desaturated content and hurt readability; surfaces are flat and legible again in both themes.
- WebSocket log streams close promptly instead of hanging graceful shutdown, and no longer emit `CancelledError` tracebacks on exit.

## [0.2.0] — 2026-07-08

### Added
- Failure notifications: per-workflow or global webhook (`RUNRAIL_NOTIFY_WEBHOOK_URL`), posting on the first failure after a success and again on recovery. Slack and Teams incoming webhooks work without configuration.
- Auto-pause: workflows can disable themselves after N consecutive failures, with a webhook notice.
- Run retry: `POST /api/runs/{id}/retry` and a Retry button on the run page re-queue any finished run with identical parameters.
- Workflows as code: `runrail export` and `runrail apply` round-trip workflow and task definitions through YAML, referencing projects and environments by name for portability across machines.
- GitHub Actions CI: lint and tests on Python 3.11/3.12 plus a frontend typecheck and build.
- Live log tailing follows output as it streams, with a terminal-style caret while a task is running.

### Fixed
- Windows: managed environment builds no longer fail with `WinError 5` when OneDrive or antivirus briefly locks the build directory; directory promotion now retries and falls back to copy.
- Hourly cron schedules (`30 * * * *`) were displayed as daily; schedule previews and the Upcoming list now interpret them correctly.
- Schedule labels now render in the viewer's local timezone, matching the timestamps shown beside them.
- Dark mode: native dropdown options and the date-picker icon are now legible on all platforms.
- Reduced-motion environments (including Windows with animation effects off) keep functional indicators: running spinners continue, entrances fall back to fades instead of disappearing.

## [0.1.0] — 2026-07-06

Initial public release: projects, managed Python environments, workflows with dependency-ordered tasks (shell, Python, notebook, SQL), cron scheduling with coalescing, parallel task execution, backfills, retries, live logs, artifacts with retention cleanup, and the bundled web UI.

[Unreleased]: https://github.com/rutr-labs/runrail/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/rutr-labs/runrail/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rutr-labs/runrail/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rutr-labs/runrail/releases/tag/v0.1.0
