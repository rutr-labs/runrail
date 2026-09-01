import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import clsx from 'clsx';
import { Link } from 'react-router-dom';
import { api } from '../api';
import { localDateKey, viewerZone } from '../format';
import type { GapDay } from './ScheduleGaps';

/** Activity grid: one cell per day, weeks as columns running Monday → Sunday.
 *  Green intensity tracks run volume; any failures tint the day amber/red.
 *  Optional 4w/8w/16w/6m range selector; the choice persists in localStorage.
 *
 *  With `gaps` supplied (per-workflow only — a gap belongs to one crontab) the
 *  grid also carries the runs that NEVER HAPPENED. Those are a different fact
 *  from a quiet day and from a failing day, so they are painted as a marking
 *  ON TOP of the run fill rather than as another fill colour:
 *
 *    missed  amber hatch    the schedule came due and no run exists
 *    paused  slate ring     disabled or snooze-paused, nothing was owed
 *
 *  `blocked` fires get no mark at all: the scheduler dropped them because a run
 *  was already queued, which means the day already has that run's colour and a
 *  second glyph would imply a fault where there is none. It stays in the tip.
 *
 *  Cells older than `gapsSince` are left unmarked AND say so in their tooltip —
 *  the scan never reached them, and an unmarked cell must not be readable as
 *  "checked and clean". */

type DayStat = { date: string; success: number; failed: number; other: number };

const CELL = 12;
const GAP = 3;
const DAY_MS = 86_400_000;

/** Selectable history ranges. 6m = 26 weeks = 182 days (API caps days at 366). */
const RANGES = [
  { label: '4w', weeks: 4 },
  { label: '8w', weeks: 8 },
  { label: '16w', weeks: 16 },
  { label: '6m', weeks: 26 },
] as const;

const WEEKS_STORAGE_KEY = 'runrail.heatmap.weeks';

function storedWeeks(fallback: number): number {
  try {
    const parsed = Number(localStorage.getItem(WEEKS_STORAGE_KEY));
    return RANGES.some(r => r.weeks === parsed) ? parsed : fallback;
  } catch {
    return fallback; // localStorage unavailable (private mode, etc.)
  }
}

/** `X at 40%` rather than `background: X; opacity: .4`. Identical compositing
 *  for a solid box, but element opacity would also fade the ::after gap marks,
 *  and a hatched miss has to stay legible on a pale cell. */
const fade = (color: string, alpha: number) =>
  `color-mix(in srgb, ${color} ${Math.round(alpha * 100)}%, transparent)`;

function cellColor(stat: DayStat | undefined): string {
  if (!stat || stat.success + stat.failed + stat.other === 0) {
    return 'var(--hm-empty, rgba(127,127,127,0.10))';
  }
  const total = stat.success + stat.failed;
  if (stat.failed > 0) {
    return stat.failed >= Math.max(1, total / 2) ? fade('var(--danger)', 0.85) : fade('var(--warning)', 0.85);
  }
  const intensity = stat.success >= 100 ? 1 : stat.success >= 25 ? 0.75 : stat.success >= 5 ? 0.5 : 0.3;
  return fade('var(--success)', intensity);
}

