import { describe, expect, it } from 'vitest';
import { buildLanes, countdown, fires, laneState, runEnd, upcomingFires, type BoardFlow, type BoardRun } from './board';

const MINUTE = 60_000;
const HOUR = 3_600_000;
const NOW = Date.UTC(2026, 10, 22, 18, 40);

const flow = (id: number, name: string, extra: Partial<BoardFlow> = {}): BoardFlow =>
  ({ id, name, enabled: true, ...extra });

const run = (id: number, workflow_id: number, status: string, agoMinutes: number, seconds: number | null = 30): BoardRun => ({
  id, workflow_id, status,
  created_at: new Date(NOW - agoMinutes * MINUTE).toISOString(),
  started_at: new Date(NOW - agoMinutes * MINUTE).toISOString(),
  duration_seconds: seconds,
});

const build = (flows: BoardFlow[], runs: BoardRun[], owed = new Map<number, number[]>(), failing: number[] = [], max = 12) =>
  buildLanes(flows, runs, owed, { start: NOW - HOUR, now: NOW, failing: new Set(failing), max });

describe('fires', () => {
  it('needs a schedule and an enabled workflow', () => {
    expect(fires(flow(1, 'a', { schedule_cron: '0 * * * *' }))).toBe(true);
    expect(fires(flow(1, 'a'))).toBe(false);
    expect(fires(flow(1, 'a', { schedule_cron: '0 * * * *', enabled: false }))).toBe(false);
  });

  it('stops for a mute that pauses runs, but not for one that only silences alerts', () => {
    const cron = { schedule_cron: '0 * * * *', snoozed: true };
    expect(fires(flow(1, 'a', { ...cron, snooze_pauses_runs: true }))).toBe(false);
    expect(fires(flow(1, 'a', { ...cron, snooze_pauses_runs: false }))).toBe(true);
  });
});

describe('upcomingFires', () => {
  it('lists each owed fire inside the horizon, in order', () => {
    const owed = upcomingFires([flow(7, 'hourly', { schedule_cron: '0 * * * *', schedule_timezone: 'UTC' })], NOW, 3 * HOUR);
    expect(owed.get(7)).toEqual([19, 20, 21].map(h => Date.UTC(2026, 10, 22, h, 0)));
  });

  it('does not skip the next minute of an every-minute schedule', () => {
    const owed = upcomingFires([flow(7, 'busy', { schedule_cron: '* * * * *', schedule_timezone: 'UTC' })], NOW, 3 * MINUTE);
    expect(owed.get(7)).toEqual([1, 2, 3].map(m => NOW + m * MINUTE));
  });

  it('leaves out workflows that owe nothing', () => {
    const owed = upcomingFires([flow(1, 'manual'), flow(2, 'off', { schedule_cron: '* * * * *', enabled: false })], NOW, HOUR);
    expect(owed.size).toBe(0);
  });

  it('caps what it reads from one workflow', () => {
    const owed = upcomingFires([flow(7, 'busy', { schedule_cron: '* * * * *', schedule_timezone: 'UTC' })], NOW, HOUR, 4);
    expect(owed.get(7)).toHaveLength(4);
  });
});

describe('runEnd', () => {
  it('uses the recorded finish when there is one', () => {
    const finished = { ...run(1, 1, 'success', 10), finished_at: new Date(NOW - 4 * MINUTE).toISOString() };
    expect(runEnd(finished, NOW)).toBe(NOW - 4 * MINUTE);
  });

  it('falls back to start plus duration', () => {
    expect(runEnd(run(1, 1, 'success', 10, 120), NOW)).toBe(NOW - 8 * MINUTE);
  });

  it('is "now" for a run still occupying its line', () => {
    expect(runEnd(run(1, 1, 'running', 10, null), NOW)).toBe(NOW);
    expect(runEnd(run(1, 1, 'waiting_approval', 90, null), NOW)).toBe(NOW);
  });

  it('gives a queued run no width', () => {
    expect(runEnd(run(1, 1, 'queued', 2, null), NOW)).toBe(NOW - 2 * MINUTE);
  });
});

