# Changelog

Notable changes to RunRail. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

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

[0.2.0]: https://github.com/rutr-labs/runrail/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rutr-labs/runrail/releases/tag/v0.1.0
