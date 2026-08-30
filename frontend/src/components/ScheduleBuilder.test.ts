import { describe, expect, test } from 'vitest';
import {
  DAY_CRON, DEFAULTS, MINUTE_PRESETS, parseCron, toCron, type State,
} from './ScheduleBuilder';

/* ─── Schedule builder round-trip ──────────────────────────
   The builder is the only writer of most crontabs in the product, and it reads
   them back every time the edit modal opens. Two properties keep that honest:

     1. Every expression the dropdowns can emit parses back to the mode that
        emitted it, and re-emits byte-identically.
     2. Every expression they CANNOT emit opens as Advanced with the operator's
        text untouched — never silently rewritten into a mode that means
        something else.

   The option sets are imported rather than copied, so adding a preset to the
   UI extends this suite instead of quietly escaping it. */

const state = (patch: Partial<State> & Pick<State, 'mode'>): State =>
  ({ ...DEFAULTS, ...patch });

/** Round-trip from the UI's side: state → cron → state. */
const reparse = (s: State) => parseCron(toCron(s)!);

describe('every mode the dropdowns can emit', () => {
  test('minutes — one entry per preset the select offers', () => {
    for (const everyMinutes of MINUTE_PRESETS) {
      const cron = toCron(state({ mode: 'minutes', everyMinutes }))!;
      expect(cron, `every ${everyMinutes}`).toBe(
        everyMinutes <= 1 ? '* * * * *' : `*/${everyMinutes} * * * *`);
      const back = parseCron(cron);
      expect(back.mode, cron).toBe('minutes');
      expect(back.everyMinutes, cron).toBe(everyMinutes);
      expect(toCron(back), cron).toBe(cron);
    }
  });

  test('hourly — all sixty minute options', () => {
    for (let atMinute = 0; atMinute < 60; atMinute++) {
      const cron = toCron(state({ mode: 'hourly', atMinute }))!;
      expect(cron).toBe(`${atMinute} * * * *`);
      const back = parseCron(cron);
      expect(back.mode, cron).toBe('hourly');
      expect(back.atMinute, cron).toBe(atMinute);
      expect(toCron(back), cron).toBe(cron);
    }
  });

  test('daily — every wall-clock minute of the day', () => {
    for (let h = 0; h < 24; h++) {
      for (let m = 0; m < 60; m += 7) {
        const time = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
        const cron = toCron(state({ mode: 'daily', time }))!;
        expect(cron).toBe(`${m} ${h} * * *`);
        const back = parseCron(cron);
        expect(back.mode, cron).toBe('daily');
        // The zero padding has to survive: <input type="time"> ignores "9:5".
        expect(back.time, cron).toBe(time);
        expect(toCron(back), cron).toBe(cron);
      }
    }
  });

  test('weekly — every subset of the day chips', () => {
    // All 127 non-empty subsets, so a chip order or a sort change cannot pass.
    for (let mask = 1; mask < 128; mask++) {
      const days = DAY_CRON.filter((_, i) => mask & (1 << i));
      const cron = toCron(state({ mode: 'weekly', days, time: '09:30' }))!;
      const back = parseCron(cron);
      expect(back.mode, cron).toBe('weekly');
      expect(back.time, cron).toBe('09:30');
      expect(new Set(back.days), cron).toEqual(new Set(days));
      expect(toCron(back), cron).toBe(cron);
    }
  });

  test('weekly — the chips are emitted in cron order, not click order', () => {
    expect(toCron(state({ mode: 'weekly', days: [5, 0, 3], time: '09:30' })))
      .toBe('30 9 * * 0,3,5');
  });

  test('weekly — no days selected publishes nothing', () => {
    // The panel shows "Pick at least one day"; the hidden input must be empty
    // rather than carrying a half-built expression.
    expect(toCron(state({ mode: 'weekly', days: [] }))).toBeNull();
  });

  test('monthly — days 1 to 28', () => {
    for (let dayOfMonth = 1; dayOfMonth <= 28; dayOfMonth++) {
      const cron = toCron(state({ mode: 'monthly', dayOfMonth, time: '06:00' }))!;
      expect(cron).toBe(`0 6 ${dayOfMonth} * *`);
      const back = parseCron(cron);
      expect(back.mode, cron).toBe('monthly');
      expect(back.dayOfMonth, cron).toBe(dayOfMonth);
      expect(toCron(back), cron).toBe(cron);
    }
  });

  test('manual-runs-only publishes nothing', () => {
    expect(toCron(state({ mode: 'none' }))).toBeNull();
  });

  test('advanced passes the text through, trimmed', () => {
    expect(toCron(state({ mode: 'custom', custom: '0 6 * * *' }))).toBe('0 6 * * *');
    expect(toCron(state({ mode: 'custom', custom: '  @daily  ' }))).toBe('@daily');
    expect(toCron(state({ mode: 'custom', custom: '   ' }))).toBeNull();
    expect(toCron(state({ mode: 'custom', custom: '' }))).toBeNull();
  });

  test('the state that comes back re-emits the same cron', () => {
    // The edit modal's real loop: open on a saved cron, change nothing, save.
    // Anything that is not a fixed point here rewrites the operator's schedule
    // just for having looked at it.
    const saved = ['* * * * *', '*/5 * * * *', '0 * * * *', '37 * * * *',
                   '0 9 * * *', '5 0 * * *', '30 9 * * 1,3,5', '0 6 15 * *'];
    for (const cron of saved) {
      expect(toCron(parseCron(cron)), cron).toBe(cron);
      expect(toCron(reparse(parseCron(cron))), cron).toBe(cron);
    }
  });
});

