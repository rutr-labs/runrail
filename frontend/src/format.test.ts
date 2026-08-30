import { describe, expect, test } from 'vitest';
import { formatBytes, formatDate, formatDuration, timeAgo } from './format';

/* ─── Shared formatting ────────────────────────────────────
   These four were nineteen private copies until recently, and the reason the
   consolidation was safe is that every copy produced the same string. What is
   pinned here is that string — mostly at the thresholds, because that is where
   a "simplification" changes what an operator reads.

   The viewer's zone and locale come from vitest.config.ts (TZ=UTC) and from the
   runner's ICU default, so the tests below never assert a localised rendering,
   only the parts these helpers actually decide. */

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

describe('timeAgo', () => {
  const now = Date.parse('2026-08-30T12:00:00Z');
  const ago = (ms: number) => timeAgo(new Date(now - ms).toISOString(), now);

  test('the boundaries', () => {
    expect(ago(0)).toBe('just now');
    expect(ago(MINUTE - 1)).toBe('just now');
    expect(ago(MINUTE)).toBe('1m ago');
    expect(ago(HOUR - 1)).toBe('59m ago');
    expect(ago(HOUR)).toBe('1h ago');
    expect(ago(DAY - 1)).toBe('23h ago');
    expect(ago(DAY)).toBe('1d ago');
    expect(ago(400 * DAY)).toBe('400d ago');
  });

  test('it floors rather than rounds', () => {
    // 119 seconds is "1m ago", not "2m ago" — a run that started 1m59s back has
    // not been going for two minutes.
    expect(ago(119_000)).toBe('1m ago');
    expect(ago(2 * HOUR - 1000)).toBe('1h ago');
  });

  test('a future stamp reads as "just now", never as negative time', () => {
    // Clock skew between the server stamp and the browser is routine; "-1m ago"
    // would be alarming and meaningless.
    expect(timeAgo(new Date(now + 30_000).toISOString(), now)).toBe('just now');
    expect(timeAgo(new Date(now + DAY).toISOString(), now)).toBe('just now');
  });

  test('the injected clock is what makes a list agree with itself', () => {
    // Every row in a table is painted from one tick; the same stamp read at two
    // different `now`s is allowed to differ, the same `now` never is.
    const stamp = '2026-08-30T11:00:00Z';
    expect(timeAgo(stamp, now)).toBe('1h ago');
    expect(timeAgo(stamp, now + 2 * HOUR)).toBe('3h ago');
  });

  test('absent values are an em dash', () => {
    expect(timeAgo(null)).toBe('—');
    expect(timeAgo(undefined)).toBe('—');
    expect(timeAgo('')).toBe('—');
  });

  test('an unparseable stamp renders "NaNd ago"', () => {
    // Characterising a real gap, not endorsing it: the `!value` guard catches
    // null and '' but nothing checks that the string parses. Every caller is
    // fed a server-generated ISO stamp today, so this is unreachable with real
    // data — it is one line from being reachable.
    expect(timeAgo('not a date', now)).toBe('NaNd ago');
  });
});

describe('formatDuration', () => {
  test('the sub-second floor', () => {
    expect(formatDuration(0)).toBe('<1s');
    expect(formatDuration(0.4)).toBe('<1s');
    expect(formatDuration(0.999)).toBe('<1s');
    expect(formatDuration(1)).toBe('1.0s');
  });

  test('a decimal below ten seconds, whole seconds above', () => {
    // The decimal is the whole signal at that scale: 1.2s and 8.4s are
    // different answers, 41s and 42s are not.
    expect(formatDuration(1.24)).toBe('1.2s');
    expect(formatDuration(9.4)).toBe('9.4s');
    expect(formatDuration(10)).toBe('10s');
    expect(formatDuration(41.6)).toBe('42s');
  });

  test('minutes and hours', () => {
    expect(formatDuration(60)).toBe('1m 0s');
    expect(formatDuration(90)).toBe('1m 30s');
    expect(formatDuration(3599)).toBe('59m 59s');
    expect(formatDuration(3600)).toBe('1h 0m');
    expect(formatDuration(3660)).toBe('1h 1m');
    expect(formatDuration(86_400)).toBe('24h 0m');
  });

  test('null is an em dash, and zero is not null', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(undefined)).toBe('—');
    expect(formatDuration(0)).toBe('<1s');
  });

  test('rounding at a boundary can print a full unit of the smaller one', () => {
    // Characterising, not endorsing. Each threshold is compared against the raw
    // seconds while the digits come from a rounded value, so the two disagree
    // in the last fraction before a boundary. Cosmetic, and only ever for a
    // duration within a second of the boundary.
    expect(formatDuration(59.6)).toBe('60s');       // one tick short of '1m 0s'
    expect(formatDuration(3599.6)).toBe('59m 60s'); // one tick short of '1h 0m'
    expect(formatDuration(9.96)).toBe('10.0s');     // one tick short of '10s'
  });
});

describe('formatBytes', () => {
  test('the unit boundaries', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1)).toBe('1 B');
    expect(formatBytes(1023)).toBe('1023 B');
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(1024 * 1024 - 1)).toBe('1024.0 KB');
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
  });

  test('there is no gigabyte tier', () => {
    // Characterising: a workflow that writes a 3 GB artifact reads as
    // "3072.0 MB". Deliberate for a log or a report; wrong the day artifacts
    // get big.
    expect(formatBytes(3 * 1024 ** 3)).toBe('3072.0 MB');
  });
});

describe('formatDate', () => {
  test('absent values are an em dash', () => {
    expect(formatDate(null)).toBe('—');
    expect(formatDate(undefined)).toBe('—');
    expect(formatDate('')).toBe('—');
  });

  test('the year is dropped inside the current one and kept outside it', () => {
    // Locale-independent assertion: the only decision this helper makes is
    // whether the year appears at all.
    const thisYear = new Date().getFullYear();
    expect(formatDate(`${thisYear}-06-15T09:30:00Z`)).not.toContain(String(thisYear));
    expect(formatDate(`${thisYear - 3}-06-15T09:30:00Z`)).toContain(String(thisYear - 3));
  });

  test('an unparseable stamp renders "Invalid Date"', () => {
    // Same gap as timeAgo's: guarded against absent, not against malformed.
    expect(formatDate('not a date')).toBe('Invalid Date');
  });
});
