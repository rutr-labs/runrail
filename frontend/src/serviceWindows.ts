/* ─── Service windows ──────────────────────────────────────
   The browser's copy of src/runrail/services/windows.py, so the week editor
   previews exactly what the supervisor will do. A window is weekdays plus a
   wall-clock span in the service's zone; the days name where it STARTS, an end
   at or before the start crosses midnight, and an end equal to the start is
   the whole day. Overlapping windows act as one. The test files on both sides
   pin the same cases. */

import { wallParts, zonedTimeToUtc } from './cron';

export const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const;
export type Day = typeof DAYS[number];
export const DAY_SHORT: Record<Day, string> = {
  mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun',
};

export interface ServiceWindow { days: Day[]; start: string; end: string }

const HORIZON_DAYS = 8;
const DAY_MS = 86_400_000;

export const minutesOf = (hhmm: string): number => {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
};

export const validTime = (value: string): boolean =>
  /^([01]\d|2[0-3]):[0-5]\d$/.test(value);

/** End at or before start: the window runs past midnight into the next day. */
export const crossesMidnight = (w: ServiceWindow): boolean =>
  minutesOf(w.end) <= minutesOf(w.start);

/* Monday-first weekday index (0 = Monday) of a civil date. */
const mondayIndex = (y: number, mo: number, d: number): number =>
  (new Date(Date.UTC(y, mo - 1, d)).getUTCDay() + 6) % 7;

const addDays = (y: number, mo: number, d: number, n: number): [number, number, number] => {
  const t = new Date(Date.UTC(y, mo - 1, d) + n * DAY_MS);
  return [t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate()];
};

/** Window occurrences starting on the `days` dates from `first` (a civil date
 *  in the zone), as merged, sorted [start, end) instants in ms. */
export function intervals(windows: ServiceWindow[], zone: string,
                          first: [number, number, number], days: number): [number, number][] {
  const spans: [number, number][] = [];
  for (let offset = 0; offset < days; offset++) {
    const [y, mo, d] = addDays(...first, offset);
    const weekday = DAYS[mondayIndex(y, mo, d)];
    for (const w of windows) {
      if (!w.days.includes(weekday) || !validTime(w.start) || !validTime(w.end)) continue;
      const [sh, sm] = w.start.split(':').map(Number);
      const [eh, em] = w.end.split(':').map(Number);
      const end = crossesMidnight(w) ? addDays(y, mo, d, 1) : [y, mo, d] as const;
      spans.push([zonedTimeToUtc(y, mo, d, sh, sm, zone).getTime(),
                  zonedTimeToUtc(end[0], end[1], end[2], eh, em, zone).getTime()]);
    }
  }
  spans.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [s, e] of spans) {
    const last = merged[merged.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  return merged;
}

function around(windows: ServiceWindow[], zone: string, at: Date): [number, number][] {
  const w = wallParts(at, zone);
  return intervals(windows, zone, addDays(w.y, w.mo, w.d, -1), HORIZON_DAYS + 1);
}

export function isActive(windows: ServiceWindow[], zone: string, at: Date): boolean {
  const t = at.getTime();
  return around(windows, zone, at).some(([s, e]) => s <= t && t < e);
}

/** The next instant the schedule flips, or null when there are no windows or
 *  they cover the whole week without a break. */
export function nextChange(windows: ServiceWindow[], zone: string, at: Date): Date | null {
  const t = at.getTime();
  const spans = around(windows, zone, at);
  for (const [s, e] of spans) {
    if (s <= t && t < e) {
      const unbroken = spans.length === 1 && e - s >= 7 * DAY_MS;
      return unbroken ? null : new Date(e);
    }
    if (s > t) return new Date(s);
  }
  return null;
}

/** Per weekday row of the week view (Monday first), the [from, to) minute
 *  spans each window paints, with overnight windows continuing on the next
 *  row. `index` is the window's position, so a row can be traced back to it. */
export function weekSegments(windows: ServiceWindow[]): { from: number; to: number; index: number }[][] {
  const rows: { from: number; to: number; index: number }[][] = DAYS.map(() => []);
  windows.forEach((w, index) => {
    if (!validTime(w.start) || !validTime(w.end)) return;
    const start = minutesOf(w.start);
    const end = minutesOf(w.end);
    for (const day of w.days) {
      const row = DAYS.indexOf(day);
      if (end > start) {
        rows[row].push({ from: start, to: end, index });
      } else {
        rows[row].push({ from: start, to: 1440, index });
        if (end > 0) rows[(row + 1) % 7].push({ from: 0, to: end, index });
      }
    }
  });
  return rows;
}

/** Total scheduled hours in a week, overlaps counted once. */
export function weeklyHours(windows: ServiceWindow[]): number {
  return weekSegments(windows).reduce((sum, row) => {
    const marks = new Array(1440).fill(false);
    for (const { from, to } of row) for (let m = from; m < to; m++) marks[m] = true;
    return sum + marks.filter(Boolean).length / 60;
  }, 0);
}

/** "Mon–Fri", "Mon, Wed, Fri", "Every day", "Weekends". */
export function daysLabel(days: Day[]): string {
  const set = DAYS.filter(d => days.includes(d));
  if (set.length === 7) return 'Every day';
  if (set.length === 5 && !set.includes('sat') && !set.includes('sun')) return 'Weekdays';
  if (set.length === 2 && set.includes('sat') && set.includes('sun')) return 'Weekends';
  const idx = set.map(d => DAYS.indexOf(d));
  const contiguous = idx.length > 2 && idx.every((v, i) => i === 0 || v === idx[i - 1] + 1);
  if (contiguous) return `${DAY_SHORT[set[0]]}–${DAY_SHORT[set[set.length - 1]]}`;
  return set.map(d => DAY_SHORT[d]).join(', ');
}

/** "Weekdays 08:00 → 17:00", "Fri 22:00 → 06:00 next day". */
export function windowLabel(w: ServiceWindow): string {
  const span = w.start === w.end ? 'all day'
    : `${w.start} → ${w.end}${crossesMidnight(w) ? ' next day' : ''}`;
  return `${daysLabel(w.days)} ${span}`;
}
