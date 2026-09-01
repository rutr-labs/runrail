/* ─── Timezone-aware cron helpers ─────────────────────────
   Schedules evaluate as wall-clock in the workflow's IANA timezone
   (UTC when unset), matching the server's APScheduler semantics —
   including the unintuitive ones. A wall time inside a spring-forward
   gap is NOT skipped: it fires, resolved against the offset in force
   before the jump, so 02:30 runs at 03:30 on that one day. An ambiguous
   fall-back time takes the earlier of its two instants. Occurrences are
   computed by probing real instants through Intl (no offset tables
   shipped), so DST is exact for any zone.

   Supported grammar mirrors what the ScheduleBuilder emits plus the
   pre-existing fast paths: minute/hour accept N, *, and *​/N; day-of-
   month accepts N or *; month must be *; day-of-week accepts N, a comma
   list, or *, counting 0=Sun..6=Sat with 7 a second spelling of Sunday —
   the standard-crontab dialect src/runrail/crontab.py translates for the
   scheduler. Every field is range-checked against what that parser
   accepts: an expression the server would refuse must never be given a
   confident label or a next-run time, because it will never run.
   Anything else returns null and callers fall back to showing the raw
   expression. */

/* Inclusive field bounds, matching the server's parser column for column.
   A step wider than the span is rejected there too — a 61-minute step over
   0-59 is not a cadence, it is a typo. */
const MINUTE = [0, 59] as const;
const HOUR = [0, 23] as const;
const DOM = [1, 31] as const;
const WEEKDAY = [0, 7] as const;

type Bounds = readonly [number, number];

const stepOf = (part: string): number | null => {
  const match = part.match(/^\*\/(\d+)$/);
  return match ? Number(match[1]) || null : null;
};

const fieldMatch = (part: string, value: number): boolean => {
  if (part === '*') return true;
  const step = stepOf(part);
  if (step) return value % step === 0;
  return part.split(',').some(p => Number(p) === value);
};

const fieldValid = (part: string, [min, max]: Bounds): boolean => {
  if (part === '*') return true;
  const step = stepOf(part);
  if (step) return step <= max - min;
  return part.split(',').every(p => /^\d+$/.test(p) && +p >= min && +p <= max);
};

/* 7 is Sunday in crontab, and the server translates it to the same day, so
   the preview must not disagree about the one day it names. */
const sundayIsZero = (part: string): string =>
  part.split(',').map(p => (p === '7' ? '0' : p)).join(',');

/* Cached per-zone formatters; an unknown zone falls back to UTC so a
   stale/bad value degrades to the old behavior instead of crashing. */
const dtfCache = new Map<string, Intl.DateTimeFormat | null>();
function zonedFormatter(tz: string): Intl.DateTimeFormat | null {
  if (!dtfCache.has(tz)) {
    try {
      dtfCache.set(tz, new Intl.DateTimeFormat('en-US', {
        timeZone: tz, hour12: false, year: 'numeric', month: 'numeric',
        day: 'numeric', hour: 'numeric', minute: 'numeric', weekday: 'short',
      }));
    } catch {
      dtfCache.set(tz, null);
    }
  }
  return dtfCache.get(tz) ?? null;
}

const DOW: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

interface Wall { y: number; mo: number; d: number; h: number; mi: number; dow: number }

function wallParts(date: Date, tz: string): Wall {
  const formatter = zonedFormatter(tz) ?? zonedFormatter('UTC')!;
  const parts: Record<string, string> = {};
  for (const part of formatter.formatToParts(date)) parts[part.type] = part.value;
  return {
    y: +parts.year, mo: +parts.month, d: +parts.day,
    h: +parts.hour % 24, mi: +parts.minute, dow: DOW[parts.weekday] ?? 0,
  };
}

/* Wall-clock → instant, resolved the way the server's zoneinfo does: with the
   offset in force BEFORE the transition. That makes an ambiguous fall-back
   time the earlier of its two instants, and a wall time that never happens at
   all the instant the schedule really fires — an hour past the wall time the
   operator typed, never a skipped day. */
function zonedTimeToUtc(
  y: number, mo: number, d: number, h: number, mi: number, tz: string,
): Date {
  const target = Date.UTC(y, mo - 1, d, h, mi);
  const offsetAt = (instant: number): number => {
    const w = wallParts(new Date(instant), tz);
    return Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi) - instant;
  };
  const reads = (instant: number): boolean => {
    const w = wallParts(new Date(instant), tz);
    return w.y === y && w.mo === mo && w.d === d && w.h === h && w.mi === mi;
  };
  /* A day either side brackets any transition. The earlier offset wins when
     both read back correctly, and is the fallback when neither does — which
     is exactly the gap. */
  const earlier = target - offsetAt(target - 86_400_000);
  if (reads(earlier)) return new Date(earlier);
  const later = target - offsetAt(target + 86_400_000);
  return new Date(reads(later) ? later : earlier);
}

interface Fields { min: string; hour: string; dom: string; dow: string }

/** The fields this engine can evaluate, normalised, or null when the
 *  expression is outside its grammar or outside the ranges the server's parser
 *  accepts. Callers render the raw text instead. */
