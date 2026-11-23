import { describe, expect, test } from 'vitest';
import { ChainInfo, ChainUpstream, chainSummary } from './ChainStrip';
import { afterSummary, runsAfter } from './ScheduleBuilder';

/* ─── Chaining, as the pages say it ────────────────────────
   A workflow that runs after others has no schedule to read, so the page has
   to say in words what it is waiting for, and the editor what the choice
   means. */

const upstream = (name: string, patch: Partial<ChainUpstream> = {}): ChainUpstream => ({
  id: name.length, name, enabled: true, last_run_id: 1, last_status: 'success',
  last_finished_at: null, done: false, ...patch,
});
const info = (after: ChainUpstream[], patch: Partial<ChainInfo> = {}): ChainInfo =>
  ({ after, then: [], queued: false, ...patch });

describe('chainSummary', () => {
  test('a workflow with no upstreams says nothing', () => {
    expect(chainSummary(info([]), true)).toBeNull();
  });

  test('it names what it waits for, and why when there is a reason', () => {
    const summary = chainSummary(info([
      upstream('ingest-sales', { done: true }),
      upstream('ingest-crm', { last_status: 'failed' }),
      upstream('ingest-web', { enabled: false }),
      upstream('ingest-app'),
    ]), true);
    expect(summary).toBe('Waiting for ingest-crm (its last run failed), ingest-web (switched off), ingest-app.');
  });

  test('a queued run and a switched-off workflow say so instead', () => {
    expect(chainSummary(info([upstream('a')], { queued: true }), true)).toBe('A run is queued.');
    expect(chainSummary(info([upstream('a')]), false)).toMatch(/Switched off/);
  });
});

describe('afterSummary', () => {
  test('one upstream reads as "each time it succeeds"', () => {
    expect(afterSummary(['ingest'])).toBe('Runs each time ingest succeeds.');
  });

  test('several read as "once all have succeeded"', () => {
    expect(afterSummary(['a', 'b', 'c'])).toBe('Runs once a, b and c have all succeeded since it last started.');
  });

  test('none asks for a pick', () => {
    expect(afterSummary([])).toMatch(/Pick/);
  });
});

describe('runsAfter', () => {
  test('finds what runs after a workflow, directly and through others', () => {
    const flows = [
      { id: 1, after: [] }, { id: 2, after: [1] }, { id: 3, after: [2] },
      { id: 4, after: [] }, { id: 5, after: [4, 3] },
    ];
    expect([...runsAfter(flows, 1)].sort()).toEqual([2, 3, 5]);
    expect([...runsAfter(flows, 4)]).toEqual([5]);
    expect(runsAfter(flows, undefined).size).toBe(0);
  });
});