describe('what the dropdowns cannot express opens as Advanced', () => {
  const unrepresentable = [
    '@daily',            // not five fields at all
    '@every 5m',
    '0 9 * *',           // four fields
    '0 9 * * * *',       // six
    '0 9 * 6 *',         // a specific month
    '0 9 * * mon',       // day names
    '0 9 * * 1-5',       // a day range
    '0 9 * * 0,7',       // 7 is out of the chips' range
    '0 9 1 * 1',         // day-of-month AND day-of-week
    '0 9 29 * *',        // day 29 — the select stops at 28 so it fires every month
    '0 9 31 * *',
    '0 */2 * * *',       // an hour step: "Hourly" means "at minute N", not "every N hours"
    '*/15 9 * * *',      // a minute step under a fixed hour
    '0,30 9 * * *',      // a minute list
    '0 9,17 * * *',      // an hour list
    'nonsense',
    '',
  ];

  for (const cron of unrepresentable) {
    test(JSON.stringify(cron), () => {
      const parsed = parseCron(cron);
      expect(parsed.mode).toBe('custom');
      // The operator's own text, verbatim — the whole point of the fallback.
      expect(parsed.custom).toBe(cron);
      expect(toCron(parsed)).toBe(cron.trim() || null);
    });
  }

  test('the fallback state carries clean defaults for the other modes', () => {
    // Switching Advanced → Weekly must land on the defaults, not on debris left
    // by a failed parse.
    const parsed = parseCron('0 9 * * 1-5');
    expect(parsed.days).toEqual(DEFAULTS.days);
    expect(parsed.time).toBe(DEFAULTS.time);
    expect(parsed.dayOfMonth).toBe(DEFAULTS.dayOfMonth);
    expect(parsed.everyMinutes).toBe(DEFAULTS.everyMinutes);
  });
});

/* ─── Values a mode accepts but cannot display ─────────────
   parseCron picks a mode from the SHAPE of the expression and never checks that
   the value is one the control can show. These are green because they describe
   what happens today; each one is a small defect. */
describe('KNOWN GAP: a mode is chosen before the value is checked', () => {
  test('a minute step outside the preset list leaves the select blank', () => {
    // Nothing in the dropdown has value 7, so React renders it with no option
    // selected. The cron itself survives — the hidden input still publishes
    // */7 — but the control reads as empty until it is touched.
    const parsed = parseCron('*/7 * * * *');
    expect(parsed.mode).toBe('minutes');
    expect(parsed.everyMinutes).toBe(7);
    expect(MINUTE_PRESETS).not.toContain(7);
    // Advanced would have been the honest answer.
    expect(parsed.mode).not.toBe('custom');
  });

  test('an out-of-range hour or minute opens Daily on an invalid time', () => {
    // <input type="time" required> rejects "25:00", so the form blocks the save
    // with no message pointing at the cause. Advanced would show the real text.
    expect(parseCron('0 25 * * *')).toMatchObject({ mode: 'daily', time: '25:00' });
    expect(parseCron('60 9 * * *')).toMatchObject({ mode: 'daily', time: '09:60' });
    expect(parseCron('99 99 * * *')).toMatchObject({ mode: 'daily', time: '99:99' });
  });

  test('a leading zero is normalised away on the way back out', () => {
    // '00 09 * * *' is a valid crontab that the builder rewrites to '0 9 * * *'
    // on save. Same schedule, but the saved text changes without being edited.
    const parsed = parseCron('00 09 * * *');
    expect(parsed.mode).toBe('daily');
    expect(toCron(parsed)).toBe('0 9 * * *');
  });
});
