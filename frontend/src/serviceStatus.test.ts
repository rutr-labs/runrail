import { describe, expect, test } from 'vitest';
import {
  byAttention, cpuLabel, treeLabel, handovers, heldBy, resumeLabel, serviceCounts, stoppingDetail, type Service,
} from './serviceStatus';

/* What the dashboard and the wallboard say about services: who needs a person
   first, and who has a shared resource now and who is next for it. */

const svc = (id: number, name: string, state: string, extra: Partial<Service> = {}): Service => ({
  id, key: String(id), name, description: null, command: 'x', cwd: null, project_id: null,
  environment_id: null, env_vars_json: null, start_mode: 'startup', windows: [], timezone: 'UTC',
  restart_policy: 'always', stop_timeout_seconds: 30, lock_resource: null, lock_mode: 'exclusive',
  notify_webhook_url: null, override_running: null, override_until: null, override_reason: null,
  state, state_detail: null, state_since: null, next_attempt_at: null, should_run: false,
  cpu_percent: null, rss_bytes: null, process_count: null, thread_count: null, resources_at: null,
  cpu_count: 10,
  next_change: null, current_session: null, last_session: null, ...extra,
});

describe('ordering', () => {
  test('a person is needed first, then what the supervisor is handling, then what is up', () => {
    const list = [svc(1, 'a-off', 'stopped'), svc(2, 'b-up', 'running'), svc(3, 'c-loop', 'crash_loop'),
                  svc(4, 'd-wait', 'waiting_lock'), svc(5, 'e-up', 'running')];
    expect([...list].sort(byAttention).map(s => s.name)).toEqual(['c-loop', 'd-wait', 'b-up', 'e-up', 'a-off']);
    expect(serviceCounts(list)).toEqual({ total: 5, up: 2, down: 1 });
  });
});

describe('handovers', () => {
  test('the holder of a resource, and the scheduled service that takes it next', () => {
    const day = svc(1, 'llama-day', 'running', { lock_resource: 'gpu', start_mode: 'schedule', should_run: true });
    const night = svc(2, 'llama-night', 'stopped', {
      lock_resource: 'gpu', start_mode: 'schedule', next_change: '2026-10-19T22:00:00Z' });
    const later = svc(3, 'llama-weekend', 'stopped', {
      lock_resource: 'gpu', start_mode: 'schedule', next_change: '2026-10-24T08:00:00Z' });
    const [gpu] = handovers([later, day, night]);
    expect(gpu.resource).toBe('gpu');
    expect(gpu.holders.map(s => s.name)).toEqual(['llama-day']);
    expect(gpu.upcoming?.name).toBe('llama-night');
  });

  test('a service waiting for the resource is who is next', () => {
    const holder = svc(1, 'big', 'stopping', { lock_resource: 'gpu' });
    const waiting = svc(2, 'small', 'waiting_lock', { lock_resource: 'gpu' });
    expect(handovers([holder, waiting])[0]).toMatchObject({
      holders: [{ name: 'big' }], waiting: { name: 'small' } });
  });

  test('resources nobody holds or expects are left out', () => {
    expect(handovers([svc(1, 'idle', 'stopped', { lock_resource: 'disk', start_mode: 'manual' })])).toEqual([]);
  });

  test('a shared resource names every service holding it', () => {
    const a = svc(1, 'embed-a', 'running', { lock_resource: 'gpu', lock_mode: 'shared' });
    const b = svc(2, 'embed-b', 'running', { lock_resource: 'gpu', lock_mode: 'shared' });
    const [gpu] = handovers([b, a]);
    expect(gpu.holders.map(s => s.name)).toEqual(['embed-a', 'embed-b']);
    expect(heldBy(gpu)).toBe('embed-a + embed-b');
  });

  test('a resource a workflow holds is not called free while a service waits for it', () => {
    const waiting = svc(2, 'small', 'waiting_lock', {
      lock_resource: 'gpu', state_detail: 'Waiting for gpu · held by workflow nightly-train' });
    const [gpu] = handovers([waiting]);
    expect(gpu.holders).toEqual([]);
    expect(gpu.waiting?.name).toBe('small');
    expect(heldBy(gpu)).toBe('in use');
  });

  test('a resource with no holder and nobody waiting is free', () => {
    const later = svc(3, 'c', 'stopped', { lock_resource: 'gpu', start_mode: 'schedule', next_change: '2026-10-19T22:00:00Z' });
    expect(heldBy(handovers([later])[0])).toBe('free');
  });
});

describe('the stopping countdown', () => {
  const t0 = Date.parse('2026-10-19T12:00:00Z');
  const stopping = svc(1, 'big', 'stopping', { state_since: '2026-10-19T12:00:00Z', stop_timeout_seconds: 30 });

  test('counts down from when the stop began, without the server saying so every second', () => {
    expect(stoppingDetail(stopping, t0)).toBe('Force stop in 30s');
    expect(stoppingDetail(stopping, t0 + 12_400)).toBe('Force stop in 18s');
    expect(stoppingDetail(stopping, t0 + 30_000)).toBe('Force-stopping');
    expect(stoppingDetail(stopping, t0 + 90_000)).toBe('Force-stopping');
  });

  test('the server saying it has already force-stopped wins over the arithmetic', () => {
    expect(stoppingDetail({ ...stopping, state_detail: 'Force-stopping' }, t0 + 1000)).toBe('Force-stopping');
  });

  test('without a start time, the stored detail is all there is', () => {
    expect(stoppingDetail({ ...stopping, state_since: null, state_detail: 'Stopping' }, t0)).toBe('Stopping');
    expect(stoppingDetail({ ...stopping, state_since: null }, t0)).toBe('Stopping');
  });
});

describe('resume wording', () => {
  test('only a scheduled service has a schedule to resume', () => {
    expect(resumeLabel(svc(1, 'a', 'stopped', { start_mode: 'schedule' }))).toBe('Resume schedule');
    expect(resumeLabel(svc(2, 'b', 'stopped', { start_mode: 'startup' }))).toBe('Resume');
    expect(resumeLabel(svc(3, 'c', 'running', { start_mode: 'manual' }))).toBe('Resume');
  });
});

describe('resource figures', () => {
  test('CPU is shown as the machine share, with the cores it amounts to', () => {
    expect(cpuLabel(svc(1, 'a', 'running', { cpu_percent: 197, cpu_count: 10 }))).toBe('20% CPU (2.0 cores)');
    expect(cpuLabel(svc(1, 'a', 'running', { cpu_percent: 100, cpu_count: 4 }))).toBe('25% CPU (1.0 core)');
    expect(cpuLabel(svc(1, 'a', 'running', { cpu_percent: 0.4, cpu_count: 10 }))).toBe('0% CPU (0.0 cores)');
    expect(cpuLabel(svc(1, 'a', 'running'))).toBe('');
  });
  test('the tree is counted in processes and threads', () => {
    expect(treeLabel(svc(1, 'a', 'running', { process_count: 3, thread_count: 7 }))).toBe('3 processes · 7 threads');
    expect(treeLabel(svc(1, 'a', 'running', { process_count: 1, thread_count: 1 }))).toBe('1 process · 1 thread');
    expect(treeLabel(svc(1, 'a', 'running'))).toBe('');
  });
});
