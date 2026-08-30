import { useCallback, useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import {
  AlertTriangle, CalendarCheck2, CalendarOff, CalendarX2, ChevronDown, Layers,
  PauseCircle, RefreshCw, ScanSearch,
} from 'lucide-react';
import { api } from '../api';
import { Button } from './ui';
import { cronLabel, viewerZone, zoneTag } from '../cron';

/* ─── Missed scheduled runs ────────────────────────────────
   A run that never happened has no row — schedule_gaps.py refuses to write a
   placeholder because it would corrupt success rates, 24h counts and the
   heatmap — so the whole of this history is derived on read from the
   workflow's own crontab. GET /workflows/{id}/schedule-gaps answers with what
   became of every fire the schedule owed.

   FOUR states, and the distinction is the entire feature:

     ran      a scheduled run landed on that fire            success green
     missed   the fire came and went with no run at all      warning amber
     blocked  the scheduler dropped it because a run was     queued slate
              already sitting in the queue — coalescing by
              design, nothing was lost
     paused   the workflow was disabled or snooze-paused     queued slate,
              at that instant, so nothing was owed           hollow

   Painting blocked or paused as failures would be a lie, and on a five-minute
   cron it would paint most of a week red. Only `missed` is allowed to alarm.

   The response is also honest about its own limits: a schedule dense enough to
   overflow the fire cap reports `complete: false` and a `stopped_by`, and this
   component must repeat that rather than present a partial scan as a clean
   bill of health. */

/* ─── Backend shapes ───────────────────────────────────────
   Mirrors schedule_gaps.find_gaps() exactly; every datetime arrives as an
   ISO-8601 string with an offset, and every `date` is a UTC day key — the same
   bucketing /stats/daily uses, which is what lets `daily` drop straight into
   the activity heatmap beside the runs. */

export type GapState = 'ran' | 'missed' | 'blocked' | 'paused';

/** One UTC day of the scan. `expected` is the sum of the other four. */
export interface GapDay {
  date: string;
  expected: number;
  ran: number;
  missed: number;
  blocked: number;
  paused: number;
}

export interface MissedFire {
  /** The instant the crontab owed a run. */
  expected_at: string;
  /** UTC day key, for the heatmap column this fire belongs to. */
  date: string;
}

export interface PausedSpan {
  reason: 'disabled' | 'snoozed' | string;
  since: string;
  until: string;
}

/** Why the scan stopped short. Ordered by significance server-side, so at most
 *  one is ever set: a caller cannot act on the row valve while the fire cap is
 *  still biting. */
export type GapStopReason = 'invalid_cron' | 'max_fires' | 'run_rows';

export interface ScheduleGapsData {
  workflow_id: number;
  schedule_cron: string | null;
  schedule_timezone: string;
  window: {
    /** What was ACTUALLY examined — later than `requested_since` when the
     *  workflow is younger than the window, or when the cap moved the floor. */
    since: string;
    /** Stops one match-tolerance short of now: a fire younger than that is not
     *  history yet, because its run is still allowed to arrive. */
    until: string;
    requested_since: string;
  };
  totals: Record<GapState | 'expected', number>;
  daily: GapDay[];
  /** Newest first, capped at the request's `limit`. */
  missed: MissedFire[];
  missed_shown: number;
  paused_spans: PausedSpan[];
  complete: boolean;
  stopped_by: GapStopReason | null;
}

/* ─── Fetching ─────────────────────────────────────────────
   Deliberately a hook rather than a fetch buried in the panel: the same
   payload feeds the panel AND the activity heatmap, and the workflow page
   should pay for one scan, not two. */

/** A month reads as "recent history" without asking the server to walk a
 *  quarter of fires. Dense schedules truncate at any window — that is what
 *  `complete` is for. */
export const DEFAULT_GAP_DAYS = 30;

export interface ScheduleGapsState {
  data: ScheduleGapsData | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useScheduleGaps(
  workflowId: number | null | undefined,
  days: number = DEFAULT_GAP_DAYS,
): ScheduleGapsState {
  const [data, setData] = useState<ScheduleGapsData | null>(null);
  const [loading, setLoading] = useState(Boolean(workflowId));
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce(n => n + 1), []);

  useEffect(() => {
    if (!workflowId) { setData(null); setLoading(false); setError(null); return; }
    let stale = false; // widening the window mid-flight must not resurrect the old scan
    setLoading(true);
    const window = Math.min(365, Math.max(1, Math.round(days)));
    api<ScheduleGapsData>(`/workflows/${workflowId}/schedule-gaps?days=${window}`)
      .then(payload => { if (!stale) { setData(payload); setError(null); } })
      .catch(err => { if (!stale) setError(err?.message || 'Could not read the schedule history'); })
      .finally(() => { if (!stale) setLoading(false); });
    return () => { stale = true; };
  }, [workflowId, days, nonce]);

  return { data, loading, error, reload };
}

/* ─── Local formatting ─────────────────────────────────────
   Same behaviour as App.tsx's helpers, which are module-private there. */

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

function formatDate(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
    hour: '2-digit', minute: '2-digit',
  });
}

