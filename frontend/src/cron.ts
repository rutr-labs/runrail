/* ─── Timezone-aware cron helpers ─────────────────────────
   Schedules evaluate as wall-clock in the workflow's IANA timezone
   (UTC when unset), matching the server's APScheduler semantics —
   including the unintuitive ones. A wall time inside a spring-forward
   gap is NOT skipped: it fires, resolved against the offset in force
   before the jump, so 02:30 runs at 03:30 on that one day. On a fall-back
   night a schedule pinned to particular hours fires once, at the earlier
   of the two instants; one whose hour field is * or a step fires in both
   repeated hours, as the server does. Occurrences are computed by probing
   real instants through Intl (no offset tables shipped), so DST is exact
   for any zone.

   The grammar is standard five-field cron, as src/runrail/crontab.py hands
   it to the scheduler: each field a comma list of `*`, `N`, `A-B`, with an
   optional `/step` (`N/step` counts from N to the end of the field), month
   and weekday names (JAN-DEC, SUN-SAT), day-of-week 0=Sun..6=Sat with 7 a
   second Sunday. Every field is range-checked against what that parser
   accepts, and a schedule restricting both day-of-month and day-of-week is
   refused there, so it is refused here: an expression the server would
   refuse must never be given a confident label or a next-run time. The
   server's rarer forms (`last`, `1st mon`) are not evaluated here; they
   return null and callers show the raw expression. */

/* ─── Fields ──────────────────────────────────────────────── */

const MONTH_NAMES = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
const DAY_NAMES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];

interface FieldRule {
  name: string;
  /** What `*` covers, and the highest number written out (7 is Sunday). */
  lo: number; hi: number; max: number;
  names?: string[]; nameBase?: number;
}

const RULES: FieldRule[] = [
  { name: 'minute', lo: 0, hi: 59, max: 59 },
  { name: 'hour', lo: 0, hi: 23, max: 23 },
  { name: 'day-of-month', lo: 1, hi: 31, max: 31 },
  { name: 'month', lo: 1, hi: 12, max: 12, names: MONTH_NAMES, nameBase: 1 },
  { name: 'day-of-week', lo: 0, hi: 6, max: 7, names: DAY_NAMES, nameBase: 0 },
];

/** Not wrong, only outside what this preview evaluates (`last`, `1st mon`):
 *  the server is the judge of those. */
const UNREAD = Symbol('unread');

/** The values one field allows, sorted, or a reason it is not a field. */
function expand(field: string, rule: FieldRule): number[] | string | typeof UNREAD {
  const values = new Set<number>();
  const number = (token: string): number | null => {
    if (/^\d+$/.test(token)) return Number(token);
    const index = rule.names?.indexOf(token) ?? -1;
    return index >= 0 ? index + (rule.nameBase ?? 0) : null;
  };
  for (const part of field.toLowerCase().split(',')) {
    const m = part.match(/^(?:(\*)|([a-z0-9]+)(?:-([a-z0-9]+))?)(?:\/(\d+))?$/);
    if (!m) return UNREAD;
    const step = m[4] !== undefined ? Number(m[4]) : null;
    if (step !== null && step < 1) return `a step must be at least 1`;
    let low: number, high: number;
    if (m[1]) {
      low = rule.lo; high = rule.hi;
      // A step wider than the whole field is a typo, not a cadence: */61.
      if (step !== null && step > rule.max - rule.lo) return `a step of ${step} is wider than the field`;
    } else {
      const first = number(m[2]);
      const last = m[3] !== undefined ? number(m[3]) : step !== null ? rule.hi : first;
      if (first === null || last === null) return UNREAD;
      if (first > last) return `the range ${part} runs backwards`;
      low = first; high = last;
    }
    if (low < rule.lo || high > rule.max) return `${part} is outside ${rule.lo}-${rule.max}`;
    for (let v = low; v <= high; v += step ?? 1) values.add(v);
  }
  return [...values].sort((a, b) => a - b);
}

interface Spec {
  minutes: number[]; hours: number[];
  /** null when the field is `*`: no restriction. */
  dom: number[] | null; months: number[] | null; dows: number[] | null;
  raw: string[];
  /** Hours given as numbers or ranges (no * or step): the server fires such a
   *  schedule once on a fall-back night, not in both repeated hours. */
  pinned: boolean;
}

