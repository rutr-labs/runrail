/* ─── Timezone-aware cron helpers ─────────────────────────
   Schedules evaluate as wall-clock in the workflow's IANA timezone
   (UTC when unset), matching the server's APScheduler semantics:
   a spring-forward gap skips the firing, a fall-back repeat fires
   once. Occurrences are computed by probing real instants through
   Intl (no offset tables shipped), so DST is exact for any zone.

   Supported grammar mirrors what the ScheduleBuilder emits plus the
   pre-existing fast paths: minute/hour accept N, *, and *​/N; day-of-
   month accepts N or *; month must be *; day-of-week accepts N, a
   comma list, or *. Anything else returns null and callers fall back
   to showing the raw expression. */

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

const fieldValid = (part: string): boolean =>
  part === '*' || stepOf(part) != null || part.split(',').every(p => /^\d+$/.test(p));

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

/* Wall-clock → instant, by refining an offset guess through the real
   zone rules. Returns null when the wall time does not exist (DST gap),
   which callers treat as "skip this day" — same as the server. */
function zonedTimeToUtc(
  y: number, mo: number, d: number, h: number, mi: number, tz: string,
): Date | null {
  const target = Date.UTC(y, mo - 1, d, h, mi);
  let guess = target;
  for (let i = 0; i < 3; i++) {
    const wall = wallParts(new Date(guess), tz);
    const wallAsUtc = Date.UTC(wall.y, wall.mo - 1, wall.d, wall.h, wall.mi);
    if (wallAsUtc === target) return new Date(guess);
    guess += target - wallAsUtc;
  }
  const wall = wallParts(new Date(guess), tz);
  return wall.h === h && wall.mi === mi ? new Date(guess) : null;
}

/* The wallboard asks every second for every workflow; cron resolves at
   minute granularity, so memoize on (expr, tz, minute-of-after). */
const occCache = new Map<string, Date | null>();

export function nextCronOccurrence(
  expr: string, tz?: string | null, after: Date = new Date(),
): Date | null {
  const zone = tz || 'UTC';
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minP, hourP, domP, monP, dowP] = parts;
  if (monP !== '*') return null;
  if (![minP, hourP, dowP].every(fieldValid) || !(domP === '*' || /^\d+$/.test(domP))) return null;
  /* dom+dow both restricted uses cron's OR semantics — out of scope here. */
  if (domP !== '*' && dowP !== '*') return null;

  const bucket = Math.floor(after.getTime() / 60_000);
  const key = `${expr}|${zone}|${bucket}`;
  if (occCache.has(key)) return occCache.get(key)!;
  if (occCache.size > 512) occCache.clear();

  const result = compute(minP, hourP, domP, dowP, zone, after);
  occCache.set(key, result);
  return result;
}

function compute(
  minP: string, hourP: string, domP: string, dowP: string, zone: string, after: Date,
): Date | null {
  const subDaily = hourP === '*' || stepOf(hourP) != null;

  if (subDaily) {
    /* Minute walk, bounded to 26h — covers every sub-daily pattern the
       builder can emit, across any offset including :30/:45 zones. */
    const cursor = new Date(after);
    cursor.setSeconds(0, 0);
    for (let i = 0; i < 26 * 60; i++) {
      cursor.setMinutes(cursor.getMinutes() + 1);
      const w = wallParts(cursor, zone);
      if (fieldMatch(minP, w.mi) && fieldMatch(hourP, w.h) &&
          fieldMatch(domP, w.d) && fieldMatch(dowP, w.dow)) {
        return new Date(cursor);
      }
    }
    return null;
  }

  const hour = Number(hourP);
  const minute = minP === '*' ? 0 : Number(minP);
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
    if (!fieldMatch(domP, w.d) || !fieldMatch(dowP, w.dow)) continue;
    const instant = zonedTimeToUtc(w.y, w.mo, w.d, hour, minute, zone);
    if (instant && instant > after) return instant;
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

export const viewerZone = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone;

/** Human label for a cron. Fixed-time schedules read as wall-clock in the
 *  WORKFLOW's zone (that is what the author chose), tagged with the offset
 *  when it differs from the viewer's zone; sub-hourly cadences are zoneless. */
export function cronLabel(expr: string, tz?: string | null): string {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return expr;
  const [minutePart, hourPart, domPart, monthPart, dowPart] = parts;
  if (monthPart !== '*') return expr;

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
  const days = dowPart.split(',').map(Number).filter(n => !Number.isNaN(n));
  const names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const dayLabel = days.length === 1
    ? `every ${names[days[0]] ?? dowPart}`
    : days.map(d => names[d] ?? String(d)).join('/');
  return `${dayLabel} at ${time}${tag}`;
}