function timeAgo(value?: string | null): string {
  if (!value) return '—';
  const ms = Date.now() - new Date(value).getTime();
  if (ms < MINUTE) return 'just now';
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)}m ago`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h ago`;
  return `${Math.floor(ms / DAY)}d ago`;
}

/** Wall-clock only — the day is already carried by the row above the chips. */
function clockTime(value: string): string {
  return new Date(value).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function dayLabel(value: string): string {
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

const sameLocalDay = (a: string, b: string) =>
  new Date(a).toDateString() === new Date(b).toDateString();

/** A span for a sentence rather than a table cell: coarse, never "0 days". */
function humanSpan(ms: number): string {
  if (ms < HOUR) return `${Math.max(1, Math.round(ms / MINUTE))} minutes`;
  if (ms < 2 * DAY) {
    const hours = Math.round(ms / HOUR);
    return hours === 1 ? 'an hour' : `${hours} hours`;
  }
  const days = Math.round(ms / DAY);
  return days === 1 ? 'a day' : `${days} days`;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/* ─── Streaks ──────────────────────────────────────────────
   A minute-cron that was down over lunch produces 120 missed fires. Listing
   them as 120 identical rows buries the one fact that matters — the server was
   off from 12:04 to 14:04 — so adjacent fires collapse into one outage row.

   The cadence is estimated from the window rather than assumed, because the
   response never carries the fire list itself. `expected` fires spread over
   the scanned span gives the mean interval; the slack absorbs an uneven
   crontab (weekdays-only spreads the mean well past its real 24h step). Every
   collapsed timestamp stays one click away, so the grouping only ever changes
   how the same facts are presented. */

const CADENCE_SLACK = 1.75;

export interface MissedStreak {
  key: string;
  /** Oldest fire in the streak. */
  from: string;
  /** Newest fire in the streak. */
  to: string;
  /** Newest first, like the response. */
  fires: string[];
}

export function groupMissed(data: ScheduleGapsData): MissedStreak[] {
  const fires = data.missed.map(m => m.expected_at);
  if (fires.length === 0) return [];

  const span = new Date(data.window.until).getTime() - new Date(data.window.since).getTime();
  const cadence = data.totals.expected > 1 && span > 0 ? span / data.totals.expected : 0;
  // No usable cadence (a single expected fire, a degenerate window) means no
  // grounds for grouping — every fire stands alone rather than guessing.
  const threshold = cadence > 0 ? cadence * CADENCE_SLACK : 0;

  const streaks: MissedStreak[] = [];
  let current: string[] = [];
  const flush = () => {
    if (!current.length) return;
    streaks.push({ key: current[0], to: current[0], from: current[current.length - 1], fires: current });
    current = [];
  };
  for (const fire of fires) { // newest first
    const previous = current[current.length - 1];
    const adjacent = previous !== undefined && threshold > 0
      && new Date(previous).getTime() - new Date(fire).getTime() <= threshold;
    if (previous !== undefined && !adjacent) flush();
    current.push(fire);
  }
  flush();
  return streaks;
}

/* ─── The panel ────────────────────────────────────────────
   Self-contained card, like the approval gate: it carries its own tone, so the
   host drops it in without wrapping it in a panel of the right colour. */

/** How many outage rows show before the list asks to be expanded. */
const VISIBLE_STREAKS = 6;

type Tone = 'calm' | 'missed' | 'muted' | 'broken';

const REASON_LABEL: Record<string, string> = {
  disabled: 'This workflow was switched off',
  snoozed: 'A snooze was set to pause runs, not just alerts',
};

export function ScheduleGapsPanel({ workflowId, days = DEFAULT_GAP_DAYS, state, className }: {
  workflowId: number;
  /** Scan window in days; ignored when `state` is supplied. */
  days?: number;
  /** Pass the result of useScheduleGaps when the host already owns the payload
   *  (it usually does, to feed the heatmap too). Omit and the panel fetches. */
  state?: ScheduleGapsState;
  className?: string;
}) {
  // Hooks cannot be conditional, so the owned fetch is disarmed with a null id
  // rather than skipped.
  const own = useScheduleGaps(state ? null : workflowId, days);
  const { data, loading, error, reload } = state ?? own;
  const [expanded, setExpanded] = useState(false);

  const streaks = useMemo(() => (data ? groupMissed(data) : []), [data]);

  if (loading && !data) {
    return (
      <section className={clsx('schedule-gaps', 'schedule-gaps--loading', className)} aria-busy="true">
        <div className="schedule-gaps-skel schedule-gaps-skel--title" />
        <div className="schedule-gaps-skel" />
      </section>
    );
  }

  if (error && !data) {
    return (
      <section className={clsx('schedule-gaps', 'schedule-gaps--muted', className)}>
        <div className="schedule-gaps-head">
          <span className="schedule-gaps-glyph"><ScanSearch size={18} /></span>
          <div className="schedule-gaps-headtext">
            <span className="schedule-gaps-eyebrow">Schedule history</span>
            <h2>Couldn't check for missed runs</h2>
            <p>{error}</p>
          </div>
          <Button variant="ghost" size="sm" onClick={reload}><RefreshCw size={13} /> Retry</Button>
        </div>
      </section>
    );
  }

  if (!data) return null;

  const { totals, window: scan, stopped_by: stoppedBy, schedule_cron: cron } = data;
  const scanned = new Date(scan.until).getTime() - new Date(scan.since).getTime();
  const scannedLabel = humanSpan(Math.max(0, scanned));
  // The floor moves for two very different reasons and only one is a caveat:
  // a young workflow was simply never owed anything earlier.
  const youngWorkflow = data.complete
    && new Date(scan.since).getTime() - new Date(scan.requested_since).getTime() > 5 * MINUTE;
  const zone = data.schedule_timezone;
  let foreignZone = false;
  try { foreignZone = Boolean(zone) && zone !== viewerZone(); } catch { foreignZone = false; }

  /* Four openings, one card. The headline is the single sentence a glance has
     to land on, so it never says "all clear" for a scan that stopped early. */
  let tone: Tone = 'calm';
  let Glyph = CalendarCheck2;
  let headline = 'Every scheduled run happened';
  let subline = '';

  if (!cron) {
    tone = 'muted';
    Glyph = CalendarOff;
    headline = 'Runs on demand only';
    subline = 'This workflow has no schedule, so no run is ever owed and none can be missed. '
      + 'Give it a cron and RunRail starts keeping this history.';
  } else if (stoppedBy === 'invalid_cron') {
    tone = 'broken';
    Glyph = AlertTriangle;
    headline = "This schedule can't be read";
    subline = `RunRail could not parse "${cron}", so the scheduler skipped this workflow entirely. `
      + 'Nothing here is a missed run — nothing was ever scheduled. Fix the expression and the '
      + 'schedule starts firing.';
  } else if (totals.expected === 0) {
    tone = 'calm';
    headline = 'Nothing was due yet';
    subline = `${cronLabel(cron, zone)} owed no runs in the last ${scannedLabel}, `
      + 'so there is nothing to have missed.';
  } else if (totals.missed > 0) {
    tone = 'missed';
    Glyph = CalendarX2;
    headline = `${plural(totals.missed, 'scheduled run')} never happened`;
    subline = `Over the last ${scannedLabel} this workflow owed ${plural(totals.expected, 'run')} and `
      + `started ${totals.ran}. The rest are below — the schedule came due and RunRail created no run at all.`;
  } else {
    tone = 'calm';
    headline = data.complete ? 'Every scheduled run happened' : 'No misses in the part we could scan';
    subline = `${plural(totals.ran, 'run')} of the ${totals.expected} this schedule owed in the last `
      + `${scannedLabel} started on time.`;
  }

  const shown = expanded ? streaks : streaks.slice(0, VISIBLE_STREAKS);
  const hiddenFires = streaks.slice(VISIBLE_STREAKS).reduce((n, s) => n + s.fires.length, 0);

  return (
    <section className={clsx('schedule-gaps', `schedule-gaps--${tone}`, className)}>
      <div className="schedule-gaps-head">
        <span className="schedule-gaps-glyph"><Glyph size={18} /></span>
        <div className="schedule-gaps-headtext">
          <span className="schedule-gaps-eyebrow">Missed runs</span>
          <h2>{headline}</h2>
          <p>{subline}</p>
        </div>
        <Button variant="ghost" size="sm" onClick={reload} title="Re-scan the schedule against the runs that exist">
          <RefreshCw size={13} className={loading ? 'icon-spin' : undefined} />
        </Button>
      </div>

      {totals.expected > 0 && (
        <div className="schedule-gaps-stats">
          <div className="gap-stat" title="Fires this workflow's crontab owed inside the scanned window">
            <span>Expected</span><strong>{totals.expected}</strong>
          </div>
          <div className="gap-stat gap-stat--ran" title="Fires a scheduled run actually landed on">
            <span>Ran</span><strong>{totals.ran}</strong>
          </div>
          <div className="gap-stat gap-stat--missed"
               title="Fires that came due and produced no run at all — the only faulty state here">
            <span>Missed</span><strong>{totals.missed}</strong>
          </div>
          {totals.blocked > 0 && (
            <div className="gap-stat gap-stat--blocked"
                 title="Dropped on purpose because a run was already queued — coalescing, not a fault">
              <span>Skipped</span><strong>{totals.blocked}</strong>
            </div>
          )}
          {totals.paused > 0 && (
            <div className="gap-stat gap-stat--paused"
                 title="The workflow was disabled or snooze-paused, so no run was owed">
              <span>Not owed</span><strong>{totals.paused}</strong>
            </div>
          )}
        </div>
      )}

      {/* ── Honesty rail: everything that qualifies the numbers above ── */}
      {stoppedBy === 'max_fires' && (
        <p className="schedule-gaps-note schedule-gaps-note--warn">
          <ScanSearch size={13} />
          <span>
            This schedule fires too often to walk the whole window in one scan. Only{' '}
            <strong>{formatDate(scan.since)}</strong> onward was examined — earlier days are
            unchecked, not clean. Ask for fewer days to see a shorter window in full.
          </span>
        </p>
      )}
      {stoppedBy === 'run_rows' && (
        <p className="schedule-gaps-note schedule-gaps-note--warn">
          <Layers size={13} />
          <span>
            This workflow has more runs in the window than one scan reads, so some fires could not be
            matched to the run that answered them. Treat the missed count as an upper bound.
          </span>
        </p>
      )}
      {totals.blocked > 0 && (
        <p className="schedule-gaps-note">
          <Layers size={13} />
          <span>
            {plural(totals.blocked, 'fire')} {totals.blocked === 1 ? 'was' : 'were'} skipped
            deliberately because a run was already sitting in the queue. RunRail coalesces those so a
            backlog can never pile up — none of them is a fault.
          </span>
        </p>
      )}
      {data.paused_spans.map(span => {
        const open = Math.abs(new Date(span.until).getTime() - new Date(scan.until).getTime()) < 2 * MINUTE;
        return (
          <p className="schedule-gaps-note schedule-gaps-note--muted" key={`${span.reason}:${span.since}`}>
            <PauseCircle size={13} />
            <span>
              {REASON_LABEL[span.reason] ?? `Paused (${span.reason})`} from{' '}
              <strong>{formatDate(span.since)}</strong>{open ? ' until now' : ` to ${formatDate(span.until)}`}.
              Nothing was owed in that stretch, so those fires are not misses.
            </span>
          </p>
        );
      })}
      {youngWorkflow && (
        <p className="schedule-gaps-note schedule-gaps-note--muted">
          <CalendarCheck2 size={13} />
          <span>Scanned from {formatDate(scan.since)}, when this workflow was created — it owed nothing before that.</span>
        </p>
      )}

      {streaks.length > 0 && (
        <div className="schedule-gaps-list">
          <p className="schedule-gaps-explainer">
            Each of these is a moment the schedule came due and no run was ever created — most often
            RunRail itself was stopped, restarting, or the machine was asleep. Nothing runs
            retroactively; these hours simply have no run.
          </p>
          {shown.map((streak, index) => (
            <MissedStreakRow key={streak.key} streak={streak} index={index} />
          ))}
          {streaks.length > VISIBLE_STREAKS && (
            <button type="button" className="schedule-gaps-more" onClick={() => setExpanded(v => !v)}>
              <ChevronDown size={12} className={clsx('gap-chev', expanded && 'gap-chev--open')} />
              {expanded
                ? 'Show fewer'
                : `Show ${streaks.length - VISIBLE_STREAKS} older ${streaks.length - VISIBLE_STREAKS === 1 ? 'gap' : 'gaps'} (${hiddenFires} fires)`}
            </button>
          )}
          {totals.missed > data.missed_shown && (
            <p className="schedule-gaps-note schedule-gaps-note--muted">
              <ScanSearch size={13} />
              <span>
                Listing the {data.missed_shown} most recent of {totals.missed} missed fires in this window.
              </span>
            </p>
          )}
        </div>
      )}

      {foreignZone && totals.expected > 0 && (
        <p className="schedule-gaps-foot">
          Times shown in your local zone. This schedule runs on {zone} ({zoneTag(zone)}).
        </p>
      )}
    </section>
  );
}

/* ─── One outage ───────────────────────────────────────────
   A single missed fire and a two-hour hole are the same fact at different
   scales, so they share a row and differ only in what the row says. */

function MissedStreakRow({ streak, index }: { streak: MissedStreak; index: number }) {
  const [open, setOpen] = useState(false);
  const count = streak.fires.length;
  const single = count === 1;
  const spanMs = new Date(streak.to).getTime() - new Date(streak.from).getTime();

  const when = single
    ? formatDate(streak.to)
    : sameLocalDay(streak.from, streak.to)
      ? `${dayLabel(streak.from)} · ${clockTime(streak.from)} → ${clockTime(streak.to)}`
      : `${formatDate(streak.from)} → ${formatDate(streak.to)}`;

  const why = single
    ? 'The schedule came due at this instant and no run was created.'
    : `${plural(count, 'fire')} in a row went unanswered across ${humanSpan(spanMs)} — RunRail was not `
      + 'taking scheduled work for that whole stretch.';

  return (
    <div className={clsx('gap-row', !single && 'gap-row--streak')}
         style={{ animationDelay: `${Math.min(index, 8) * 28}ms` }}>
      <span className="gap-row-glyph" aria-hidden="true"><CalendarX2 size={13} /></span>
      <div className="gap-row-body">
        <div className="gap-row-top">
          <strong className="gap-row-when">{when}</strong>
          {!single && <span className="gap-row-count">{count} fires</span>}
          <span className="gap-row-ago">{timeAgo(streak.to)}</span>
        </div>
        <p className="gap-row-why">{why}</p>
        {!single && (
          <>
            <button type="button" className="gap-row-toggle" aria-expanded={open}
                    onClick={() => setOpen(v => !v)}>
              <ChevronDown size={12} className={clsx('gap-chev', open && 'gap-chev--open')} />
              {open ? 'Hide timestamps' : `Show all ${count} timestamps`}
            </button>
            {open && (
              <div className="gap-row-times">
                {streak.fires.map(fire => (
                  <span key={fire} className="gap-time-chip" title={formatDate(fire)}>
                    {sameLocalDay(streak.from, streak.to)
                      ? clockTime(fire)
                      : `${dayLabel(fire)} ${clockTime(fire)}`}
                  </span>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ─── Heatmap bridge ───────────────────────────────────────
   The heatmap keys cells by the UTC day string it derives locally; `daily`
   already uses that key, so the only thing it needs on top is where the scan
   actually starts. A day older than the floor gets no gap treatment at all —
   an unmarked cell must never be able to mean "checked and clean" when it was
   never checked. */

export interface HeatmapGapFeed {
  gaps: GapDay[];
  gapsSince: string | null;
  gapsComplete: boolean;
}

export function heatmapGapFeed(data: ScheduleGapsData | null): HeatmapGapFeed | null {
  if (!data || !data.schedule_cron || data.stopped_by === 'invalid_cron') return null;
  return { gaps: data.daily, gapsSince: data.window.since, gapsComplete: data.complete };
}
