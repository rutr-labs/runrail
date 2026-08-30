# Changelog

Notable changes to RunRail. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Resume from the failed task.** A failed run picks up in place — same run id, same `ds`, same artifacts folder — re-executing only what did not succeed plus everything downstream. A dialog shows the plan first: which tasks are reused, how much time that saves, and why each remaining task will run again.
- **Manual approval gates.** Any task can require a human before it runs. The run parks, releases its worker thread (so a gate left open overnight cannot starve the pool) and waits; the run page shows the author's prompt, the exact command about to run, and what already succeeded, with approve and reject. A waiting run is surfaced on the dashboard, and cancelling one is allowed — approve and reject are otherwise its only exits.
- **Snooze a workflow** until a chosen time, optionally pausing its scheduled runs too. It expires by the clock, so there is nothing to re-enable and nothing to forget.
- **Missed-run alerts (a dead man's switch)** — opt-in per workflow, alerting when a schedule goes silent because the scheduler stopped, the host slept, or someone paused it and forgot. Expected fire times come from the scheduler's own trigger, so the watchdog can never disagree with it about a DST boundary.
- **"Must finish by" SLAs** — alert while a run is still going once it passes its deadline, measured from creation so queue wait counts.
- **Notebook reports.** Executed notebooks render as HTML inline on the run page — charts, tables and markdown — cached on first view, with the original `.ipynb` a click away. Requires the `notebook` extra; without it the UI says so instead of failing.
- **A stable `/latest` report URL** per workflow, resolving to the newest successful run that produced a report, and saying how stale it is rather than quietly showing month-old numbers.
- **Share a run** as one self-contained HTML file — status, timeline, logs and the embedded report — that opens from an email with no RunRail and no login.
- **Log search across runs**, answering "when did this error first appear?" without opening runs one at a time. Every scan is bounded, and the result says which bound stopped it: a partial window is never presented as a full history.
- **Run notes** — annotate a run ("bad upstream file, ignore") so the reason a failure was dismissed outlives the person who dismissed it. Annotated runs are flagged in run tables.
- **Per-task duration trends** with a sparkline and a "slower than usual" flag, computed from a median and a robust spread so one pathological run cannot poison the baseline, and never flagged without enough history.

### Fixed
- Progress-bar comets animate correctly in Chromium-based browsers (Edge, Chrome), where they could appear to shift without animating. Two independent causes, both fixed:
  - **Blanked frames.** Resizing a canvas clears it, and per the HTML spec ResizeObserver callbacks run *after* `requestAnimationFrame` but *before* paint — so on a bar whose width animates every frame (wallboard fills, growing Gantt bars) the observer wiped the frame that had just been drawn, before it was ever shown. Measured at 86% blank frames in Chromium; every resize now repaints immediately, measured at 0%.
  - **Reduced motion drew moving particles.** With the OS motion preference on — which on Windows is the general Settings → Accessibility → Visual effects → "Animation effects" toggle, not a motion-specific one — the engine parks on a static frame. But embers are stored as a fraction of the bar's width, so re-laying that frozen field on a widening bar slid and stretched the whole cloud: maximum apparent movement in the mode that asks for none. The static frame is now anchored to the frontier in absolute pixels, and the engine reacts to the preference being toggled instead of requiring a reload.
- Notebook kernels no longer warn "running over TCP without encryption": the worker launches papermill through a small shim that puts the Jupyter kernel on IPC transport (Unix domain sockets, protected by file permissions) instead of loopback TCP. Besides silencing ipykernel's warning, this closes the kernel's TCP ports entirely on shared machines. Windows keeps TCP-on-loopback (ZeroMQ has no ipc:// there).

## [0.4.0] — 2026-08-04

### Changed
- The built frontend bundle (`src/runrail/web/static/`) is no longer committed to git — the repo is source-only. Release builds are unaffected: CI rebuilds the UI before packaging and the wheel still ships it via hatch's `artifacts` config. Running from a raw clone now requires `cd frontend && npm run build` once (the API responds with exactly that hint if the bundle is missing).
- Typography is now bundled and identical on every OS: Inter Variable for the UI and JetBrains Mono Variable for code surfaces (run ids, commands, the log viewer) ship with the app as subsetted woff2. Previously Windows fell back to Segoe UI — and to Courier New in several mono spots — which read noticeably cheaper than the macOS rendering.
- Windows/Edge polish: native `<select>` controls are de-natived (custom chevron, no system chrome), the modal close button uses a real icon instead of a text glyph, text selection is theme-tinted instead of OS blue, and the shared inner-highlight shadow no longer uses a half-pixel inset (which rendered patchy at 100% DPI).

### Added
- Timezone-aware schedules: each workflow's cron now evaluates in its own IANA timezone (`schedule_timezone`; unset keeps the old UTC behavior, so nothing changes on upgrade). DST follows APScheduler semantics — a time skipped by spring-forward doesn't fire, a repeated fall-back time fires once. The timezone round-trips through `runrail export`/`apply`, and the API rejects unknown zone names.
- Schedule builder: workflow modals replace the raw cron field with dropdowns — every few minutes, hourly, daily, weekly (day chips), or monthly — plus a timezone picker defaulting to your browser's zone and a live preview of the next two runs. Raw cron remains available as the "Advanced" mode, and existing expressions parse back into the dropdowns when they can. Schedule labels across the app (workflow cards, upcoming lists, wallboard) now render in the workflow's zone, tagged with the offset (e.g. "daily at 09:00 GMT+4") when it differs from yours.
- Motion pass in the comet's design language, all compositor-friendly and reduced-motion-aware: a sliding sidebar active indicator, boot choreography (logo rails light up, nav rises in), staggered entrances for tables, panels, list rows, and command-palette items, a topological left-to-right reveal for dependency graphs, Gantt bars that sweep open, an activity-heatmap column wave, status chips that "land" on state change, modal exit choreography (dismissals animate out instead of blinking), workflow-card-to-detail title morphs via view transitions, arrival glows on newly live runs, and a one-shot specular sweep on primary buttons.

### Fixed
- Microsoft Teams notifications work with Power Automate workflow webhooks — the current Microsoft-recommended path after the Office 365 connector retirement. RunRail now detects Teams-shaped URLs (`*.logic.azure.com`, `*.powerplatform.com`, legacy `*.webhook.office.com`) and posts the `type: message` / Adaptive Card `attachments` envelope the standard "when a webhook request is received" flow template requires; the previous flat `text` payload rendered nothing through that template. Slack and custom receivers keep the existing flat payload.
- Long task and workflow names no longer truncate or overflow across the UI — 23 surfaces audited and fixed, including Gantt lane labels (previously cut at ~13 characters; the name column now sizes to the longest name up to 240px via grid subgrid), dependency-graph nodes (previously hard-cut at 18 characters with no tooltip; nodes now size to their names with glyph-aware measurement and carry native SVG tooltips), dashboard sparkline rows, wallboard tiles and tickers, run tables, task cards, summary strips, modal form selects, and the command palette. Everywhere a name can still ellipsize at extreme lengths, a hover tooltip carries the full name.
- The status chip in run/workflow summary strips rendered with dim gray uppercase text: the strip's label selector (`.summary-strip > div > span`) also matched the `StatusBadge` span and clobbered its ink. Now scoped to `:first-child`.
- Contrast pass on muted text: `--text-3` was ~2.8:1 in dark (`#475569`) and ~2.2:1 in light (`#94a3b8`) — both now `#64748b`, lifting every label, table header, timestamp, and metadata line past ~4:1. Dark-theme card borders brightened (0.07→0.095, strong 0.11→0.15), summary-strip labels bumped to weight 600, and the log viewer's placeholder/loading text raised from 25–30% to 45% alpha.
- Running progress bars redesigned again, this time as a real particle comet: a shared canvas engine (`frontend/src/comet.ts`) streams fine embers into a bright pulsing core at the progress frontier, with sparks shedding off the head. One rAF loop drives every bar (wallboard fills, running Gantt bars, and the indeterminate `LoadingBar` for env builds and queued runs); glow sprites are pre-rendered, canvases are DPR-aware (capped at 2×), and the loop parks when the tab is hidden or no bars are mounted. The `--over-ratio` overrun contract is unchanged — embers amberize ahead of the head as a run passes its median duration — and reduced-motion renders a single static frame.
- Finished Gantt bars joined the same design language as a comet at rest: instead of flat green/red fills, a one-time static render draws an evenly spaced dot rail ending in a solid cooled endcap orb, tinted by outcome (success green, failure red). Stillness is deliberate — uniform spacing and a flat tint read as "landed", in contrast to the live comet's chaotic embers. Finished bars cost nothing per frame; they repaint only on resize or theme change.

## [0.3.1] — 2026-07-15

### Packaging
- Available on PyPI — `pipx install runrail`. The wheel bundles the prebuilt web UI, so no Node is needed at runtime, and `pyproject.toml` now carries full metadata (description, Rutr Labs authors, keywords, trove classifiers, and project URLs).
- Releases publish to PyPI via Trusted Publishing (OIDC) from CI when a release is published; a manual dispatch can dry-run to TestPyPI.
- Data now lives in a stable per-user application-data directory by default — `~/Library/Application Support/RunRail` (macOS), `~/.local/share/RunRail` (Linux, honouring `$XDG_DATA_HOME`), `%LOCALAPPDATA%\RunRail` (Windows) — instead of `./.runrail` in the current directory. Override with `RUNRAIL_HOME` (e.g. `RUNRAIL_HOME=./.runrail`). `runrail serve` creates the directory and database on first run, so a fresh install needs only `pipx install runrail && runrail serve`.

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

[0.3.1]: https://github.com/rutr-labs/runrail/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/rutr-labs/runrail/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rutr-labs/runrail/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rutr-labs/runrail/releases/tag/v0.1.0
