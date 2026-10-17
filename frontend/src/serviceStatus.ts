/* ─── Service status ──────────────────────────────────────
   The shapes the API returns for services, and the judgements every
   surface shares — who needs a person first, who holds a resource and who
   is next for it. No imports with side effects, so it is unit-testable. */

import type { ServiceWindow } from './serviceWindows';

export type StartMode = 'startup' | 'schedule' | 'manual';
export type Policy = 'always' | 'on_failure' | 'never';
export type LockMode = 'shared' | 'exclusive';

export interface ServiceSession {
  id: number;
  started_at: string;
  stopped_at: string | null;
  pid: number | null;
  exit_code: number | null;
  stop_reason: string | null;
  command: string | null;
}

export interface Service {
  id: number;
  key: string;
  name: string;
  description: string | null;
  command: string;
  cwd: string | null;
  project_id: number | null;
  environment_id: number | null;
  env_vars_json: Record<string, string> | null;
  start_mode: StartMode;
  windows: ServiceWindow[];
  timezone: string | null;
  restart_policy: Policy;
  stop_timeout_seconds: number;
  lock_resource: string | null;
  lock_mode: LockMode;
  notify_webhook_url: string | null;
  override_running: boolean | null;
  override_until: string | null;
  override_reason: string | null;
  state: string;
  state_detail: string | null;
  state_since: string | null;
  next_attempt_at: string | null;
  should_run: boolean;
  next_change: string | null;
  current_session: ServiceSession | null;
  last_session: ServiceSession | null;
}

export const isLive = (s: Service) => ['running', 'starting', 'stopping'].includes(s.state);

export type Tone = 'down' | 'busy' | 'live' | 'idle';
const TONE: Record<string, Tone> = {
  crash_loop: 'down', backoff: 'busy', waiting_lock: 'busy', stopping: 'busy',
  starting: 'live', running: 'live', stopped: 'idle',
};
const TONE_RANK: Record<Tone, number> = { down: 0, busy: 1, live: 2, idle: 3 };
export const toneOf = (s: Service): Tone => TONE[s.state] ?? 'idle';
export const byAttention = (a: Service, b: Service) =>
  TONE_RANK[toneOf(a)] - TONE_RANK[toneOf(b)] || a.name.localeCompare(b.name);
export const STATE_WORD: Record<string, string> = {
  running: 'Up', starting: 'Starting', stopping: 'Stopping', waiting_lock: 'Waiting',
  backoff: 'Restarting', crash_loop: 'Down', stopped: 'Off',
};

/** The dashboard's line about services, for the hero's status bar. */
export function serviceCounts(items: Service[] | null) {
  const list = items ?? [];
  return {
    total: list.length,
    up: list.filter(s => s.state === 'running').length,
    down: list.filter(s => s.state === 'crash_loop').length,
  };
}

/** Who has each locked resource now, and who is next for it. Services only:
 *  a resource held by a workflow shows up as a waiting service's reason. */
export function handovers(items: Service[]) {
  const resources = [...new Set(items.map(s => s.lock_resource).filter((r): r is string => !!r))].sort();
  return resources.map(resource => {
    const users = items.filter(s => s.lock_resource === resource);
    const holder = users.find(s => ['running', 'starting', 'stopping'].includes(s.state)) ?? null;
    const waiting = users.find(s => s.state === 'waiting_lock') ?? null;
    const upcoming = users
      .filter(s => s !== holder && s.start_mode === 'schedule' && !s.should_run && s.next_change)
      .sort((a, b) => new Date(a.next_change!).getTime() - new Date(b.next_change!).getTime())[0] ?? null;
    return { resource, holder, waiting, upcoming };
  }).filter(h => h.holder || h.waiting || h.upcoming);
}

