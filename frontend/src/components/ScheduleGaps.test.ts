// @vitest-environment jsdom
import { describe, expect, test } from 'vitest';
import {
  groupMissed, heatmapGapFeed, type ScheduleGapsData,
} from './ScheduleGaps';

/* jsdom only to satisfy an import, not because anything here touches the DOM:
   ScheduleGaps pulls in ./ui, which pulls in comet.ts, which reads
   `window.matchMedia` at module scope behind `typeof window.matchMedia`
   rather than `typeof window` — so the global has to exist before the module
   evaluates. ui.tsx guards it the other way and would have been fine. Fix that
   one line and this file can go back to the node environment. */

/* ─── Missed-fire grouping ─────────────────────────────────
   The one piece of judgement on this panel. It infers the schedule's cadence
   from the window rather than being told it, so the two things that can go
   wrong are collapsing an outage that is really two, and listing one outage as
   a hundred rows. Both are pinned below. */

const HOUR = 3_600_000;

/** Only the fields groupMissed and heatmapGapFeed actually read. */
function gaps(patch: {
  since: string; until: string; expected: number; missed: string[];
  cron?: string | null; stoppedBy?: ScheduleGapsData['stopped_by']; complete?: boolean;
}): ScheduleGapsData {
  return {
    workflow_id: 1,
    schedule_cron: patch.cron === undefined ? '0 * * * *' : patch.cron,
    schedule_timezone: 'UTC',
    window: { since: patch.since, until: patch.until, requested_since: patch.since },
    totals: { expected: patch.expected, ran: 0, missed: patch.missed.length, blocked: 0, paused: 0 },
    daily: [{ date: '2026-08-29', expected: patch.expected, ran: 0, missed: patch.missed.length,
              blocked: 0, paused: 0 }],
    missed: patch.missed.map(expected_at => ({ expected_at, date: expected_at.slice(0, 10) })),
    missed_shown: patch.missed.length,
    paused_spans: [],
    complete: patch.complete ?? true,
    stopped_by: patch.stoppedBy ?? null,
  };
}

/** An hourly schedule over one day: cadence 1h, so the slack threshold is 1h45m. */
const hourly = (missed: string[]) => gaps({
  since: '2026-08-29T00:00:00Z', until: '2026-08-30T00:00:00Z', expected: 24, missed,
});

