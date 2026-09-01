# Changelog

Notable changes to RunRail. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.5.1] — 2026-09-01

A repair release. Almost everything here is a defect in 0.5.0, and two of them
are serious enough to be the reason to upgrade: deleting an environment made
every workflow that used it permanently unrunnable, and editing a task in the
UI silently removed its approval gate.

### Changed
- **Days are now the viewer's calendar days, not UTC.** A run at 9pm in Toronto
  is 1am UTC the next day, so the activity heatmap filed it under tomorrow —
  and every count on that heatmap was drawn on a boundary nobody outside UTC
  recognises. The heatmap, the missed-run marks drawn on it and the run list a
  square opens all bucket by the browser's zone now, exactly and including DST.
  **Existing daily counts will shift on upgrade** if you are not in UTC; they
  are moving to the days you would have said they happened. The API keeps its
  old behaviour when the new `tz` parameter is omitted.

### Fixed
- **Deleting an environment made every workflow that used it unrunnable, and
  deleting a project silently moved where tasks executed.** `workflows.project_id`
  and `workflows.default_environment_id` never had foreign keys: the migration
  that added the columns used a bare `add_column`, so the `ON DELETE SET NULL`
  the model declares existed only in Python, on SQLite and PostgreSQL alike. A
  deleted environment left the workflow pointing at a dead id, and `/run`,
  `/backfill` and `/resume` all answered 404 "Environment not found" while the
  scheduler and worker — which never re-validate — carried on enqueueing and
  executing it. A deleted project was quieter and worse: tasks stopped resolving
  against the project root and started running in whatever directory RunRail
  was launched from, so every relative path read and wrote the wrong tree. This
  release adds the constraints and clears ids that are already dangling, which
  rebuilds the `workflows` table on SQLite.
- **Editing a task in the UI removed its approval gate, without saying so.**
  Every update endpoint wrote pydantic's defaults for fields the client left
  out, so a form that did not know about a field reset it — and the task modal
  did not know about approval. Changing a retry count on a gated task turned a
  task that waits for a person into one that runs unattended. An omitted field
  is now left alone; only an explicit value changes anything.
- **Deleting a task a run was parked on wedged that run forever.** The gate row
  cascaded away with the task while the run stayed `waiting_approval` — a state
  whose only exits were approve and reject, both now impossible — holding its
  concurrency slot and its resource lock. The held slot then made every later
  scheduled fire coalesce into nothing, so the workflow's schedule died
  silently and the watchdog stayed quiet because a parked run reads as
  in-flight. Deleting the task now releases the run first.
- A queued backfill suppressed the workflow's schedule entirely until it
  drained: coalescing counted every queued run, so scheduled fires were dropped
  rather than deferred, with no alert and no record. Backfilling a month of an
  hourly workflow lost every hourly fire in the meantime.
- `/retry` skipped the runnable check that `/run` and `/resume` both apply, so
  retrying a workflow whose tasks or environment had since gone produced a run
  that failed with no task rows — and that phantom failure counted toward
  auto-pause, disabling the schedule off one click.
- Opening the notification panel put the API into an unthrottled request loop,
  measured at roughly nineteen requests a second for as long as it stayed open,
  each one five database scans. The panel stamps itself read with the feed's
  own timestamp, which is different in every response, and that stamp triggered
  a reload.
- A notebook report grew a few pixels at a time, forever, after it had finished
  rendering. The frame asked the document for its height, and a document inside
  a frame reports at least the frame's own height — so past a certain point it
  was measuring the frame, which the parent then grew.
- Sharing a run showed "Building the file" for a fixed eight seconds however
  long the export took, which is normally about seven milliseconds. The
  transfer is tracked properly now, with a real percentage.
- An approval gate drew at the very start of its task's lane on the run
  timeline, because a gate row has no start time. It now spans from parked to
  decided, which fills the gap where the waiting actually happened.
- Switching a task's type left the previous file path behind.
- A resource lock could be silently cleared by an unrelated workflow update,
  and a lock mode could survive without a resource to apply to.
- Skips, cancellations and executor failures in a resumed run were filed under
  the original attempt rather than the segment that produced them.
- A run cancelled while a task was executing could keep that task row `running`
  forever if the worker was killed before it could settle it.
