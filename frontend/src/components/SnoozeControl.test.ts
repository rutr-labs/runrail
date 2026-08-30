// @vitest-environment jsdom
import { describe, expect, test } from 'vitest';
import { formatRemaining } from './SnoozeControl';

/* jsdom only to satisfy an import — see the note in ScheduleGaps.test.ts.
   SnoozeControl reaches comet.ts through ./ui. */

/* ─── Snooze countdown ─────────────────────────────────────
   A live countdown re-rendered every second, so the shape of the string is what
   an operator watches: coarse while the mute has hours left, second-by-second
   in the last minute. */

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe('formatRemaining', () => {
  test('the unit boundaries', () => {
    expect(formatRemaining(0)).toBe('0s');
    expect(formatRemaining(SECOND)).toBe('1s');
    expect(formatRemaining(59 * SECOND)).toBe('59s');
    expect(formatRemaining(MINUTE)).toBe('1m 00s');
    expect(formatRemaining(HOUR - SECOND)).toBe('59m 59s');
    expect(formatRemaining(HOUR)).toBe('1h 0m');
    expect(formatRemaining(DAY - SECOND)).toBe('23h 59m');
    expect(formatRemaining(DAY)).toBe('1d 0h');
    expect(formatRemaining(30 * DAY)).toBe('30d 0h'); // SnoozeIn's ceiling
  });

  test('the seconds are padded so the countdown does not jump width', () => {
    // '4m 9s' → '4m 10s' would shift the whole badge every ten seconds.
    expect(formatRemaining(4 * MINUTE + 9 * SECOND)).toBe('4m 09s');
    expect(formatRemaining(4 * MINUTE + 10 * SECOND)).toBe('4m 10s');
    expect(formatRemaining(MINUTE + 0)).toBe('1m 00s');
  });

  test('an elapsed or negative remainder floors at zero', () => {
    // The countdown reaches the end between ticks; it must read '0s', never a
    // negative span, until the component re-reads `snoozed`.
    expect(formatRemaining(-1)).toBe('0s');
    expect(formatRemaining(-5 * MINUTE)).toBe('0s');
    expect(formatRemaining(499)).toBe('0s');
  });

  test('sub-second remainders round to the nearest second', () => {
    expect(formatRemaining(500)).toBe('1s');
    expect(formatRemaining(1499)).toBe('1s');
    expect(formatRemaining(1500)).toBe('2s');
  });

  test('each unit is dropped only once the one above it is empty', () => {
    expect(formatRemaining(2 * DAY + 3 * HOUR + 40 * MINUTE)).toBe('2d 3h');
    expect(formatRemaining(3 * HOUR + 20 * MINUTE + 15 * SECOND)).toBe('3h 20m');
    expect(formatRemaining(20 * MINUTE + 15 * SECOND)).toBe('20m 15s');
  });
});
