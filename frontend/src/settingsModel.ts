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
  public_url: string | null;
  notify_webhook_url: string | null;
  notify_events: string[];
  quiet_hours: QuietHours | null;
  retention_days: number | null;
  worker_concurrency: number;
  task_parallelism: number;
  paused: boolean;
  update_check: boolean;
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

export interface UpdateInfo {
  latest: string | null;
  available: boolean | null;
  checked_at: string | null;
  error: string | null;
  enabled: boolean;
}

export interface SystemStatus {
  version: string;
  update: UpdateInfo;
  started_at: string;
  python: string;
  platform: string;
  home: string;
  database: { backend: string; location: string; size_bytes: number | null };
  disk: { total_bytes: number | null; free_bytes: number | null };
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
  retention: { days: number | null };
  now: string;
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

/** The version card's second line. */
export function updateLine(update: UpdateInfo, version: string): Line {
  if (!update.enabled) return { tone: 'default', text: 'Update check is off' };
  if (update.available && update.latest) {
    return { tone: 'warning', text: `${update.latest} available · pip install -U runrail` };
  }
  if (update.error) return { tone: 'warning', text: `Could not check: ${update.error}` };
  if (update.latest) return { tone: 'default', text: update.latest === version || update.available === false ? 'Up to date' : `Latest is ${update.latest}` };
  return { tone: 'default', text: 'Checking for updates…' };
}

/** "2 created · 1 updated" from a restore summary, skipping empty buckets. */
export function restoreSummaryLine(summary: RestoreSummary): string {
  const parts = Object.entries(summary)
    .filter(([, names]) => names.length > 0)
    .map(([bucket, names]) => `${names.length} ${bucket}`);
  return parts.length ? parts.join(' · ') : 'Nothing changed';
}
