import { describe, expect, test } from 'vitest';
import {
  EVENT_LABELS, eventLabel, formatCountdown, formatSize, formatUptime, lockedNote, parseCount, parseDays,
  pausedBanner, provenance, restoreSummaryLine, retentionSentence, sameValue, updateLine,
  type RetentionPreview, type Setting,
} from './settingsModel';

/* ─── Settings page model ──────────────────────────────────
   The sentences an operator reads on the Settings page, pinned at the places
   a refactor would change them: the thresholds of the uptime label, the
   singular/plural of "would delete", what a locked field says, what the
   paused banner counts. The page itself is React; everything it decides
   lives here so it can be read without a browser. */

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

describe('formatUptime', () => {
  const now = Date.parse('2026-10-17T12:00:00Z');
  const up = (ms: number) => formatUptime(new Date(now - ms).toISOString(), now);

  test('the largest two units, never more', () => {
    expect(up(5_000)).toBe('5s');
    expect(up(MINUTE)).toBe('1m');
    expect(up(90 * MINUTE)).toBe('1h 30m');
    expect(up(26 * HOUR)).toBe('1d 2h');
    expect(up(3 * DAY + 5 * HOUR + 7 * MINUTE)).toBe('3d 5h');
  });

  test('a clock that is behind the server reads as just started', () => {
    expect(up(-5_000)).toBe('0s');
    expect(formatUptime(null, now)).toBe('—');
  });
});

describe('formatCountdown', () => {
  const now = Date.parse('2026-10-17T12:00:00Z');
  const until = (ms: number) => formatCountdown(new Date(now + ms).toISOString(), now);

  test('how long until a scheduled fire', () => {
    expect(until(30_000)).toBe('in 30s');
    expect(until(12 * MINUTE)).toBe('in 12m');
    expect(until(2 * HOUR + 5 * MINUTE)).toBe('in 2h 5m');
    expect(until(3 * DAY)).toBe('in 3d 0h');
  });
  test('a fire that is already due says so rather than counting backwards', () => {
    expect(until(-5_000)).toBe('due now');
    expect(formatCountdown(null, now)).toBe('—');
  });
});

describe('formatSize', () => {
  test('climbs to GB and TB for disks', () => {
    expect(formatSize(0)).toBe('0 B');
    expect(formatSize(900)).toBe('900 B');
    expect(formatSize(2048)).toBe('2.0 KB');
    expect(formatSize(5 * 1024 ** 2)).toBe('5.0 MB');
    expect(formatSize(2.3 * 1024 ** 3)).toBe('2.3 GB');
    expect(formatSize(1.5 * 1024 ** 4)).toBe('1.5 TB');
  });
  test('an unmeasured size is a dash, not NaN', () => {
    expect(formatSize(null)).toBe('—');
    expect(formatSize(undefined)).toBe('—');
  });
});

describe('retentionSentence', () => {
  const preview = (runs: number, bytes: number, kept: number): RetentionPreview => ({
    days: 30, cutoff: '2026-09-17T12:00:00Z', runs, files: runs * 2, bytes,
    runs_total: runs + kept, runs_kept: kept,
  });

  test('says what goes and what stays, with thousands separators', () => {
    expect(retentionSentence(preview(41_212, 2.3 * 1024 ** 3, 18_370)))
      .toBe('Would delete 41,212 runs and free 2.3 GB; 18,370 runs kept');
  });
  test('one run is singular', () => {
    expect(retentionSentence(preview(1, 512, 1))).toBe('Would delete 1 run and free 512 B; 1 run kept');
  });
  test('a count that ran out of time is a lower bound and says so', () => {
    expect(retentionSentence({ ...preview(41_212, 2.3 * 1024 ** 3, 18_370), partial: true }))
      .toBe('Would delete at least 41,212 runs and free at least 2.3 GB; at most 18,370 runs kept (the count stopped at its time budget)');
  });
  test('nothing to delete is said plainly', () => {
    expect(retentionSentence(preview(0, 0, 18_370))).toBe('Nothing to delete right now; 18,370 runs kept');
  });
});