function parse(expr: string): Fields | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [min, hour, dom, month, dow] = parts;
  if (month !== '*') return null;
  if (!fieldValid(min, MINUTE) || !fieldValid(hour, HOUR) || !fieldValid(dow, WEEKDAY)) return null;
  if (dom !== '*' && !(/^\d+$/.test(dom) && +dom >= DOM[0] && +dom <= DOM[1])) return null;
  /* dom+dow both restricted uses cron's OR semantics — out of scope here. */
  if (dom !== '*' && dow !== '*') return null;
  return { min, hour, dom, dow: sundayIsZero(dow) };
}

/* The wallboard asks every second for every workflow; cron resolves at
   minute granularity, so memoize on (expr, tz, minute-of-after). */
const occCache = new Map<string, Date | null>();

export function nextCronOccurrence(
  expr: string, tz?: string | null, after: Date = new Date(),
): Date | null {
  const fields = parse(expr);
  if (!fields) return null;
  const zone = tz || 'UTC';

  const bucket = Math.floor(after.getTime() / 60_000);
  const key = `${expr}|${zone}|${bucket}`;
  if (occCache.has(key)) return occCache.get(key)!;
  if (occCache.size > 512) occCache.clear();

  const result = compute(fields, zone, after);
  occCache.set(key, result);
  return result;
}

function compute(f: Fields, zone: string, after: Date): Date | null {
  const subDaily = f.hour === '*' || stepOf(f.hour) != null;

  if (subDaily) {
    /* Minute walk, bounded to 26h — covers every sub-daily pattern the
       builder can emit, across any offset including :30/:45 zones. */
    const cursor = new Date(after);
    cursor.setSeconds(0, 0);
    for (let i = 0; i < 26 * 60; i++) {
      cursor.setMinutes(cursor.getMinutes() + 1);
      const w = wallParts(cursor, zone);
      if (fieldMatch(f.min, w.mi) && fieldMatch(f.hour, w.h) &&
          fieldMatch(f.dom, w.d) && fieldMatch(f.dow, w.dow)) {
        return new Date(cursor);
      }
    }
    return null;
  }

  const hour = Number(f.hour);
  const minute = f.min === '*' ? 0 : Number(f.min);
  if (Number.isNaN(hour) || Number.isNaN(minute)) return null;

  /* Fixed-time: walk half-days (so 23h DST days can't skip a wall date)
     across ~13 months for monthly patterns. */
  const seen = new Set<string>();
  for (let halfDay = 0; halfDay < 800; halfDay++) {
    const probe = new Date(after.getTime() + halfDay * 43_200_000);
    const w = wallParts(probe, zone);
    const dateKey = `${w.y}-${w.mo}-${w.d}`;
    if (seen.has(dateKey)) continue;
    seen.add(dateKey);
    if (!fieldMatch(f.dom, w.d) || !fieldMatch(f.dow, w.dow)) continue;
    const instant = zonedTimeToUtc(w.y, w.mo, w.d, hour, minute, zone);
    if (instant > after) return instant;
  }
  return null;
}

/* Short zone tag for labels: "GMT+4", "GMT+5:30" — only shown when the
   schedule's zone differs from the viewer's. */
export function zoneTag(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' })
      .formatToParts(new Date());
    return parts.find(p => p.type === 'timeZoneName')?.value ?? tz;
  } catch {
    return tz;
  }
}

/* One definition, in format.ts, because the day-bucket helpers and the cron
   labels have to agree about which zone the viewer is in. Re-exported here so
   this module's existing callers are untouched. */
export { viewerZone } from './format';
import { viewerZone } from './format';

/** Human label for a cron. Fixed-time schedules read as wall-clock in the
 *  WORKFLOW's zone (that is what the author chose), tagged with the offset
 *  when it differs from the viewer's zone; sub-hourly cadences are zoneless. */
export function cronLabel(expr: string, tz?: string | null): string {
  const fields = parse(expr);
  if (!fields) return expr;
  const { min: minutePart, hour: hourPart, dom: domPart, dow: dowPart } = fields;

  if (hourPart === '*' && dowPart === '*' && domPart === '*') {
    if (minutePart === '*') return 'every minute';
    const minuteStep = stepOf(minutePart);
    if (minuteStep) return `every ${minuteStep} min`;
  }
  const hourStep = stepOf(hourPart);
  if (hourStep && dowPart === '*' && domPart === '*') {
    return hourStep === 1 ? 'hourly' : `every ${hourStep}h`;
  }

  const zone = tz || 'UTC';
  const next = nextCronOccurrence(expr, zone);
  if (!next) return expr;

  const wall = wallParts(next, tz ? zone : viewerZone());
  const time = `${String(wall.h).padStart(2, '0')}:${String(wall.mi).padStart(2, '0')}`;
  const tag = tz && tz !== viewerZone() ? ` ${zoneTag(zone)}` : '';

  if (hourPart === '*' && dowPart === '*' && domPart === '*') {
    return `hourly at :${String(wall.mi).padStart(2, '0')}${tag}`;
  }
  if (domPart !== '*') return `monthly on day ${domPart} at ${time}${tag}`;
  if (dowPart === '*') return `daily at ${time}${tag}`;
  const days = [...new Set(dowPart.split(',').map(Number))];
  // A step has no list of day names to give; the raw expression is the honest
  // label rather than a weekly one with the days left out.
  if (days.some(day => !Number.isInteger(day))) return expr;
  const names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const dayLabel = days.length === 1
    ? `every ${names[days[0]]}`
    : days.map(d => names[d]).join('/');
  return `${dayLabel} at ${time}${tag}`;
}
