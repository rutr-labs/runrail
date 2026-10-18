import { describe, expect, test } from 'vitest';
import {
  daysLabel, isActive, isKnownZone, nextChange, scheduleSummary, weekSegments, weeklyHours, windowLabel,
  type ServiceWindow,
} from './serviceWindows';

/* The same cases as tests/test_service_windows.py: the editor's preview and
   the supervisor must agree about when a service runs. */

const at = (iso: string) => new Date(`${iso}:00Z`);
const WEEKDAYS: ServiceWindow['days'] = ['mon', 'tue', 'wed', 'thu', 'fri'];

describe('windows', () => {
  test('a weekday daytime window', () => {
    const w: ServiceWindow[] = [{ days: WEEKDAYS, start: '08:00', end: '17:00' }];
    expect(isActive(w, 'UTC', at('2026-10-19T07:59'))).toBe(false);
    expect(isActive(w, 'UTC', at('2026-10-19T08:00'))).toBe(true);
    expect(isActive(w, 'UTC', at('2026-10-19T17:00'))).toBe(false);
    expect(nextChange(w, 'UTC', at('2026-10-19T12:00'))).toEqual(at('2026-10-19T17:00'));
    expect(nextChange(w, 'UTC', at('2026-10-23T18:00'))).toEqual(at('2026-10-26T08:00'));
  });

  test('an overnight window belongs to the day it starts', () => {
    const w: ServiceWindow[] = [{ days: ['fri'], start: '22:00', end: '06:00' }];
    expect(isActive(w, 'UTC', at('2026-10-23T23:00'))).toBe(true);
    expect(isActive(w, 'UTC', at('2026-10-24T05:59'))).toBe(true);
    expect(isActive(w, 'UTC', at('2026-10-24T22:30'))).toBe(false);
  });

  test('touching windows act as one', () => {
    const w: ServiceWindow[] = [
      { days: ['mon'], start: '08:00', end: '12:00' },
      { days: ['mon'], start: '12:00', end: '13:00' },
    ];
    expect(nextChange(w, 'UTC', at('2026-10-19T09:00'))).toEqual(at('2026-10-19T13:00'));
  });

  test('a full week never changes', () => {
    const w: ServiceWindow[] = [{ days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
                                  start: '00:00', end: '00:00' }];
    expect(nextChange(w, 'UTC', at('2026-10-21T03:00'))).toBeNull();
  });

  test('wall clock across DST, and the long night of the autumn change', () => {
    const day: ServiceWindow[] = [{ days: WEEKDAYS, start: '08:00', end: '17:00' }];
    expect(isActive(day, 'America/New_York', at('2026-10-30T12:00'))).toBe(true);
    expect(isActive(day, 'America/New_York', at('2026-11-02T12:30'))).toBe(false);
    expect(isActive(day, 'America/New_York', at('2026-11-02T13:00'))).toBe(true);
    const night: ServiceWindow[] = [{ days: ['sat'], start: '22:00', end: '06:00' }];
    const opened = nextChange(night, 'America/New_York', at('2026-10-31T20:00'))!;
    const closed = nextChange(night, 'America/New_York', opened)!;
    expect((closed.getTime() - opened.getTime()) / 3_600_000).toBe(9);
  });
});

describe('the week view', () => {
  test('an overnight window paints two rows', () => {
    const rows = weekSegments([{ days: ['sun'], start: '22:00', end: '06:00' }]);
    expect(rows[6]).toEqual([{ from: 1320, to: 1440, index: 0 }]);
    expect(rows[0]).toEqual([{ from: 0, to: 360, index: 0 }]); // Monday morning
  });

  test('weekly hours count overlaps once', () => {
    expect(weeklyHours([
      { days: WEEKDAYS, start: '08:00', end: '17:00' },
      { days: WEEKDAYS, start: '16:00', end: '18:00' },
    ])).toBe(50);
  });

  test('labels', () => {
    expect(daysLabel(WEEKDAYS)).toBe('Weekdays');
    expect(daysLabel(['sat', 'sun'])).toBe('Weekends');
    expect(daysLabel(['mon', 'tue', 'wed'])).toBe('Mon–Wed');
    expect(daysLabel(['mon', 'wed', 'fri'])).toBe('Mon, Wed, Fri');
    expect(windowLabel({ days: ['fri'], start: '22:00', end: '06:00' })).toBe('Fri 22:00 → 06:00 next day');
  });
});

describe('zones', () => {
  test('a zone Intl does not know is flagged rather than quietly read as UTC', () => {
    expect(isKnownZone('UTC')).toBe(true);
    expect(isKnownZone('America/New_York')).toBe(true);
    expect(isKnownZone('Mars/Olympus_Mons')).toBe(false);
    expect(isKnownZone('')).toBe(false);
  });

  test('the summary says the zone is unknown instead of showing UTC times', () => {
    const w: ServiceWindow[] = [{ days: WEEKDAYS, start: '08:00', end: '17:00' }];
    expect(scheduleSummary(w, 'Mars/Olympus_Mons', at('2026-10-19T12:00').getTime()))
      .toBe('Unknown timezone "Mars/Olympus_Mons" — fix it to see when this runs.');
  });
});

describe('the schedule in words', () => {
  const w: ServiceWindow[] = [{ days: WEEKDAYS, start: '08:00', end: '17:00' }];
  test('says what the windows do, not what the process is doing', () => {
    expect(scheduleSummary(w, 'UTC', at('2026-10-19T12:00').getTime()))
      .toBe('In a window now · closes Mon 17:00 (in 5h)');
    expect(scheduleSummary(w, 'UTC', at('2026-10-23T18:00').getTime()))
      .toBe('Next window opens Mon 08:00 (in 2d 14h)');
    expect(scheduleSummary([], 'UTC')).toBe('No windows yet — it will never start on its own.');
  });
});
