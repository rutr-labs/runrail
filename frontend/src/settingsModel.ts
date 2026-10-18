import { timeAgo } from './format';

/* ─── Settings page model ──────────────────────────────────
   What the Settings page reads from /api/settings and /api/system/*, and the
   sentences it makes of them. Nothing here touches React or the network, so
   the wording an operator sees — "would delete 41,212 runs", "set by
   RUNRAIL_X in the environment" — is pinned by plain unit tests. */

/* ── Shapes, as the server sends them ─────────────────── */

export type SettingSource = 'environment' | 'saved' | 'default';

export interface QuietHours { start: string; end: string; timezone: string }

/** Every setting's value type, keyed as runtime_settings.DEFINITIONS names them. */
export interface SettingValues {
  notify_webhook_url: string | null;
  notify_events: string[];
  quiet_hours: QuietHours | null;
  retention_days: number | null;
  worker_concurrency: number;
  task_parallelism: number;
  paused: boolean;
}
export type SettingKey = keyof SettingValues;
export type SettingValue = SettingValues[SettingKey];

export interface Setting {
  key: string;
  title: string;
  help: string;
  value: SettingValue;
  default: SettingValue;
  source: SettingSource;
  locked: boolean;
  env_var: string | null;
  updated_at: string | null;
}

export interface SettingsResponse { settings: Setting[]; event_kinds: string[] }

export interface SystemStatus {
  version: string;
  started_at: string;
  python: string;
  platform: string;
  home: string;
  database: { backend: string; location: string; size_bytes: number | null };
  storage: {
    logs_bytes: number | null; artifacts_bytes: number | null;
    environments_bytes: number | null; backups_bytes: number | null;
    measured_at: string | null; measuring: boolean;
  };
  worker: {
    concurrency: number; task_parallelism: number;
    running: number; queued: number; waiting_approval: number; paused: boolean;
  };
  services: { supervised: boolean; total: number; running: number };
  scheduler: { next_fire_at: string | null };
  retention: { days: number | null; cleanup: CleanupState; last: LastCleanup | null };
  now: string;
}

/** The retention cleanup the server is running, or last ran. */
export interface CleanupState {
  running: boolean; days: number | null;
  started_at: string | null; finished_at: string | null;
  runs_deleted: number; files_deleted: number; errors: number;
}

/** The newest cleanup on record: survives a restart, unlike CleanupState. */
export interface LastCleanup {
  days: number; started_at: string; finished_at: string;
  runs_deleted: number; files_deleted: number; errors: number;
}

export interface RetentionPreview {
  days: number; cutoff: string; runs: number; files: number; bytes: number;
  runs_total: number; runs_kept: number;
  /** The count stopped at its time budget: every figure is a lower bound. */
  partial?: boolean;
}

export interface RestorePoint { name: string; size_bytes: number; created_at: string }

export interface BackupPreview {
  manifest: {
    created_at?: string; runrail_version?: string; revision?: string | null;
    database?: string; home?: string;
    counts?: Partial<Record<'workflows' | 'services' | 'projects' | 'environments' | 'runs', number>>;
  };
  includes_database: boolean;
  configuration: Record<string, number>;
}

export type RestoreSummary = Record<string, string[]>;

export interface NotifyTestResult { ok: boolean; error: string | null; url_host: string | null }

/* ── Event kinds ──────────────────────────────────────── */

/** The sentence for each kind of alert, in the server's order. */
export const EVENT_LABELS: Record<string, string> = {
  run_failed: 'A run failed',
  run_recovered: 'A workflow recovered',
  workflow_paused: 'A workflow was auto-paused',
  approval_requested: 'Approval requested',
  approval_rejected: 'Approval rejected',
  run_missed: 'A scheduled run was missed',
  run_missed_recovered: 'A missed schedule recovered',
  sla_breached: 'A run breached its deadline',
  sla_finished_late: 'A run finished late',
  service_crash_loop: 'A service is in a crash loop',
};

