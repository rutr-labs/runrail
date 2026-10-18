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

/* The zone maths below falls back to UTC for a name Intl rejects (see cron.ts),
   which would quietly show UTC times under a wrong label. Callers ask first. */
const knownZones = new Map<string, boolean>();
export function isKnownZone(zone: string): boolean {
  if (!knownZones.has(zone)) {
    try { new Intl.DateTimeFormat('en-US', { timeZone: zone }); knownZones.set(zone, zone !== ''); }
    catch { knownZones.set(zone, false); }
  }
  return knownZones.get(zone)!;
}

function formatInZone(date: Date, zone: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: zone, weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
  } catch {
    return date.toISOString().slice(0, 16).replace('T', ' ');
  }
}

export function untilLabel(target: Date, now: number): string {
  const minutes = Math.max(0, Math.round((target.getTime() - now) / 60_000));
  if (minutes < 1) return 'in under a minute';
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `in ${hours}h${minutes % 60 ? ` ${minutes % 60}m` : ''}`;
  const days = Math.floor(hours / 24);
  return `in ${days}d${hours % 24 ? ` ${hours % 24}h` : ''}`;
}

/** What the windows do next, in words, for this zone. About the schedule, not
 *  the process: a service can be stopped inside a window, so this never says
 *  "running". */
export function scheduleSummary(windows: ServiceWindow[], zone: string, now = Date.now()): string {
  if (!windows.length) return 'No windows yet — it will never start on its own.';
  if (!isKnownZone(zone)) return `Unknown timezone "${zone}" — fix it to see when this runs.`;
  const at = new Date(now);
  const change = nextChange(windows, zone, at);
  const active = isActive(windows, zone, at);
  if (!change) return active ? 'Covers the whole week — always on.' : 'Never runs.';
  return active
    ? `In a window now · closes ${formatInZone(change, zone)} (${untilLabel(change, now)})`
    : `Next window opens ${formatInZone(change, zone)} (${untilLabel(change, now)})`;
}

/** End at or before start: the window runs past midnight into the next day. */
export const crossesMidnight = (w: ServiceWindow): boolean =>
  minutesOf(w.end) <= minutesOf(w.start);

/** A wall time in the zone as an instant (ms). A time inside a spring-forward
 *  gap does not exist; it becomes the instant the clocks jump, the first one
 *  that does — as windows.py does. (zonedTimeToUtc alone keeps the offset from
 *  before the jump and lands an hour into the far side, 02:30 → 03:30.) */
function wallInstant(y: number, mo: number, d: number, h: number, mi: number, zone: string): number {
  const instant = zonedTimeToUtc(y, mo, d, h, mi, zone).getTime();
  const reads = (t: number): boolean => {
    const w = wallParts(new Date(t), zone);
    return w.y === y && w.mo === mo && w.d === d && w.h === h && w.mi === mi;
  };
  if (reads(instant)) return instant;
  const offsetAt = (t: number): number => {
    const w = wallParts(new Date(t), zone);
    return Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi) - t;
  };
  // The two offsets straddle the transition; find it to the minute.
  const after = offsetAt(instant + 86_400_000);
  let low = instant - 86_400_000, high = instant;
  while (high - low > 60_000) {
    const middle = low + Math.floor((high - low) / 2 / 60_000) * 60_000;
    if (offsetAt(middle) === after) high = middle; else low = middle + 60_000;
  }
  return high;
}

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
      const opens = wallInstant(y, mo, d, sh, sm, zone);
      const closes = wallInstant(end[0], end[1], end[2], eh, em, zone);
      if (closes > opens) spans.push([opens, closes]); // wholly inside a gap: never happens
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