describe('parseCount / parseDays', () => {
  test('a whole number inside the range, or the reason it is not', () => {
    expect(parseCount('6', 1, 64)).toEqual({ ok: true, value: 6 });
    expect(parseCount(' 64 ', 1, 64)).toEqual({ ok: true, value: 64 });
    expect(parseCount('0', 1, 64)).toEqual({ ok: false, reason: 'must be a whole number from 1 to 64' });
    expect(parseCount('65', 1, 64)).toEqual({ ok: false, reason: 'must be a whole number from 1 to 64' });
    expect(parseCount('4.5', 1, 64)).toEqual({ ok: false, reason: 'must be a whole number from 1 to 64' });
    expect(parseCount('', 1, 64)).toEqual({ ok: false, reason: 'must be a whole number from 1 to 64' });
  });
  test('retention: empty means never; zero, negatives and fractions are refused', () => {
    const refused = { ok: false, reason: 'must be a whole number of days from 1, or empty for never' };
    expect(parseDays('')).toEqual({ ok: true, value: null });
    expect(parseDays('  ')).toEqual({ ok: true, value: null });
    expect(parseDays('1')).toEqual({ ok: true, value: 1 });
    expect(parseDays('30')).toEqual({ ok: true, value: 30 });
    expect(parseDays('0')).toEqual(refused);
    expect(parseDays('-1')).toEqual(refused);
    expect(parseDays('1.5')).toEqual(refused);
  });
});

describe('lockedNote and provenance', () => {
  const now = Date.parse('2026-10-17T12:00:00Z');
  const base: Setting = {
    key: 'worker_concurrency', title: 'Concurrent runs', help: '', value: 4, default: 4,
    source: 'default', locked: false, env_var: 'RUNRAIL_WORKER_CONCURRENCY', updated_at: null,
  };

  test('a pinned setting names the variable and where to change it', () => {
    expect(lockedNote({ ...base, locked: true, source: 'environment' }))
      .toBe('Set by RUNRAIL_WORKER_CONCURRENCY in the environment — change it there');
    expect(lockedNote(base)).toBeNull();
  });

  test('where the value comes from, in a few words', () => {
    expect(provenance(base, now)).toBe('default');
    expect(provenance({ ...base, source: 'saved', updated_at: new Date(now - 3 * MINUTE).toISOString() }, now))
      .toBe('saved · 3m ago');
    expect(provenance({ ...base, source: 'saved', updated_at: null }, now)).toBe('saved');
    expect(provenance({ ...base, source: 'environment', locked: true }, now)).toBe('environment');
  });
});

describe('eventLabel', () => {
  test('every kind the server can send has a sentence', () => {
    const kinds = ['run_failed', 'run_recovered', 'workflow_paused', 'approval_requested',
      'approval_rejected', 'run_missed', 'run_missed_recovered', 'sla_breached',
      'sla_finished_late', 'service_crash_loop'];
    for (const kind of kinds) expect(EVENT_LABELS[kind], kind).toBeTruthy();
    expect(eventLabel('run_failed')).toBe('A run failed');
    expect(eventLabel('service_crash_loop')).toBe('A service is in a crash loop');
  });
  test('a kind added later still reads as words', () => {
    expect(eventLabel('disk_nearly_full')).toBe('disk nearly full');
  });
});

describe('pausedBanner', () => {
  test('counts what is still draining', () => {
    expect(pausedBanner(2)).toBe('Paused · 2 runs still finishing · queued runs and services wait');
    expect(pausedBanner(1)).toBe('Paused · 1 run still finishing · queued runs and services wait');
    expect(pausedBanner(0)).toBe('Paused · nothing is running · queued runs and services wait');
  });
});

describe('updateLine', () => {
  const update = (over: Partial<Parameters<typeof updateLine>[0]>) => updateLine({
    latest: null, available: null, checked_at: null, error: null, enabled: true, ...over }, '0.7.1');

  test('a newer release says how to get it', () => {
    expect(update({ latest: '0.8.0', available: true })).toEqual({ tone: 'warning', text: '0.8.0 available · pip install -U runrail' });
  });
  test('up to date, could not check, switched off, still asking', () => {
    expect(update({ latest: '0.7.1', available: false })).toEqual({ tone: 'default', text: 'Up to date' });
    expect(update({ error: 'URLError: no route' })).toEqual({ tone: 'warning', text: 'Could not check: URLError: no route' });
    expect(update({ enabled: false })).toEqual({ tone: 'default', text: 'Update check is off' });
    expect(update({})).toEqual({ tone: 'default', text: 'Checking for updates…' });
  });
});

describe('restoreSummaryLine', () => {
  test('lists only the buckets that had something in them', () => {
    expect(restoreSummaryLine({ created: ['a', 'b'], updated: ['c'], skipped: [] })).toBe('2 created · 1 updated');
    expect(restoreSummaryLine({ created: [], updated: [] })).toBe('Nothing changed');
  });
});

describe('sameValue', () => {
  test('compares by structure, so a reordered list of events is not a change', () => {
    expect(sameValue({ start: '22:00', end: '07:00', timezone: 'UTC' }, { start: '22:00', end: '07:00', timezone: 'UTC' })).toBe(true);
    expect(sameValue(['a', 'b'], ['b', 'a'])).toBe(true);
    expect(sameValue(['a'], ['a', 'b'])).toBe(false);
    expect(sameValue(null, '')).toBe(false);
    expect(sameValue(4, 4)).toBe(true);
  });
});