/** The expression read, why it cannot be, or UNREAD. */
function read(expr: string): Spec | string | typeof UNREAD {
  const raw = expr.trim().split(/\s+/).filter(Boolean);
  if (raw.length !== 5) return 'A cron expression has five fields: minute hour day-of-month month day-of-week';
  const fields: number[][] = [];
  for (let i = 0; i < 5; i++) {
    const values = expand(raw[i], RULES[i]);
    if (values === UNREAD) return UNREAD;
    if (typeof values === 'string') return `${RULES[i].name}: ${values}`;
    fields.push(values);
  }
  if (raw[2] !== '*' && raw[4] !== '*') {
    return 'Restrict day-of-month or day-of-week, not both (use two workflows for both)';
  }
  return {
    minutes: fields[0], hours: fields[1],
    dom: raw[2] === '*' ? null : fields[2],
    months: raw[3] === '*' ? null : fields[3],
    dows: raw[4] === '*' ? null : [...new Set(fields[4].map(d => d % 7))].sort((a, b) => a - b),
    raw,
    pinned: raw[1].split(',').every(part => /^\d+$/.test(part.replace('-', ''))),
  };
}

function parse(expr: string): Spec | null {
  const spec = read(expr);
  return typeof spec === 'object' ? spec : null;
}

/** Why an expression cannot be scheduled, in a line, or null when it reads.
 *  The server is still the judge; this answers before the save does. */
export function cronProblem(expr: string): string | null {
  if (!expr.trim()) return null;
  const spec = read(expr);
  return typeof spec === 'string' ? spec : null;
}

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

export interface Wall { y: number; mo: number; d: number; h: number; mi: number; dow: number }

