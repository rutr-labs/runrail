import { describe, expect, test } from 'vitest';
import { byAttention, handovers, serviceCounts, type Service } from './serviceStatus';

/* What the dashboard and the wallboard say about services: who needs a person
   first, and who has a shared resource now and who is next for it. */

const svc = (id: number, name: string, state: string, extra: Partial<Service> = {}): Service => ({
  id, key: String(id), name, description: null, command: 'x', cwd: null, project_id: null,
  environment_id: null, env_vars_json: null, start_mode: 'startup', windows: [], timezone: 'UTC',
  restart_policy: 'always', stop_timeout_seconds: 30, lock_resource: null, lock_mode: 'exclusive',
  notify_webhook_url: null, override_running: null, override_until: null, override_reason: null,
  state, state_detail: null, state_since: null, next_attempt_at: null, should_run: false,
  cpu_percent: null, rss_bytes: null, resources_at: null,
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
    expect(gpu.holder?.name).toBe('llama-day');
    expect(gpu.upcoming?.name).toBe('llama-night');
  });

  test('a service waiting for the resource is who is next', () => {
    const holder = svc(1, 'big', 'stopping', { lock_resource: 'gpu' });
    const waiting = svc(2, 'small', 'waiting_lock', { lock_resource: 'gpu' });
    expect(handovers([holder, waiting])[0]).toMatchObject({
      holder: { name: 'big' }, waiting: { name: 'small' } });
  });

  test('resources nobody holds or expects are left out', () => {
    expect(handovers([svc(1, 'idle', 'stopped', { lock_resource: 'disk', start_mode: 'manual' })])).toEqual([]);
  });
});
