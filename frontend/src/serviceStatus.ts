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
  cpu_percent: number | null;  // per core, as top reports it
  rss_bytes: number | null;
  process_count: number | null;
  thread_count: number | null;
  resources_at: string | null;
  cpu_count: number;
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

/** Who has each locked resource now, and who is next for it. `holders` are
 *  services only — several when the lock is shared; a resource a workflow
 *  holds has none, and shows up as a waiting service's reason. */
export function handovers(items: Service[]) {
  const resources = [...new Set(items.map(s => s.lock_resource).filter((r): r is string => !!r))].sort();
  return resources.map(resource => {
    const users = items.filter(s => s.lock_resource === resource);
    const holders = users.filter(isLive).sort((a, b) => a.name.localeCompare(b.name));
    const waiting = users.find(s => s.state === 'waiting_lock') ?? null;
    const upcoming = users
      .filter(s => !holders.includes(s) && s.start_mode === 'schedule' && !s.should_run && s.next_change)
      .sort((a, b) => new Date(a.next_change!).getTime() - new Date(b.next_change!).getTime())[0] ?? null;
    return { resource, holders, waiting, upcoming };
  }).filter(h => h.holders.length || h.waiting || h.upcoming);
}

export type Handover = ReturnType<typeof handovers>[number];

/** "a + b", or "in use" when something we cannot name (a workflow run) has it
 *  — a service is waiting, so it is certainly not free. */
export function heldBy(h: Handover): string {
  if (h.holders.length) return h.holders.map(s => s.name).join(' + ');
  return h.waiting ? 'in use' : 'free';
}

/** The line while a service is stopping. The supervisor writes "Asked to stop"
 *  once and "Force-stopping" once the kill went out; the seconds in between
 *  are ours to count, from when the stopping state began. */
export function stoppingDetail(s: Service, now: number): string {
  if (s.state_detail === 'Force-stopping') return s.state_detail;
  if (!s.state_since) return s.state_detail || 'Stopping';
  const left = Math.ceil(s.stop_timeout_seconds - (now - new Date(s.state_since).getTime()) / 1000);
  return left > 0 ? `Force stop in ${left}s` : 'Force-stopping';
}

/** The CPU figure as people read it: the machine's share, with the cores it
 *  amounts to. The supervisor stores it per core (200 = two cores flat out),
 *  which is what top shows and what confuses everyone. */
export function cpuLabel(s: Service): string {
  if (s.cpu_percent == null) return '';
  const cores = s.cpu_percent / 100;
  const share = Math.round(cores / Math.max(1, s.cpu_count) * 100);
  return `${share}% CPU (${cores < 10 ? cores.toFixed(1) : Math.round(cores)} ${cores === 1 ? 'core' : 'cores'})`;
}

/** The tree's size, for the detail page: a worker pool that did not come up
 *  shows here before it shows anywhere else. */
export function treeLabel(s: Service): string {
  if (s.process_count == null || s.thread_count == null) return '';
  return `${s.process_count} ${s.process_count === 1 ? 'process' : 'processes'} · `
    + `${s.thread_count} ${s.thread_count === 1 ? 'thread' : 'threads'}`;
}

/** Dropping a Start/Stop means going back to the schedule only when there is one. */
export const resumeLabel = (s: Service) => s.start_mode === 'schedule' ? 'Resume schedule' : 'Resume';