/** A kind the server added after this build still reads as words. */
export const eventLabel = (kind: string): string => EVENT_LABELS[kind] ?? kind.replace(/_/g, ' ');

/* ── Numbers and times ────────────────────────────────── */

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

/** Sizes up to a disk. format.ts's formatBytes stops at MB, which is right
 *  for a log file and wrong for a volume. */
export function formatSize(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return '—';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) { value /= 1024; unit++; }
  return `${value.toFixed(1)} ${UNITS[unit]}`;
}

/** "3d 5h", "1h 30m", "12m", "45s": the two largest units that are not zero. */
export function formatUptime(startedAt: string | null | undefined, now = Date.now()): string {
  if (!startedAt) return '—';
  const seconds = Math.max(0, Math.floor((now - new Date(startedAt).getTime()) / 1000));
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${seconds}s`;
}

/** "in 12m" until a scheduled fire; "due now" once the moment has passed. */
export function formatCountdown(at: string | null | undefined, now = Date.now()): string {
  if (!at) return '—';
  const target = new Date(at).getTime();
  if (target <= now) return 'due now';
  return `in ${formatUptime(new Date(now).toISOString(), target)}`;
}

const count = (n: number, noun: string) => `${n.toLocaleString('en-US')} ${noun}${n === 1 ? '' : 's'}`;

/** One sentence for the retention preview, before anything is deleted. */
export function retentionSentence(preview: RetentionPreview): string {
  if (preview.partial) {
    return `Would delete at least ${count(preview.runs, 'run')} and free at least ${formatSize(preview.bytes)}; `
      + `at most ${count(preview.runs_kept, 'run')} kept (the count stopped at its time budget)`;
  }
  const kept = `${count(preview.runs_kept, 'run')} kept`;
  if (preview.runs === 0) return `Nothing to delete right now; ${kept}`;
  return `Would delete ${count(preview.runs, 'run')} and free ${formatSize(preview.bytes)}; ${kept}`;
}

/** The paused banner, counting what is still draining. */
export function pausedBanner(running: number): string {
  const draining = running === 0 ? 'nothing is running' : `${count(running, 'run')} still finishing`;
  return `Paused · ${draining} · queued runs and services wait`;
}

/* ── Input parsing ────────────────────────────────────── */

export type Parsed<T> = { ok: true; value: T } | { ok: false; reason: string };

const WHOLE = /^-?\d+$/;

/** A whole number from `low` to `high`, with the same wording the server uses. */
export function parseCount(text: string, low: number, high: number): Parsed<number> {
  const trimmed = text.trim();
  const value = Number(trimmed);
  if (!WHOLE.test(trimmed) || value < low || value > high) {
    return { ok: false, reason: `must be a whole number from ${low} to ${high}` };
  }
  return { ok: true, value };
}

/** Retention days: empty is "never", otherwise a whole number of days from 1.
 *  Zero is refused here as on the server — "never" is the empty value, not 0. */
export function parseDays(text: string): Parsed<number | null> {
  const trimmed = text.trim();
  if (trimmed === '') return { ok: true, value: null };
  const value = Number(trimmed);
  if (!WHOLE.test(trimmed) || value < 1) {
    return { ok: false, reason: 'must be a whole number of days from 1, or empty for never' };
  }
  return { ok: true, value };
}

/* ── Per-field notes ──────────────────────────────────── */

/** What a pinned field says instead of accepting input. */
export function lockedNote(setting: Setting): string | null {
  if (!setting.locked) return null;
  return `Set by ${setting.env_var ?? 'the environment'} in the environment — change it there`;
}

/** "saved · 3m ago", "default", "environment": where a value comes from. */
export function provenance(setting: Setting, now = Date.now()): string {
  if (setting.source !== 'saved') return setting.source;
  return setting.updated_at ? `saved · ${timeAgo(setting.updated_at, now)}` : 'saved';
}

/** Structural equality for setting values, so a Save button knows whether
 *  anything changed. Lists compare as sets: the server returns events in its
 *  own order whatever order they were sent. */
export function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && [...a].sort().every((x, i) => x === [...b].sort()[i]);
  }
  return JSON.stringify(a) === JSON.stringify(b);
}

/* ── Status sentences ─────────────────────────────────── */

export interface Line { tone: 'default' | 'warning'; text: string }

/** "2 created · 1 updated" from a restore summary, skipping empty buckets. */
export function restoreSummaryLine(summary: RestoreSummary): string {
  const parts = Object.entries(summary)
    .filter(([, names]) => names.length > 0)
    .map(([bucket, names]) => `${names.length} ${bucket}`);
  return parts.length ? parts.join(' · ') : 'Nothing changed';
}

/* ── Presentation helpers for the redesigned page ─────── */

/** The last segment of a path, for a chip that shows little and copies all. */
export function basename(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, '');
  const parts = trimmed.split(/[\\/]/);
  return parts[parts.length - 1] || trimmed || path;
}

export type StorageKey = 'logs' | 'artifacts' | 'environments' | 'backups';
export interface StorageSegment { key: StorageKey; label: string; bytes: number; share: number }

/** The storage bar: one segment per folder, in a fixed order, sharing 1.
 *  Null while any figure is still being measured. */
export function storageSegments(storage: SystemStatus['storage']): { total: number; segments: StorageSegment[] } | null {
  const parts: [StorageKey, string, number | null][] = [
    ['logs', 'Logs', storage.logs_bytes], ['artifacts', 'Artifacts', storage.artifacts_bytes],
    ['environments', 'Environments', storage.environments_bytes], ['backups', 'Backups', storage.backups_bytes],
  ];
  if (parts.some(([, , bytes]) => bytes == null)) return null;
  const total = parts.reduce((sum, [, , bytes]) => sum + (bytes ?? 0), 0);
  return {
    total,
    segments: parts.map(([key, label, bytes]) => ({
      key, label, bytes: bytes ?? 0, share: total > 0 ? (bytes ?? 0) / total : 0,
    })),
  };
}

/** Where the quiet span falls on a 24-hour bar, as fractions of the day: one
 *  span, or two when it crosses midnight. Empty for a malformed time. */
export function quietSpans(start: string, end: string): [number, number][] {
  const minutes = (hhmm: string): number | null => {
    const match = /^(\d{2}):(\d{2})$/.exec(hhmm);
    if (!match) return null;
    const value = Number(match[1]) * 60 + Number(match[2]);
    return value < 24 * 60 ? value : null;
  };
  const from = minutes(start);
  const to = minutes(end);
  if (from == null || to == null || from === to) return [];
  const day = 24 * 60;
  return from < to ? [[from / day, to / day]] : [[from / day, 1], [0, to / day]];
}

/** Retention presets offered as one click each; anything else is Custom. */
export const RETENTION_PRESETS: { days: number; label: string }[] = [
  { days: 7, label: '7 days' }, { days: 30, label: '30 days' },
  { days: 90, label: '90 days' }, { days: 365, label: '1 year' },
];

/** The alert kinds, grouped the way a person thinks about them. A kind this
 *  build does not know lands in "Other" rather than vanishing. */
export const EVENT_GROUPS: { title: string; kinds: string[] }[] = [
  { title: 'Runs', kinds: ['run_failed', 'run_recovered', 'workflow_paused'] },
  { title: 'Approvals', kinds: ['approval_requested', 'approval_rejected'] },
  { title: 'Schedules', kinds: ['run_missed', 'run_missed_recovered'] },
  { title: 'Deadlines', kinds: ['sla_breached', 'sla_finished_late'] },
  { title: 'Services', kinds: ['service_crash_loop'] },
];

export function groupEvents(kinds: string[]): { title: string; kinds: string[] }[] {
  const known = new Set(EVENT_GROUPS.flatMap(group => group.kinds));
  const groups = EVENT_GROUPS
    .map(group => ({ title: group.title, kinds: group.kinds.filter(kind => kinds.includes(kind)) }))
    .filter(group => group.kinds.length > 0);
  const other = kinds.filter(kind => !known.has(kind));
  return other.length ? [...groups, { title: 'Other', kinds: other }] : groups;
}

/** The hero's headline: what state the whole system is in, in one look. */
export function systemHeadline(status: SystemStatus): { tone: 'ok' | 'paused' | 'warning'; title: string; detail: string } {
  const { worker, services } = status;
  const queue = [
    `${worker.running.toLocaleString('en-US')} running`,
    `${worker.queued.toLocaleString('en-US')} queued`,
    ...(worker.waiting_approval ? [`${worker.waiting_approval.toLocaleString('en-US')} waiting approval`] : []),
  ].join(' · ');
  if (worker.paused) return { tone: 'paused', title: 'Paused', detail: queue };
  if (services.total > 0 && !services.supervised) {
    return { tone: 'warning', title: 'Running · services unsupervised', detail: queue };
  }
  return { tone: 'ok', title: 'Running', detail: queue };
}

/** What the cleanup is doing, or did lately, in one line; null when there is
 *  nothing worth saying (none ran, or the last one is old news). */
export function cleanupLine(cleanup: CleanupState | undefined, now = Date.now()): { tone: 'busy' | 'done' | 'warning'; text: string } | null {
  if (!cleanup || !cleanup.started_at) return null;
  const runs = `${cleanup.runs_deleted.toLocaleString('en-US')} run${cleanup.runs_deleted === 1 ? '' : 's'}`;
  if (cleanup.running) {
    return { tone: 'busy', text: `Deleting runs older than ${cleanup.days} days… ${runs} so far` };
  }
  const finished = cleanup.finished_at ? new Date(cleanup.finished_at).getTime() : 0;
  if (now - finished > 15 * 60_000) return null;
  const files = `${cleanup.files_deleted.toLocaleString('en-US')} file${cleanup.files_deleted === 1 ? '' : 's'}`;
  if (cleanup.errors > 0) {
    return { tone: 'warning', text: `Deleted ${runs} and ${files}; ${cleanup.errors} could not be removed and will be retried` };
  }
  return { tone: 'done', text: cleanup.runs_deleted === 0 ? 'Cleaned up: nothing was old enough to delete'
    : `Deleted ${runs} and ${files}` };
}

/** The preview for the figure already saved: not a warning about what a
 *  choice would do, but what is waiting for the next six-hourly pass — runs
 *  that have aged past the line since the last one. */
export function settledSentence(preview: RetentionPreview): string {
  const kept = `${count(preview.runs_kept, 'run')} kept`;
  if (preview.runs === 0) return `Up to date; ${kept}`;
  const aged = preview.runs === 1 ? '1 run has' : `${preview.runs.toLocaleString('en-US')} runs have`;
  return `Up to date; ${kept}. ${aged} aged past ${preview.days} days since the last pass, `
    + `and the next one, within six hours, removes ${preview.runs === 1 ? 'it' : 'them'}`;
}

/** The quiet line under Retention: what the newest cleanup on record did. */
export function lastCleanupLine(last: LastCleanup | null | undefined, now = Date.now()): string | null {
  if (!last) return null;
  const when = timeAgo(last.finished_at, now);
  if (last.runs_deleted === 0) return `Last cleanup ${when}: nothing was older than ${last.days} days`;
  const runs = `${last.runs_deleted.toLocaleString('en-US')} run${last.runs_deleted === 1 ? '' : 's'}`;
  return `Last cleanup ${when}: deleted ${runs} older than ${last.days} days`;
}
