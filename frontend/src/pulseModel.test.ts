import { describe, expect, it } from 'vitest';
import { failingFlows, moodOf, newestFirst, signalLabel, unjudgedFailures, verdictLine } from './pulseModel';

const MINUTE = 60_000;
const HOUR = 3_600_000;
const NOW = Date.UTC(2026, 10, 22, 18, 40);

const flows = [{ id: 1, name: 'ingest' }, { id: 2, name: 'report' }, { id: 3, name: 'sync' }];

/** Runs are served newest first; `ago` is minutes before NOW. */
const run = (id: number, workflow_id: number, status: string, ago: number) => ({
  id, workflow_id, status,
  created_at: new Date(NOW - ago * MINUTE).toISOString(),
  finished_at: status === 'success' || status === 'failed' ? new Date(NOW - ago * MINUTE).toISOString() : null,
});

describe('failingFlows', () => {
  it('names a workflow whose newest verdict is a failure', () => {
    const failing = failingFlows([run(3, 1, 'failed', 5), run(2, 1, 'success', 20)], flows, NOW);
    expect(failing.map(f => f.flow.name)).toEqual(['ingest']);
    expect(failing[0].run.id).toBe(3);
  });

  it('forgives a failure that a later run recovered from', () => {
    expect(failingFlows([run(3, 1, 'success', 5), run(2, 1, 'failed', 20)], flows, NOW)).toEqual([]);
  });

  it('looks past runs that have no verdict yet', () => {
    const runs = [run(4, 1, 'running', 1), run(3, 1, 'cancelled', 3), run(2, 1, 'failed', 20)];
    expect(failingFlows(runs, flows, NOW).map(f => f.run.id)).toEqual([2]);
  });

  it('judges each workflow on its own newest run', () => {
    const runs = [run(5, 2, 'failed', 2), run(4, 1, 'success', 4), run(3, 3, 'failed', 6), run(2, 2, 'success', 30)];
    expect(failingFlows(runs, flows, NOW).map(f => f.flow.name)).toEqual(['report', 'sync']);
  });

  it('lets an old failure become history', () => {
    expect(failingFlows([run(1, 1, 'failed', 25 * 60)], flows, NOW)).toEqual([]);
    expect(failingFlows([run(1, 1, 'failed', 25 * 60)], flows, NOW, 48 * HOUR)).toHaveLength(1);
  });

  it('ignores runs of a workflow that no longer exists', () => {
    expect(failingFlows([run(1, 99, 'failed', 5)], flows, NOW)).toEqual([]);
  });
});

describe('moodOf', () => {
  it('ranks a service down over a wait over a failure', () => {
    expect(moodOf({ down: 1, waiting: 2, failing: 3 })).toBe('down');
    expect(moodOf({ down: 0, waiting: 2, failing: 3 })).toBe('waiting');
    expect(moodOf({ down: 0, waiting: 0, failing: 3 })).toBe('failing');
    expect(moodOf({ down: 0, waiting: 0, failing: 0 })).toBe('calm');
  });
});

describe('the words', () => {
  it('speaks in the singular for one', () => {
    expect(verdictLine({ down: 1, waiting: 0, failing: 0 })).toBe('A service is down.');
    expect(verdictLine({ down: 0, waiting: 1, failing: 0 })).toBe('A run is waiting for you.');
    expect(verdictLine({ down: 0, waiting: 0, failing: 1 })).toBe('A workflow is failing.');
  });

  it('counts when there are several', () => {
    expect(verdictLine({ down: 0, waiting: 4, failing: 1 })).toBe('4 runs are waiting for you.');
    expect(verdictLine({ down: 0, waiting: 0, failing: 3 })).toBe('3 workflows are failing.');
  });

  it('says so when nothing is wrong', () => {
    expect(verdictLine({ down: 0, waiting: 0, failing: 0 })).toBe('Everything is on track.');
    expect(signalLabel({ down: 0, waiting: 0, failing: 0 })).toBe('All clear');
  });

  it('keeps the dock label to the leading problem', () => {
    expect(signalLabel({ down: 2, waiting: 4, failing: 1 })).toBe('2 down');
    expect(signalLabel({ down: 0, waiting: 4, failing: 1 })).toBe('4 waiting');
  });
});

describe('a failure the newest page cannot see', () => {
  it('asks about a workflow that failed recently and has not run since', () => {
    const recent = [run(90, 2, 'success', 1), run(89, 3, 'running', 2)];
    const failed = [run(40, 1, 'failed', 120), run(12, 2, 'failed', 300)];
    // Workflow 1 is absent from the page; workflow 2 has a verdict there.
    expect(unjudgedFailures(recent, failed, NOW)).toEqual([1]);
  });

  it('does not count a run still in flight as a verdict', () => {
    const recent = [run(90, 1, 'running', 1)];
    expect(unjudgedFailures(recent, [run(40, 1, 'failed', 120)], NOW)).toEqual([1]);
  });

  it('lets old failures go, and caps how many it asks about', () => {
    expect(unjudgedFailures([], [run(1, 1, 'failed', 25 * 60)], NOW)).toEqual([]);
    const many = Array.from({ length: 30 }, (_, i) => run(i + 1, i + 1, 'failed', 10));
    expect(unjudgedFailures([], many, NOW)).toHaveLength(12);
  });

  it('judges on the merged evidence', () => {
    const recent = [run(90, 2, 'success', 1)];
    const older = [run(41, 1, 'cancelled', 100), run(40, 1, 'failed', 120), run(39, 1, 'success', 200)];
    const failing = failingFlows(newestFirst(recent, older), flows, NOW);
    expect(failing.map(f => f.flow.name)).toEqual(['ingest']);
  });

  it('merges newest first without repeating a run', () => {
    const merged = newestFirst([run(5, 1, 'success', 1), run(3, 1, 'failed', 3)], [run(4, 2, 'success', 2), run(3, 1, 'failed', 3)]);
    expect(merged.map(r => r.id)).toEqual([5, 4, 3]);
  });
});
