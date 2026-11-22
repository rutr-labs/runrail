import { describe, expect, it } from 'vitest';
import {
  RAIL_WINDOWS, bucketize, railAxis, railHorizonLabel, railWeight, railWindow,
  railWindowLabel, untilLabel, type RailRun,
} from './pulseRail';

const MINUTE = 60_000;
const HOUR = 3_600_000;
const NOW = Date.UTC(2026, 10, 22, 18, 40);

/** `count` runs spread evenly over the `span` before NOW. */
function runsOver(span: number, count: number): RailRun[] {
  return Array.from({ length: count }, (_, i) => ({
    id: i + 1, workflow_id: 1, status: 'success',
    created_at: new Date(NOW - (span * (i + 0.5)) / count).toISOString(),
  }));
}

describe('railWindow', () => {
  it('shows a whole day when the entire history is a handful of runs', () => {
    expect(railWindow(runsOver(20 * HOUR, 6), NOW, true)).toBe(24 * HOUR);
  });

  it('shows a whole day when nothing has run yet', () => {
    expect(railWindow([], NOW, true)).toBe(24 * HOUR);
  });

  it('never reaches back past the oldest run it was given', () => {
    // 100 loaded runs cover 3.3 hours: the 6-hour window would draw 2.7 empty
    // hours in which runs really happened.
    const runs = runsOver(3.3 * HOUR, 40);
    expect(railWindow(runs, NOW, false)).toBe(3 * HOUR);
  });

  it('narrows to the last hour on a busy install', () => {
    expect(railWindow(runsOver(3.3 * HOUR, 100), NOW, false)).toBe(HOUR);
  });

  it('always answers one of the known windows', () => {
    for (const count of [0, 1, 10, 55, 100]) {
      expect(RAIL_WINDOWS).toContain(railWindow(runsOver(30 * HOUR, count), NOW, true));
    }
  });

  it('holds its scale while the count hovers at the threshold', () => {
    // 52 runs in three hours: above the 48 needed to widen, below the 72 that
    // forces a narrowing — so the answer is whatever it already was.
    const runs = runsOver(3 * HOUR, 52);
    expect(railWindow(runs, NOW, true)).toBe(HOUR);
    expect(railWindow(runs, NOW, true, HOUR)).toBe(HOUR);
    expect(railWindow(runs, NOW, true, 3 * HOUR)).toBe(3 * HOUR);
  });

  it('ignores runs stamped in the future', () => {
    const runs: RailRun[] = [{
      id: 1, workflow_id: 1, status: 'queued',
      created_at: new Date(NOW + 5 * MINUTE).toISOString(),
    }];
    expect(railWindow(runs, NOW, false)).toBe(24 * HOUR);
  });
});

describe('railAxis', () => {
  it('puts marks on round clock times', () => {
    const marks = railAxis(NOW - HOUR, NOW, 15 * MINUTE, 0);
    expect(marks.map(t => new Date(t).toISOString().slice(11, 16)))
      .toEqual(['17:45', '18:00', '18:15', '18:30']);
  });

  it('aligns long steps to the viewer’s midnight, not UTC’s', () => {
    // UTC+5:30 reports an offset of -330 minutes; a four-hour step must land
    // on 00:00, 04:00, … local, which is :30 past in UTC.
    const marks = railAxis(NOW - 12 * HOUR, NOW, 4 * HOUR, -330);
    for (const t of marks) {
      const local = new Date(t + 330 * MINUTE);
      expect(local.getUTCMinutes()).toBe(0);
      expect(local.getUTCHours() % 4).toBe(0);
    }
    expect(marks.length).toBe(3);
  });

  it('answers nothing for an empty range', () => {
    expect(railAxis(NOW, NOW - 1, HOUR, 0)).toEqual([]);
  });
});

describe('bucketize', () => {
  it('keeps runs that started together in one column', () => {
    const at = [0, 10, 59, 60, 61, 200].map(s => s * 1000);
    const columns = bucketize(at, t => t, MINUTE);
    expect([...columns.keys()]).toEqual([0, 1, 3]);
    expect(columns.get(0)).toHaveLength(3);
    expect(columns.get(1)).toHaveLength(2);
  });

  it('gives a run the same column whatever "now" is', () => {
    const run = NOW - 7 * MINUTE;
    const key = [...bucketize([run], t => t, 48_000).keys()][0];
    expect(key).toBe(Math.floor(run / 48_000));
  });
});

describe('labels', () => {
  it('names the window', () => {
    expect(railWindowLabel(HOUR)).toBe('Last hour');
    expect(railWindowLabel(6 * HOUR)).toBe('Last 6 hours');
  });

  it('names the horizon', () => {
    expect(railHorizonLabel(30 * MINUTE)).toBe('Next 30 min');
    expect(railHorizonLabel(90 * MINUTE)).toBe('Next 90 min');
    expect(railHorizonLabel(12 * HOUR)).toBe('Next 12 hours');
  });

  it('says how far off a fire is', () => {
    expect(untilLabel(20_000)).toBe('in under a minute');
    expect(untilLabel(12 * MINUTE)).toBe('in 12m');
    expect(untilLabel(2 * HOUR)).toBe('in 2h');
    expect(untilLabel(185 * MINUTE)).toBe('in 3h 5m');
  });
});

describe('railWeight', () => {
  it('stacks what needs a person closest to the rail', () => {
    const order = ['success', 'failed', 'queued', 'waiting_approval', 'running']
      .sort((a, b) => railWeight(a) - railWeight(b));
    expect(order).toEqual(['failed', 'waiting_approval', 'running', 'queued', 'success']);
  });
});
