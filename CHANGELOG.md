# Changelog

Notable changes to RunRail. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-07-09

### Added
- Live dependency graph: workflow pages render the task DAG, and run pages overlay live statuses — nodes highlight while running, edges trace as branches execute.
- Gantt timeline v2: one lane per task with retry attempts as separate bars, a time axis, and detailed hover tooltips; parallel execution is visible at a glance, and running bars grow live against the wall clock.
- ANSI color rendering in the log viewer (16-color, 256-color, and truecolor), plus in-log search with match navigation and tail-follow.
- GitHub-style activity heatmap on the dashboard and per workflow, backed by a new `GET /api/stats/daily` aggregation endpoint.
- Wallboard: a full-screen, auto-refreshing status view at `/wallboard` for a team TV — health verdict, live-run cards with ETA, urgency-sorted tiles, failure streaks, overdue-schedule detection, and a failures strip.
- Sparkle-fill progress indicator: running progress bars render as a dot-matrix that fills left-to-right with a scattered sparkle as a run advances toward its median duration; reused as a shared `LoadingBar` for environment builds and queued runs.
- Graceful shutdown: `runrail serve` reports which runs are still executing (with a median-based ETA) on the first Ctrl+C and force-quits on the second; interrupted runs are recovered as failed on the next start so a killed worker never leaves a phantom run holding a concurrency slot.

### Changed
- Dashboard reorganized: the activity heatmap moved into the hero beside the headline, removing a large half-empty panel.
- Run and dashboard views refresh reliably while live — a polling regression (the interval was torn down every render) is fixed, so the Gantt and statuses update without a manual reload.

### Fixed
- Removed a "premium" visual pass (film grain, cursor-tracked specular wash, and a modal depth-of-field blur) that desaturated content and hurt readability; surfaces are flat and legible again in both themes.
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

[0.3.0]: https://github.com/rutr-labs/runrail/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rutr-labs/runrail/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rutr-labs/runrail/releases/tag/v0.1.0