- Log tabs fetched the same log twice per click with no guard against a slow
  response overwriting a newer one, and following a running task's output
  stopped permanently after any burst longer than about six lines.
- The dashboard scrolled sideways between 1101px and roughly 1350px, and the
  metric cards were clipped at the right edge from 1101px up. Both were a `1fr`
  grid track refusing to shrink below its content.
- The activity heatmap keyed its own cells by UTC date while drawing them on
  local-midnight boundaries, and stepped the grid by a fixed 24 hours — which
  drifts at every DST change and, on a 25-hour day, repeats one date and skips
  another.
- Metric counters jumped back to their starting value when they changed twice
  inside one animation, and the notification bell's arrival nudge played once
  per session and then never again.
- `runrail` reported the version recorded when it was installed rather than the
  version it is, so a source checkout stamped a stale number into every
  exported run and into the OpenAPI schema. Exports were saying 0.1.0.
- The screenshots and demo in this README did not render on PyPI, which has no
  repository context for a relative path.

### Added
- Approval gates can be created and edited from the UI. The gate columns, the
  worker parking on them and the approve/reject card all shipped in 0.5.0, but
  no control anywhere in the app could switch one on — every gate that existed
  was made through the YAML file or the API. There is now a "Wait for approval"
  toggle with its prompt on both the create-task and edit-task forms, and a
  test that fails if any other configurable field ever becomes unreachable the
  same way.
- Clicking a day on the activity heatmap opens that day's runs. `GET /api/runs`
  takes a `day` filter, and `/stats/daily`, `/schedule-gaps` and `/runs` all
  take an optional `tz`.
- The timeline shows how long a run waited for approval, rather than leaving a
  gap.
- RunRail has a logo. It appears in the sidebar, on the wallboard, as the
  browser tab icon — there was none before — and on shared run exports.

## [0.5.0] — 2026-08-31

### Added
- **Missed runs are visible history, not just an alert.** A schedule that came due while RunRail was stopped, restarting, or the machine was asleep now shows up on the workflow page and in the activity heatmap alongside successes and failures. Gaps are computed from the cron on read rather than written as placeholder rows, so they can never corrupt success rates or averages and they recalculate themselves when a schedule changes. Fires the scheduler deliberately skipped (a run was already queued) and fires during a pause or snooze are shown as their own states — calling those failures would be a lie, and would paint most of a frequent workflow's history red.
- **A notification centre.** A bell in the topbar with an unread count, opening a panel of recent events — failed, recovered, waiting for approval, auto-paused, SLA breached, schedule missed — each clicking through to the run it concerns. Until now the app only had transient toasts: miss one and it was gone. The feed is derived from existing data (no event table to grow or prune) and unread state lives in the browser, since there is exactly one user.
- **Resource locks between workflows.** A workflow can name a resource — a database, a licence, a mounted share — and declare whether it needs it *alone* or can *share* it. A heavy monthly validation marked exclusive runs by itself while everything else on that resource queues and waits; shared workflows overlap each other freely. A queued exclusive run also bars new shared runs from starting, or a steady drip of hourly jobs would mean the monthly job never got its turn. Enforced in the same atomic claim that already enforces per-workflow concurrency, so two workers racing cannot both acquire.
- Resource locks are released automatically after a crash: a run left mid-flight by a killed server or a machine restart is recovered as failed on the next start, which frees both its concurrency slot and its lock. A run parked on an approval gate deliberately survives a restart instead — nobody has decided yet — and keeps its lock until it is approved, rejected or cancelled. Both behaviours are now covered by tests, so the interaction cannot regress silently.
- A queued run now explains **why** it is waiting — at its workflow's concurrency limit, behind a resource lock, or behind the exclusive barrier — naming and linking the run that is blocking it.

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

- Frontend tests (Vitest): 143 covering the timezone-aware cron engine, the schedule builder's round-trip, formatting helpers, and the lock/gap/trend logic. The cron tests assert against fire times generated by the backend's own scheduler, so a divergence between what the UI promises and what actually runs fails the build.
- Backend cross-feature tests: resume, approval gates, resource locks, crash recovery, concurrency and the watchdogs are now tested in combination rather than only in isolation — which is how five of the defects under Fixed were found.
- `scripts/seed_demo.py` fills a database with a realistic large history for demos and performance work. Failures arrive as day-clustered incidents rather than a uniform sprinkle, and the live tail starts at believable progress, so the heatmap and wallboard read like a real install.

