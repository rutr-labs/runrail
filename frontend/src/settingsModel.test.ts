import { describe, expect, test } from 'vitest';
import {
  settledSentence,
  cleanupLine,
  basename, groupEvents, quietSpans, storageSegments, systemHeadline,
  EVENT_LABELS, eventLabel, formatCountdown, formatSize, formatUptime, lockedNote, parseCount, parseDays,
  pausedBanner, provenance, restoreSummaryLine, retentionSentence, sameValue,
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

describe('presentation helpers', () => {
  test('a path chip shows only the last segment', () => {
    expect(basename('/private/tmp/rr-demo/runrail.db')).toBe('runrail.db');
    expect(basename('/private/tmp/rr-demo/')).toBe('rr-demo');
    expect(basename('C:\\Users\\me\\RunRail')).toBe('RunRail');
    expect(basename('db.internal:5432/runrail')).toBe('runrail');
  });

  test('the storage bar shares one, and waits for every figure', () => {
    const base = { measured_at: null, measuring: false };
    const bar = storageSegments({ ...base, logs_bytes: 300, artifacts_bytes: 100, environments_bytes: 0, backups_bytes: 600 });
    expect(bar?.total).toBe(1000);
    expect(bar?.segments.map(s => [s.key, s.share])).toEqual([
      ['logs', 0.3], ['artifacts', 0.1], ['environments', 0], ['backups', 0.6]]);
    expect(storageSegments({ ...base, logs_bytes: 1, artifacts_bytes: null, environments_bytes: 0, backups_bytes: 0 })).toBeNull();
    expect(storageSegments({ ...base, logs_bytes: 0, artifacts_bytes: 0, environments_bytes: 0, backups_bytes: 0 })
      ?.segments.every(s => s.share === 0)).toBe(true);
  });

  test('quiet hours become one span, or two across midnight', () => {
    expect(quietSpans('09:00', '17:00')).toEqual([[0.375, 17 / 24]]);
    expect(quietSpans('22:00', '06:00')).toEqual([[22 / 24, 1], [0, 0.25]]);
    expect(quietSpans('22:00', '22:00')).toEqual([]);
    expect(quietSpans('9:00', '17:00')).toEqual([]);
  });

  test('alert kinds are grouped, and an unknown kind is kept under Other', () => {
    expect(groupEvents(['run_failed', 'sla_breached', 'new_kind']).map(g => [g.title, g.kinds])).toEqual([
      ['Runs', ['run_failed']], ['Deadlines', ['sla_breached']], ['Other', ['new_kind']]]);
  });

  test('the headline says running, paused, or unsupervised', () => {
    const status = {
      worker: { running: 2, queued: 1, waiting_approval: 0, paused: false, concurrency: 4, task_parallelism: 4 },
      services: { supervised: true, total: 3, running: 3 },
    } as unknown as Parameters<typeof systemHeadline>[0];
    expect(systemHeadline(status)).toEqual({ tone: 'ok', title: 'Running', detail: '2 running · 1 queued' });
    expect(systemHeadline({ ...status, worker: { ...status.worker, paused: true, waiting_approval: 4 } }))
      .toEqual({ tone: 'paused', title: 'Paused', detail: '2 running · 1 queued · 4 waiting approval' });
    expect(systemHeadline({ ...status, services: { supervised: false, total: 3, running: 0 } }).tone).toBe('warning');
    expect(systemHeadline({ ...status, services: { supervised: false, total: 0, running: 0 } }).tone).toBe('ok');
  });
});

describe('cleanupLine', () => {
  const at = Date.parse('2026-10-18T12:00:00Z');
  const base = { running: false, days: 30, started_at: '2026-10-18T11:58:00Z', finished_at: '2026-10-18T11:59:00Z',
                 runs_deleted: 23800, files_deleted: 47600, errors: 0 };
  test('says what is happening while it runs, and what it did after', () => {
    expect(cleanupLine({ ...base, running: true, finished_at: null, runs_deleted: 4200 }, at))
      .toEqual({ tone: 'busy', text: 'Deleting runs older than 30 days… 4,200 runs so far' });
    expect(cleanupLine(base, at)).toEqual({ tone: 'done', text: 'Deleted 23,800 runs and 47,600 files' });
    expect(cleanupLine({ ...base, runs_deleted: 0, files_deleted: 0 }, at)?.text).toBe('Cleaned up: nothing was old enough to delete');
    expect(cleanupLine({ ...base, errors: 2 }, at)?.tone).toBe('warning');
  });
  test('stays quiet when none ran, or the last one is old news', () => {
    expect(cleanupLine(undefined, at)).toBeNull();
    expect(cleanupLine({ ...base, started_at: null }, at)).toBeNull();
    expect(cleanupLine({ ...base, finished_at: '2026-10-18T11:00:00Z' }, at)).toBeNull();
  });
});

describe('settledSentence', () => {
  const base = { days: 90, cutoff: '', files: 0, bytes: 0, runs_total: 100, runs_kept: 99 };
  test('a saved figure reads as up to date, with what the next pass takes', () => {
    expect(settledSentence({ ...base, runs: 0, runs_kept: 100 })).toBe('Up to date; 100 runs kept');
    expect(settledSentence({ ...base, runs: 1 })).toBe(
      'Up to date; 99 runs kept. 1 run has aged past 90 days since the last pass, and the next one, within six hours, removes it');
    expect(settledSentence({ ...base, runs: 3, runs_kept: 97 })).toContain('3 runs have aged past 90 days');
  });
});
