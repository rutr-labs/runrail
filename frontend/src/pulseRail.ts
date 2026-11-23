/* ─── The timetable's clock ────────────────────────────────
   The home screen draws recent runs to the left of "now" and the schedule's
   next fires to the right of it, on one continuous time axis. This file holds
   the decisions about that axis that are easy to get subtly wrong and awkward
   to check by eye: how much history it may claim to show, and where the clock
   labels fall. Which workflows get a line is in board.ts. */

const MINUTE = 60_000;
const HOUR = 3_600_000;

export type RailRun = {
  id: number;
  workflow_id: number;
  status: string;
  created_at: string;
  started_at?: string | null;
  duration_seconds?: number | null;
};

/** How far back the strip can look. The future half is always half of this,
 *  so both sides share one scale. */
export const RAIL_WINDOWS = [HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, 24 * HOUR];

/** Clock-label spacing for each window: four to six labels across the past. */
const AXIS_STEP: Record<number, number> = {
  [HOUR]: 15 * MINUTE,
  [3 * HOUR]: 30 * MINUTE,
  [6 * HOUR]: HOUR,
  [12 * HOUR]: 2 * HOUR,
  [24 * HOUR]: 4 * HOUR,
};

export const axisStep = (window: number): number => AXIS_STEP[window] ?? window / 4;

/** When a run counts as having happened: its start, or its creation while it
 *  is still queued. */
export const runAt = (run: RailRun): number =>
  new Date(run.started_at || run.created_at).getTime();

/** The widest window the loaded runs can fill honestly and legibly.
 *
 *  Honestly: the dashboard holds only the newest N runs. Unless that is the
 *  whole history (`complete`), a window reaching back past the oldest loaded
 *  run would draw an empty stretch where runs really happened.
 *
 *  Legibly: past roughly `target` runs the columns saturate, so a busy install
 *  gets the last hour and a quiet one gets the whole day.
 *
 *  `previous` adds hysteresis. Without it an install hovering at the threshold
 *  would flip between two scales every time a run arrived. */
export function railWindow(
  runs: RailRun[], now: number, complete: boolean, previous?: number, target = 60,
): number {
  const times = runs.map(runAt).filter(t => t <= now);
  const oldest = complete || times.length === 0 ? -Infinity : Math.min(...times);
  let pick = RAIL_WINDOWS[0];
  for (const window of RAIL_WINDOWS.slice(1)) {
    if (now - window < oldest) break;
    const count = times.filter(t => t >= now - window).length;
    const cap = previous != null && window <= previous ? target * 1.2 : target * 0.8;
    if (count > cap) break;
    pick = window;
  }
  return pick;
}

/** Clock boundaries inside [start, end], aligned to the viewer's wall clock —
 *  a four-hour step lands on 00:00, 04:00, 08:00 local, not on UTC's. */
export function railAxis(start: number, end: number, step: number, tzOffsetMinutes: number): number[] {
  const shift = tzOffsetMinutes * MINUTE;
  const marks: number[] = [];
  for (let t = Math.ceil((start - shift) / step) * step + shift; t <= end; t += step) marks.push(t);
  return marks;
}

export function railWindowLabel(window: number): string {
  return window === HOUR ? 'Last hour' : `Last ${window / HOUR} hours`;
}

export function railHorizonLabel(horizon: number): string {
  if (horizon < 2 * HOUR) return `Next ${Math.round(horizon / MINUTE)} min`;
  return `Next ${horizon / HOUR} hours`;
}

/** "in 12m", "in 3h 5m" — the distance to a scheduled fire. */
export function untilLabel(ms: number): string {
  if (ms < MINUTE) return 'in under a minute';
  const minutes = Math.round(ms / MINUTE);
  if (minutes < 60) return `in ${minutes}m`;
  return minutes % 60 ? `in ${Math.floor(minutes / 60)}h ${minutes % 60}m` : `in ${minutes / 60}h`;
}