export function wallParts(date: Date, tz: string): Wall {
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
export function zonedTimeToUtc(
  y: number, mo: number, d: number, h: number, mi: number, tz: string,
): Date {
  return new Date(zonedInstants(y, mo, d, h, mi, tz)[0]);
}

/** Every instant a wall time names: one usually; two on a fall-back night,
 *  earlier first; and, for a time inside a spring-forward gap, the one the
 *  server fires at — resolved with the offset in force BEFORE the jump. */
function zonedInstants(y: number, mo: number, d: number, h: number, mi: number, tz: string): number[] {
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
  const later = target - offsetAt(target + 86_400_000);
  const earlyReads = reads(earlier), lateReads = reads(later);
  if (earlyReads && lateReads && later !== earlier) return [earlier, later];
  if (earlyReads) return [earlier];
  return [lateReads ? later : earlier];
}

/* ─── Next occurrence ─────────────────────────────────────── */

/* The wallboard asks every second for every workflow; cron resolves at
   minute granularity, so memoize on (expr, tz, minute-of-after). */
const occCache = new Map<string, Date | null>();

export function nextCronOccurrence(
  expr: string, tz?: string | null, after: Date = new Date(),
): Date | null {
  const spec = parse(expr);
  if (!spec) return null;
  const zone = tz || 'UTC';

  const bucket = Math.floor(after.getTime() / 60_000);
  const key = `${expr}|${zone}|${bucket}`;
  if (occCache.has(key)) return occCache.get(key)!;
  if (occCache.size > 512) occCache.clear();

  const result = compute(spec, zone, after);
  occCache.set(key, result);
  return result;
}

const DAY_MS = 86_400_000;
/* No zone moves its clock by more than this, so a wall time this far from
   `after` cannot be the next fire, and a candidate this far past the best
   one found cannot beat it. */
const SHIFT_MS = 3 * 3_600_000;
/* 29 February on a Monday: the rarest day a schedule can name comes round
   within 28 years; eight cover every leap-day schedule this century. */
const HORIZON_DAYS = 366 * 8;

function compute(spec: Spec, zone: string, after: Date): Date | null {
  const start = wallParts(after, zone);
  const firstDay = Date.UTC(start.y, start.mo - 1, start.d);
  const at = after.getTime();
  /* The offset at `after`: a cheap first guess at each candidate's instant,
     so only the few near `after` pay for an exact conversion. */
  const offset = Date.UTC(start.y, start.mo - 1, start.d, start.h, start.mi) - Math.floor(at / 60_000) * 60_000;
  for (let i = 0; i < HORIZON_DAYS; i++) {
    const day = new Date(firstDay + i * DAY_MS);
    const y = day.getUTCFullYear(), mo = day.getUTCMonth() + 1, d = day.getUTCDate();
    if (spec.months && !spec.months.includes(mo)) continue;
    if (spec.dom && !spec.dom.includes(d)) continue;
    if (spec.dows && !spec.dows.includes(day.getUTCDay())) continue;
    let best: number | null = null;
    search:
    for (const h of spec.hours) {
      for (const mi of spec.minutes) {
        const guess = Date.UTC(y, mo - 1, d, h, mi) - offset;
        if (guess < at - SHIFT_MS) continue;
        if (best !== null && guess > best + SHIFT_MS) break search;
        const instants = zonedInstants(y, mo, d, h, mi, zone);
        for (const instant of spec.pinned ? instants.slice(0, 1) : instants) {
          if (instant > at && (best === null || instant < best)) best = instant;
        }
      }
    }
    if (best !== null) return new Date(best);
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

/* ─── Labels ──────────────────────────────────────────────── */

const SHORT_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const SHORT_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = (n: number) => String(n).padStart(2, '0');
const stepOf = (part: string): number | null => {
  const match = part.match(/^\*\/(\d+)$/);
  return match ? Number(match[1]) || null : null;
};
const same = (a: number[], b: number[]) => a.length === b.length && a.every((v, i) => v === b[i]);
const list = (items: string[]) =>
  items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;

function dayPhrase(spec: Spec): string | null {
  if (spec.dows) {
    if (same(spec.dows, [1, 2, 3, 4, 5])) return 'weekdays';
    if (same(spec.dows, [0, 6])) return 'weekends';
    return spec.dows.length === 1 ? `every ${SHORT_DAYS[spec.dows[0]]}` : spec.dows.map(d => SHORT_DAYS[d]).join('/');
  }
  if (spec.dom) {
    if (spec.dom.length > 6) return null;
    return spec.dom.length === 1 ? `monthly on day ${spec.dom[0]}` : `on days ${list(spec.dom.map(String))}`;
  }
  return null;
}

function monthPhrase(spec: Spec): string {
  return spec.months ? ` in ${list(spec.months.map(m => SHORT_MONTHS[m - 1]))}` : '';
}

/** Human label for a cron. Fixed-time schedules read as wall-clock in the
 *  WORKFLOW's zone (that is what the author chose), tagged with the offset
 *  when it differs from the viewer's zone; sub-hourly cadences are zoneless.
 *  A shape it cannot say plainly is shown as the expression itself. */
export function cronLabel(expr: string, tz?: string | null): string {
  const spec = parse(expr);
  if (!spec) return expr;
  const [minutePart, hourPart] = spec.raw;
  const days = dayPhrase(spec);
  if ((spec.dows || spec.dom) && !days) return expr;
  const months = monthPhrase(spec);
  const onDays = days ? `, ${days}` : '';

  // Cadences: no time of day to speak of, so no zone either.
  if (hourPart === '*') {
    if (minutePart === '*') return `every minute${onDays}${months}`;
    const minuteStep = stepOf(minutePart);
    if (minuteStep) return `every ${minuteStep} min${onDays}${months}`;
  }
  const hourStep = stepOf(hourPart);
  if (hourStep && spec.minutes.length === 1 && spec.minutes[0] === 0) {
    return `${hourStep === 1 ? 'hourly' : `every ${hourStep}h`}${onDays}${months}`;
  }

  const zone = tz || 'UTC';
  const next = nextCronOccurrence(expr, zone);
  if (!next) return expr;
  const tag = tz && tz !== viewerZone() ? ` ${zoneTag(zone)}` : '';
  /* A schedule with no zone is UTC, shown on the viewer's clock; one with a
     zone is shown on its own. Each time is converted on the next run's date. */
  const shown = tz ? zone : viewerZone();
  const day = wallParts(next, zone);
  const clock = (h: number, mi: number) => {
    const w = wallParts(zonedTimeToUtc(day.y, day.mo, day.d, h, mi, zone), shown);
    return `${pad(w.h)}:${pad(w.mi)}`;
  };

  if (hourPart === '*') {
    if (spec.minutes.length > 4) return expr;
    return `hourly at ${list(spec.minutes.map(mi => `:${pad(mi)}`))}${onDays}${months}${tag}`;
  }
  if (spec.minutes.length === 1 && spec.hours.length <= 4) {
    const times = list(spec.hours.map(h => clock(h, spec.minutes[0])));
    if (!days) return `daily at ${times}${months}${tag}`;
    return `${days} at ${times}${months}${tag}`;
  }
  // Every few hours inside a window: "every 2h, 09:00–17:00".
  const hourGap = spec.hours.length > 1 ? spec.hours[1] - spec.hours[0] : 0;
  if (spec.minutes.length === 1 && hourGap > 1 && spec.hours.every((h, i) => i === 0 || h - spec.hours[i - 1] === hourGap)) {
    const [first, last] = [spec.hours[0], spec.hours[spec.hours.length - 1]];
    return `every ${hourGap}h, ${clock(first, spec.minutes[0])}–${clock(last, spec.minutes[0])}${onDays}${months}${tag}`;
  }
  // Every few minutes inside a window of hours: "every 15 min, 09:00–17:59, weekdays".
  const minuteStep = stepOf(minutePart) ?? (minutePart === '*' ? 1 : null);
  const hours = spec.hours;
  const contiguous = hours.every((h, i) => i === 0 || h === hours[i - 1] + 1);
  if (minuteStep && contiguous) {
    const cadence = minuteStep === 1 ? 'every minute' : `every ${minuteStep} min`;
    const lastMinute = spec.minutes[spec.minutes.length - 1];
    return `${cadence}, ${clock(hours[0], 0)}–${clock(hours[hours.length - 1], lastMinute)}${onDays}${months}${tag}`;
  }
  return expr;
}