describe('laneState', () => {
  it('puts a person-shaped wait above everything else', () => {
    expect(laneState(flow(1, 'a'), [run(1, 1, 'waiting_approval', 5, null), run(2, 1, 'running', 1, null)], true)).toBe('waiting');
  });

  it('keeps saying failing while a retry is in flight', () => {
    expect(laneState(flow(1, 'a'), [run(2, 1, 'running', 1, null)], true)).toBe('failing');
  });

  it('distinguishes paused from merely idle', () => {
    expect(laneState(flow(1, 'a', { enabled: false }), [], false)).toBe('paused');
    expect(laneState(flow(1, 'a'), [], false)).toBe('idle');
    expect(laneState(flow(1, 'a'), [run(1, 1, 'success', 5)], false)).toBe('ok');
  });
});

describe('buildLanes', () => {
  it('gives a line only to workflows with something to show', () => {
    const { lanes, quiet } = build(
      [flow(1, 'ran'), flow(2, 'silent'), flow(3, 'due')],
      [run(1, 1, 'success', 10)],
      new Map([[3, [NOW + 5 * MINUTE]]]));
    expect(lanes.map(l => l.flow.name)).toEqual(['due', 'ran']);
    expect(quiet).toBe(1);
  });

  it('keeps a failing workflow on the board even when its failure has scrolled off', () => {
    const { lanes } = build([flow(1, 'broke')], [run(1, 1, 'failed', 300)], new Map(), [1]);
    expect(lanes.map(l => l.state)).toEqual(['failing']);
  });

  it('orders what needs a person first and everything else by name', () => {
    const { lanes } = build(
      [flow(1, 'zeta'), flow(2, 'alpha'), flow(3, 'mid'), flow(4, 'beta-10'), flow(5, 'beta-9')],
      [run(1, 1, 'failed', 5), run(2, 2, 'running', 1, null), run(3, 3, 'waiting_approval', 20, null),
       run(4, 4, 'success', 8), run(5, 5, 'success', 9)],
      new Map(), [1]);
    // Failing outranks waiting here only inside the urgent group's own order;
    // a running line does not jump the alphabet.
    expect(lanes.map(l => l.flow.name)).toEqual(['zeta', 'mid', 'alpha', 'beta-9', 'beta-10']);
  });

  it('spends a short board on the urgent and the busy', () => {
    const flows = [flow(1, 'a-quiet'), flow(2, 'b-busy'), flow(3, 'c-broken'), flow(4, 'd-one')];
    const runs = [
      run(1, 1, 'success', 50),
      run(2, 2, 'success', 40), run(3, 2, 'success', 30), run(4, 2, 'success', 20),
      run(5, 3, 'failed', 45),
      run(6, 4, 'success', 10),
    ];
    const { lanes, quiet } = build(flows, runs, new Map(), [3], 2);
    expect(lanes.map(l => l.flow.name)).toEqual(['c-broken', 'b-busy']);
    expect(quiet).toBe(2);
  });

  it('shows a long run that started before the window and is still going', () => {
    const { lanes } = build([flow(1, 'long')], [run(1, 1, 'running', 180, null)]);
    expect(lanes[0].runs).toHaveLength(1);
  });

  it('drops runs that ended before the window', () => {
    const { lanes } = build([flow(1, 'old')], [run(1, 1, 'success', 180)]);
    expect(lanes).toHaveLength(0);
  });

  it('lays each line out oldest first', () => {
    const { lanes } = build([flow(1, 'a')], [run(3, 1, 'success', 5), run(2, 1, 'success', 15), run(1, 1, 'success', 25)]);
    expect(lanes[0].runs.map(r => r.id)).toEqual([1, 2, 3]);
  });
});

describe('countdown', () => {
  it('keeps its width under an hour', () => {
    expect(countdown(19 * 1000)).toBe('00:19');
    expect(countdown(13 * MINUTE + 27 * 1000)).toBe('13:27');
  });

  it('steps up to hours and days', () => {
    expect(countdown(10 * HOUR + 27 * MINUTE)).toBe('10h 27m');
    expect(countdown(2 * 24 * HOUR + 3 * HOUR)).toBe('2d 3h');
  });

  it('never counts below zero', () => {
    expect(countdown(-5000)).toBe('00:00');
  });
});