describe('groupMissed', () => {
  test('no missed fires is no rows', () => {
    expect(groupMissed(hourly([]))).toEqual([]);
  });

  test('adjacent fires collapse into one outage', () => {
    // Newest first, exactly as the response arrives.
    const streaks = groupMissed(hourly([
      '2026-08-29T14:00:00Z', '2026-08-29T13:00:00Z', '2026-08-29T12:00:00Z',
    ]));
    expect(streaks).toHaveLength(1);
    expect(streaks[0].to).toBe('2026-08-29T14:00:00Z');   // newest
    expect(streaks[0].from).toBe('2026-08-29T12:00:00Z'); // oldest
    expect(streaks[0].fires).toHaveLength(3);
  });

  test('a gap wider than the slack splits the outage in two', () => {
    // 1h steps within each run, six hours between them. The whole point: an
    // operator reading this must not think the box was down all afternoon.
    const streaks = groupMissed(hourly([
      '2026-08-29T20:00:00Z', '2026-08-29T19:00:00Z',
      '2026-08-29T13:00:00Z', '2026-08-29T12:00:00Z',
    ]));
    expect(streaks).toHaveLength(2);
    expect(streaks.map(s => [s.from, s.to])).toEqual([
      ['2026-08-29T19:00:00Z', '2026-08-29T20:00:00Z'],
      ['2026-08-29T12:00:00Z', '2026-08-29T13:00:00Z'],
    ]);
  });

  test('exactly at the slack boundary still counts as one outage', () => {
    // Threshold is cadence * 1.75 = 1h45m, and the comparison is <=.
    const within = groupMissed(hourly([
      '2026-08-29T13:45:00Z', '2026-08-29T12:00:00Z',
    ]));
    expect(within).toHaveLength(1);
    const beyond = groupMissed(hourly([
      '2026-08-29T13:46:00Z', '2026-08-29T12:00:00Z',
    ]));
    expect(beyond).toHaveLength(2);
  });

  test('a hundred minute-fires are one row, not a hundred', () => {
    // The case the grouping exists for: a two-hour outage on a minute cron.
    const start = Date.parse('2026-08-29T12:00:00Z');
    const fires = Array.from({ length: 120 }, (_, i) =>
      new Date(start + (119 - i) * 60_000).toISOString().replace('.000Z', 'Z'));
    const data = gaps({
      since: '2026-08-29T00:00:00Z', until: '2026-08-30T00:00:00Z',
      expected: 1440, missed: fires,
    });
    const streaks = groupMissed(data);
    expect(streaks).toHaveLength(1);
    expect(streaks[0].fires).toHaveLength(120);
    // Every timestamp is still there to expand.
    expect(streaks[0].from).toBe('2026-08-29T12:00:00Z');
    expect(streaks[0].to).toBe('2026-08-29T13:59:00Z');
  });

  test('a weekday-only cadence tolerates the weekend it is averaged over', () => {
    // 22 fires spread across 30 days gives a mean well past the real 24h step;
    // that is what the slack is for, and two consecutive weekday misses must
    // still read as one outage.
    const data = gaps({
      since: '2026-08-01T00:00:00Z', until: '2026-08-31T00:00:00Z', expected: 22,
      missed: ['2026-08-20T09:00:00Z', '2026-08-19T09:00:00Z'],
    });
    expect(groupMissed(data)).toHaveLength(1);
  });

  test('with no usable cadence every fire stands alone', () => {
    // A single expected fire, or a degenerate window, is no grounds to guess.
    const single = gaps({
      since: '2026-08-29T00:00:00Z', until: '2026-08-30T00:00:00Z', expected: 1,
      missed: ['2026-08-29T13:00:00Z', '2026-08-29T12:00:00Z'],
    });
    expect(groupMissed(single)).toHaveLength(2);

    const degenerate = gaps({
      since: '2026-08-30T00:00:00Z', until: '2026-08-30T00:00:00Z', expected: 24,
      missed: ['2026-08-29T13:00:00Z', '2026-08-29T12:00:00Z'],
    });
    expect(groupMissed(degenerate)).toHaveLength(2);
  });

  test('the key is stable across polls', () => {
    // It keys React rows; two identical payloads must produce identical keys.
    const fires = ['2026-08-29T14:00:00Z', '2026-08-29T13:00:00Z'];
    expect(groupMissed(hourly(fires)).map(s => s.key))
      .toEqual(groupMissed(hourly(fires)).map(s => s.key));
    expect(groupMissed(hourly(fires))[0].key).toBe('2026-08-29T14:00:00Z');
  });

  test('every fire survives the grouping', () => {
    // Grouping may only change presentation, never drop a timestamp.
    const fires = ['2026-08-29T20:00:00Z', '2026-08-29T19:00:00Z',
                   '2026-08-29T13:00:00Z', '2026-08-29T12:00:00Z', '2026-08-29T03:00:00Z'];
    const flattened = groupMissed(hourly(fires)).flatMap(s => s.fires);
    expect(flattened).toEqual(fires);
  });

  test('a DST hour does not split an outage on a daily schedule', () => {
    // Two consecutive daily fires 25 hours apart across a fall-back; the slack
    // has to absorb it or every autumn produces a spurious second outage row.
    const data = gaps({
      since: '2026-10-25T00:00:00Z', until: '2026-11-08T00:00:00Z', expected: 14,
      missed: ['2026-11-02T06:00:00Z', '2026-11-01T05:00:00Z'],
    });
    expect(groupMissed(data)).toHaveLength(1);
  });
});

describe('heatmapGapFeed', () => {
  const ok = gaps({
    since: '2026-08-29T00:00:00Z', until: '2026-08-30T00:00:00Z', expected: 24, missed: [],
  });

  test('passes the daily buckets through with the scan floor', () => {
    expect(heatmapGapFeed(ok)).toEqual({
      gaps: ok.daily, gapsSince: ok.window.since, gapsComplete: true,
    });
  });

  test('nothing to overlay yields null, never an empty-but-present feed', () => {
    // An unmarked heatmap cell must never be able to mean "checked and clean"
    // when nothing was checked, so these three all withhold the whole feed.
    expect(heatmapGapFeed(null)).toBeNull();
    expect(heatmapGapFeed(gaps({
      since: '2026-08-29T00:00:00Z', until: '2026-08-30T00:00:00Z',
      expected: 0, missed: [], cron: null,
    }))).toBeNull();
    expect(heatmapGapFeed(gaps({
      since: '2026-08-29T00:00:00Z', until: '2026-08-30T00:00:00Z',
      expected: 0, missed: [], stoppedBy: 'invalid_cron',
    }))).toBeNull();
  });

  test('a truncated scan still overlays, flagged incomplete', () => {
    // max_fires and run_rows mean "there is more history than this", not
    // "this history is wrong" — the panel says so, the cells still paint.
    for (const stoppedBy of ['max_fires', 'run_rows'] as const) {
      const feed = heatmapGapFeed(gaps({
        since: '2026-08-29T00:00:00Z', until: '2026-08-30T00:00:00Z',
        expected: 24, missed: [], stoppedBy, complete: false,
      }));
      expect(feed, stoppedBy).not.toBeNull();
      expect(feed!.gapsComplete, stoppedBy).toBe(false);
    }
  });
});
