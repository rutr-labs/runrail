/* ─── Shared formatting ───────────────────────────────────
   The four helpers every page renders timestamps and sizes with. They lived as
   a private copy per component for a while, each one marked as a drift risk;
   the strings they produced were identical and must stay so — a run's age has
   to read the same in the run table, the log search results and the activity
   feed, because they are often on screen together. */

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

/** Absolute stamp, for a tooltip or a detail row. The year is dropped inside
 *  the current one, which is almost every stamp anyone opens. */
export function formatDate(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
    hour: '2-digit', minute: '2-digit',
  });
}

/** Coarse age, for a table cell. `now` is injectable so a list painted from a
 *  single tick agrees with itself row to row instead of reading the clock once
 *  per cell. */
export function timeAgo(value?: string | null, now = Date.now()): string {
  if (!value) return '—';
  const ms = now - new Date(value).getTime();
  if (ms < MINUTE) return 'just now';
  if (ms < HOUR) return `${Math.floor(ms / MINUTE)}m ago`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}h ago`;
  return `${Math.floor(ms / DAY)}d ago`;
}

/** Elapsed time. Sub-10s keeps a decimal because the difference between 1.2s
 *  and 8.4s is the whole signal at that scale. */
export function formatDuration(seconds?: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

/* ─── Calendar days ────────────────────────────────────────
   Everything is stored in UTC, but "what ran on Tuesday" is a question about
   the viewer's calendar. These two keep the client's idea of a day identical
   to the server's: /stats/daily, /schedule-gaps and /runs?day= all bucket by
   the zone reported here. */

/** The viewer's IANA zone, or UTC where the browser will not say. */
export function viewerZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/** YYYY-MM-DD for a local calendar date.
 *
 *  Not toISOString().slice(0, 10): that formats the UTC date, which for any
 *  zone ahead of UTC names the previous day for most of the evening. */
export function localDateKey(value: Date | number): string {
  const at = typeof value === 'number' ? new Date(value) : value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}
