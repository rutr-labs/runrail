import { describe, expect, test } from 'vitest';
import { LaneSpan, packLane } from './timeline';

/* ─── Timeline lanes ────────────────────────────────────────
   The failure this replaces: on a failed run, a task that failed in a tenth
   of a second and retried at once had its attempts drawn on top of each
   other, because each bar's minimum width ran over the next attempt, and
   bars near the end of the axis were all clamped to the same spot. */

const MIN = 1.2;
const GAP = 0.25;

function expectSeparate(bars: LaneSpan[]) {
  for (const bar of bars) {
    expect(bar.left).toBeGreaterThanOrEqual(0);
    expect(bar.left + bar.width).toBeLessThanOrEqual(100 + 1e-9);
  }
  for (let i = 1; i < bars.length; i++) {
    expect(bars[i].left).toBeGreaterThanOrEqual(bars[i - 1].left + bars[i - 1].width + GAP - 1e-9);
  }
}

describe('packLane', () => {
  test('bars with room keep their true place and only grow to the minimum', () => {
    const bars = packLane([{ left: 10, width: 0.3 }, { left: 40, width: 20 }], MIN, GAP);
    expect(bars).toEqual([{ left: 10, width: MIN }, { left: 40, width: 20 }]);
  });

  test('quick retries no longer cover each other', () => {
    // Two 0.14s attempts 0.15s apart at the end of a 20s run.
    const bars = packLane([{ left: 94.58, width: 0.68 }, { left: 95.29, width: 0.67 }], MIN, GAP);
    expectSeparate(bars);
    expect(bars[0].left).toBeCloseTo(94.58);
  });

  test('attempts at the very end of the axis stay inside it, in order', () => {
    const bars = packLane([{ left: 98.4, width: 0.7 }, { left: 99.1, width: 0.7 }, { left: 99.8, width: 0.1 }], MIN, GAP);
    expectSeparate(bars);
    expect(bars[2].left + bars[2].width).toBeCloseTo(100);
  });

  test('attempts that started the same millisecond still get a bar each', () => {
    const bars = packLane([{ left: 3, width: 0 }, { left: 3, width: 0 }, { left: 3, width: 0 }], MIN, GAP);
    expectSeparate(bars);
    expect(bars.every(b => b.width === MIN)).toBe(true);
  });

  test('more attempts than fit at the minimum share the track evenly', () => {
    const bars = packLane(Array.from({ length: 120 }, (_, i) => ({ left: 50 + i * 0.01, width: 0 })), MIN, GAP);
    expect(bars).toHaveLength(120);
    expectSeparate(bars);
  });

  test('a long bar before a late retry is shortened, not pushed off its start', () => {
    const bars = packLane([{ left: 0.2, width: 99.5 }, { left: 99.75, width: 0.2 }], MIN, GAP);
    expectSeparate(bars);
    expect(bars[0].left).toBeCloseTo(0.2);
  });

  test('an empty lane places nothing', () => {
    expect(packLane([], MIN, GAP)).toEqual([]);
  });
});