### Changed
- The README opens with the live demo and a sixty-second quick start, both re-captured from the seeded demo home at retina resolution. The wallboard GIF films real runs: two workflows complete on camera, one crosses its median and turns amber, and an approval gate sits parked.
- README rewritten against what the code actually does. It had drifted badly: the roadmap still promised authentication and API tokens, which have since been deliberately ruled out; "that's the whole setup" skipped the interactive import prompt the first `runrail serve` actually shows; the notebook extra was described backwards (it renders reports, it does not run notebooks); and none of the last fourteen features were mentioned. Adds screenshots, three required config variables that were documented nowhere, and honest notes on what the tool does not do.
- `__version__` now reads from package metadata instead of being hand-maintained. Three copies had drifted to 0.1.0, 0.3.1 and 0.4.0, and the stalest of them was stamped into every exported run file.

- Five indexes added after measuring against a seeded database of ~35k runs: the dashboard summary went 32ms → 3ms, a filtered run list 14ms → 3ms, deleting a task 58ms → 3ms, and retention cleanup of 500 runs 98ms → effectively free. Two of them replace narrower single-column indexes, so writes pay for one extra entry, not three.
- CI now runs the suite against PostgreSQL as well as SQLite, and runs the new frontend test suite.

- Notes and approval decisions no longer ask who you are. RunRail is a single-user tool on your own machine, so a name field — and the disclaimer explaining that the name is unverified — added ceremony without adding meaning. The content stays: a note still has a body, and an approval still records why it was made and when.

### Fixed
- A fresh PostgreSQL database failed during its very first migration: a legacy-environment cleanup compared the native `environmenttype` enum column against a varchar bind, an operator PostgreSQL refuses at parse time. SQLite spells enums as text and never noticed; the comparison now goes through a text cast on both backends. Caught by the new PostgreSQL CI job on its first ever run.
- **Weekly schedules fired one day late.** APScheduler numbers days of the week `0=Mon`, while standard cron — and RunRail's own UI — numbers them `0=Sun`, and nothing translated between the two. Every weekly workflow ran a day after the day it was configured for: picking "Sunday" in the schedule builder ran it on Monday. A day-of-week field of `7` (valid crontab for Sunday) made the scheduler reject the expression outright, so those workflows never ran at all and said nothing. All three places that build a trigger now go through one helper that translates the field, and the frontend's occurrence engine is pinned against fire times generated by the real scheduler so the two can never drift apart again. **On upgrade, existing weekly workflows move to the day they always claimed** — if you built something downstream around the day it actually ran, check it.
- An invalid cron was accepted, previewed confidently, and then never ran: `60 9 * * *` was labelled "daily at 10:00" while the scheduler silently skipped it forever. Expressions are now validated where they are saved — through the API and through `runrail apply` — and rejected with a message naming the offending field.
- A run parked on an approval gate past its deadline raised no SLA alert, while the run queued behind it — which had never started — was reported instead. The run blocked on a human is now the one named, and only the oldest in-flight run of a workflow can breach, so the two cannot both alert.
- Approving a gate briefly released the workflow's resource lock, in the window between the click and the worker picking the run back up; a queued exclusive run could take the resource while the approved run's half-finished state waited. Occupancy is now derived from whether a run has started rather than from its status, so a run mid-flight holds its lock continuously.
- A gate left open by a crash or a cancellation could wedge a resumed run permanently — it held its concurrency slot and its resource lock with no way out but approving a phantom gate from a dead attempt. Gate rows are now settled whenever a run reaches a terminal state, by every path that can end one, and the open-gate count is scoped to the current attempt.
- Shutting down `runrail serve` while a run was in flight crashed the summary on PostgreSQL (naive and aware timestamps subtracted), and PostgreSQL connections now pin their session to UTC so day-bucketed stats and the activity heatmap cannot shift with the server's timezone. The README's PostgreSQL connection URL was also wrong: RunRail ships psycopg 3, so the URL needs the `+psycopg` driver.

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