export function RunHeatmap({
  workflowId, weeks: defaultWeeks = 16, selectable = false,
  gaps, gapsSince, gapsComplete = true, onWeeksChange,
}: {
  workflowId?: number;
  /** Fixed range when not selectable; initial fallback when selectable. */
  weeks?: number;
  /** Show the 4w/8w/16w/6m range control; the choice persists across sessions. */
  selectable?: boolean;
  /** Per-day expected/ran/missed/blocked/paused counts from
   *  GET /workflows/{id}/schedule-gaps — `daily`, keyed by the same UTC day
   *  string /stats/daily uses. Omit for the all-workflow heatmap. */
  gaps?: GapDay[];
  /** `window.since` from the same response: the oldest instant the scan
   *  actually reached. Days before it carry no gap treatment. */
  gapsSince?: string | null;
  /** `complete` from the same response. False means the scan stopped early and
   *  the grid says so instead of implying the history is whole. */
  gapsComplete?: boolean;
  /** Fires with the selected day-count whenever the range changes (and once on
   *  mount), so a host owning the gap scan can widen it to match. */
  onWeeksChange?: (days: number) => void;
}) {
  const [stats, setStats] = useState<Record<string, DayStat>>({});
  const [weeks, setWeeks] = useState(() => (selectable ? storedWeeks(defaultWeeks) : defaultWeeks));

  const pickWeeks = (w: number) => {
    setWeeks(w);
    try { localStorage.setItem(WEEKS_STORAGE_KEY, String(w)); } catch { /* non-fatal */ }
  };

  // Through a ref so a host that passes an inline arrow does not re-fire this
  // on every render of its own.
  const notifyWeeks = useRef(onWeeksChange);
  useEffect(() => { notifyWeeks.current = onWeeksChange; });
  useEffect(() => { notifyWeeks.current?.(weeks * 7); }, [weeks]);

  useEffect(() => {
    let stale = false; // ignore out-of-order responses when switching ranges quickly
    const params = new URLSearchParams({ days: String(weeks * 7), tz: viewerZone() });
    if (workflowId) params.set('workflow_id', String(workflowId));
    api<DayStat[]>(`/stats/daily?${params}`)
      .then(rows => { if (!stale) setStats(Object.fromEntries(rows.map(r => [r.date, r]))); })
      .catch(() => {});
    return () => { stale = true; };
  }, [workflowId, weeks]);

  const gapByDate = useMemo(
    () => Object.fromEntries((gaps ?? []).map(g => [g.date, g])) as Record<string, GapDay>,
    [gaps]);
  // A day is inside the scan when any part of it is: the floor can land mid-day.
  const gapFloor = gapsSince ? new Date(gapsSince).getTime() : null;

  /* Calendar arithmetic, not milliseconds. Stepping by a fixed 24h from local
     midnight drifts an hour at every DST boundary, and one fall-back day later
     lands back on the day it started — a duplicated square and a missing one,
     twice a year. setDate() moves whole calendar days by definition. */
  const addDays = (from: Date, count: number) => {
    const next = new Date(from);
    next.setDate(next.getDate() + count);
    return next;
  };
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  // Last cell is today; each column is one Monday → Sunday week.
  const start = addDays(today, -(weeks * 7 - 1));
  const firstMonday = addDays(start, -((start.getDay() + 6) % 7));

  type Cell = { stat: DayStat | undefined; date: Date; key: string };
  const columns: { key: number; days: Cell[] }[] = [];
  const monthLabels: { index: number; label: string }[] = [];
  let lastMonth = -1;
  for (let week = 0; ; week++) {
    const weekStart = addDays(firstMonday, week * 7);
    if (weekStart > today) break;
    const days = Array.from({ length: 7 }, (_, dow) => {
      const date = addDays(weekStart, dow);
      const key = localDateKey(date);
      const inRange = date >= start && date <= today;
      return {
        date, key,
        stat: !inRange ? undefined
          : stats[key] ?? { date: '', success: 0, failed: 0, other: 0 },
      };
    });
    const month = weekStart.getMonth();
    if (month !== lastMonth) {
      monthLabels.push({ index: week, label: weekStart.toLocaleDateString(undefined, { month: 'short' }) });
      lastMonth = month;
    }
    columns.push({ key: weekStart.getTime(), days });
  }

  // Only worth a footnote when the grid actually reaches past the scan.
  const showGapFloor = Boolean(gaps) && gapFloor !== null && gapFloor > start.getTime();

  return (
    <div className="run-heatmap-wrap">
      {selectable && (
        <div className="segmented slim run-heatmap-range" role="group" aria-label="Activity range">
          {RANGES.map(r => (
            <button
              key={r.weeks}
              type="button"
              className={r.weeks === weeks ? 'active' : ''}
              aria-pressed={r.weeks === weeks}
              title={`Last ${r.weeks} weeks`}
              onClick={() => pickWeeks(r.weeks)}
            >
              {r.label}
            </button>
          ))}
        </div>
      )}
      <div className="run-heatmap">
        <div className="run-heatmap-months" style={{ paddingLeft: 26 }}>
          {monthLabels.map(({ index, label }, i) => (
            <span key={i} style={{ left: index * (CELL + GAP) }}>{label}</span>
          ))}
        </div>
        <div className="run-heatmap-body">
          <div className="run-heatmap-dows">
            {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(d => <span key={d}>{d}</span>)}
          </div>
          {/* Keyed on the range so switching replays the columnar wave-in. */}
          <div key={weeks} className="run-heatmap-grid" style={{ gap: GAP }}>
            {columns.map((col, colIndex) => (
              <div key={col.key} className="run-heatmap-col"
                   style={{ gap: GAP, animationDelay: `${colIndex * 10}ms` }}>
                {col.days.map(({ stat, date, key }, dow) => {
                  const t = date.getTime();
                  const runs = stat ? stat.success + stat.failed + stat.other : 0;

                  // Gap state, only where the scan actually looked.
                  const inScan = Boolean(gaps) && (gapFloor === null || t + DAY_MS > gapFloor);
                  const gap = inScan ? gapByDate[key] : undefined;
                  const missed = (gap?.missed ?? 0) > 0;
                  // The ring is for a day that would otherwise read as quiet
                  // when in truth nothing was owed of it.
                  const pausedOnly = !missed && runs === 0 && (gap?.paused ?? 0) > 0;

                  let gapTip = '';
                  if (gap) {
                    const bits: string[] = [];
                    if (gap.missed) bits.push(`${gap.missed} missed`);
                    if (gap.blocked) bits.push(`${gap.blocked} skipped (run already queued)`);
                    if (gap.paused) bits.push(`${gap.paused} not owed (paused)`);
                    if (!bits.length && gap.expected) bits.push(`all ${gap.expected} scheduled runs on time`);
                    gapTip = bits.length ? ` · ${bits.join(' · ')}` : '';
                  } else if (gaps && !inScan) {
                    gapTip = ' · not scanned for missed runs';
                  }

                  const tip = stat === undefined ? undefined
                    : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}` +
                      (runs
                        ? ` · ${stat.success} ok${stat.failed ? ` · ${stat.failed} failed` : ''}${stat.other ? ` · ${stat.other} other` : ''}`
                        : ' · no runs') + gapTip;
                  const style: CSSProperties = {
                    width: CELL, height: CELL,
                    visibility: stat === undefined ? 'hidden' : 'visible',
                    background: cellColor(stat),
                  };
                  const className = clsx('run-heatmap-cell',
                                         missed && 'run-heatmap-cell--missed',
                                         pausedOnly && 'run-heatmap-cell--paused');
                  // A day with runs links to exactly those runs. A day with
                  // none has nothing to show, so it stays a plain square
                  // rather than a link that lands on an empty table.
                  if (stat === undefined || runs === 0) {
                    return <span key={dow} className={className} title={tip} style={style} />;
                  }
                  const query = new URLSearchParams({ day: key, tz: viewerZone() });
                  if (workflowId) query.set('workflow', String(workflowId));
                  return (
                    <Link key={dow}
                          to={`/runs?${query}`}
                          className={clsx(className, 'run-heatmap-cell--link')}
                          title={`${tip} · click to see these runs`}
                          aria-label={tip}
                          style={style} />
                  );
                })}
              </div>
            ))}
          </div>
        </div>
        <div className="run-heatmap-legend">
          <span>Fewer</span>
          {[0.3, 0.5, 0.75, 1].map(o => (
            <span key={o} className="run-heatmap-cell" style={{ width: CELL, height: CELL, background: fade('var(--success)', o) }} />
          ))}
          <span>More</span>
          <span className="run-heatmap-cell" style={{ width: CELL, height: CELL, background: fade('var(--warning)', 0.85), marginLeft: 10 }} />
          <span>Mixed</span>
          <span className="run-heatmap-cell" style={{ width: CELL, height: CELL, background: fade('var(--danger)', 0.85) }} />
          <span>Failing</span>
          {gaps && (
            <>
              <span className="run-heatmap-cell run-heatmap-cell--missed"
                    style={{ width: CELL, height: CELL, background: 'var(--hm-empty, rgba(127,127,127,0.10))', marginLeft: 10 }} />
              <span>Missed</span>
              <span className="run-heatmap-cell run-heatmap-cell--paused"
                    style={{ width: CELL, height: CELL, background: 'transparent' }} />
              <span>Paused</span>
            </>
          )}
        </div>
        {gaps && (showGapFloor || !gapsComplete) && (
          <p className="run-heatmap-gap-note">
            {gapFloor !== null && `Missed-run marks cover ${new Date(gapFloor).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} onward. `}
            {gapsComplete
              ? 'Earlier days in this range were not scanned for missed runs.'
              : 'The scan stopped early — earlier days are unchecked, not clean.'}
          </p>
        )}
      </div>
    </div>
  );
}
