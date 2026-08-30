import { describe, expect, test } from 'vitest';
import { cronLabel, nextCronOccurrence, zoneTag } from './cron';

/* ─── Cron occurrence engine ───────────────────────────────
   Two jobs here. The first is ordinary regression cover for the zone and DST
   arithmetic. The second matters more: every fire time in APSCHEDULER_TRUTH was
   produced by the backend's own trigger, so a change to either side that makes
   the UI promise a run time the scheduler will not fire shows up as a failing
   assertion instead of as a 2am phone call.

   One semantic difference is deliberate and not a bug: APScheduler's
   get_next_fire_time is at-or-after `now`, nextCronOccurrence is strictly after
   `after`. Every `after` below is off a fire boundary so the two never differ
   for that reason alone. */

const at = (iso: string) => new Date(iso);
const utc = (d: Date | null) => (d ? d.toISOString().replace('.000Z', 'Z') : null);

/* Wall clock in a zone, so an assertion can say what an operator would read on
   the kitchen clock rather than only a UTC instant. */
const wall = (d: Date | null, tz: string) =>
  d
    ? new Intl.DateTimeFormat('en-CA', {
        timeZone: tz, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
      }).format(d).replace(', ', ' ')
    : null;

/** Successive fires, each computed from the previous one — the ScheduleBuilder
 *  preview and the wallboard both walk the chain this way. */
function walk(expr: string, tz: string, after: string, count: number): (string | null)[] {
  const out: (string | null)[] = [];
  let cursor = at(after);
  for (let i = 0; i < count; i++) {
    const next = nextCronOccurrence(expr, tz, cursor);
    out.push(utc(next));
    if (!next) break;
    cursor = next;
  }
  return out;
}

/* ─── Cross-check against the backend scheduler ────────────
   Generated with the repo's own trigger builder on APScheduler 3.11.3:

     runrail.crontab.cron_trigger(expr, tz).get_next_fire_time(prev, cursor)

   walked forward by stepping one second past each fire. cron_trigger, not
   CronTrigger.from_crontab: the day-of-week translation from standard cron's
   0=Sun to APScheduler's 0=Mon lives there, and generating from the raw parser
   would pin this suite to the very off-by-one it exists to catch. Regenerate
   the same way after an APScheduler upgrade; a diff here IS the release note. */
const APSCHEDULER_TRUTH: { expr: string; tz: string; after: string; fires: string[] }[] = [
  // Whole-hour and fractional-hour offsets.
  { expr: '0 9 * * *', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T09:00:00Z', '2026-08-31T09:00:00Z'] },
  { expr: '0 9 * * *', tz: 'Asia/Kolkata', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T03:30:00Z', '2026-08-31T03:30:00Z'] },
  { expr: '0 9 * * *', tz: 'Asia/Kathmandu', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T03:15:00Z', '2026-08-31T03:15:00Z'] },
  { expr: '45 3 * * *', tz: 'Pacific/Chatham', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T15:00:00Z', '2026-08-31T15:00:00Z'] },
  { expr: '0 6 * * *', tz: 'Australia/Adelaide', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T20:30:00Z', '2026-08-31T20:30:00Z'] },

  // Fixed times either side of a spring-forward gap, but not inside it.
  { expr: '30 1 * * *', tz: 'America/New_York', after: '2026-03-07T12:00:00Z',
    fires: ['2026-03-08T06:30:00Z', '2026-03-09T05:30:00Z'] },
  { expr: '30 3 * * *', tz: 'America/New_York', after: '2026-03-07T12:00:00Z',
    fires: ['2026-03-08T07:30:00Z', '2026-03-09T07:30:00Z'] },

  // A minute cadence walks straight through a transition: every wall-clock
  // minute exists on both sides of it, so there is no gap to disagree about.
  { expr: '*/30 * * * *', tz: 'America/New_York', after: '2026-03-08T06:31:00Z',
    fires: ['2026-03-08T07:00:00Z', '2026-03-08T07:30:00Z',
            '2026-03-08T08:00:00Z', '2026-03-08T08:30:00Z'] },

  // Lord Howe moves its clock by THIRTY minutes, the case a whole-hour offset
  // table would silently get wrong.
  { expr: '0 6 * * *', tz: 'Australia/Lord_Howe', after: '2026-10-03T00:00:00Z',
    fires: ['2026-10-03T19:00:00Z', '2026-10-04T19:00:00Z'] },
  { expr: '45 1 * * *', tz: 'Australia/Lord_Howe', after: '2026-10-03T12:00:00Z',
    fires: ['2026-10-03T15:15:00Z', '2026-10-04T14:45:00Z'] },

  // Sub-daily cadences in fractional-offset zones.
  { expr: '*/15 * * * *', tz: 'UTC', after: '2026-08-30T10:07:00Z',
    fires: ['2026-08-30T10:15:00Z', '2026-08-30T10:30:00Z',
            '2026-08-30T10:45:00Z', '2026-08-30T11:00:00Z'] },
  { expr: '*/15 * * * *', tz: 'Asia/Kolkata', after: '2026-08-30T10:07:00Z',
    fires: ['2026-08-30T10:15:00Z', '2026-08-30T10:30:00Z',
            '2026-08-30T10:45:00Z', '2026-08-30T11:00:00Z'] },
  { expr: '7 * * * *', tz: 'Asia/Kolkata', after: '2026-08-30T10:07:00Z',
    fires: ['2026-08-30T10:37:00Z', '2026-08-30T11:37:00Z', '2026-08-30T12:37:00Z'] },
  { expr: '0 */6 * * *', tz: 'Asia/Kolkata', after: '2026-08-30T10:07:00Z',
    fires: ['2026-08-30T12:30:00Z', '2026-08-30T18:30:00Z',
            '2026-08-31T00:30:00Z', '2026-08-31T06:30:00Z'] },
  { expr: '*/5 * * * *', tz: 'Pacific/Chatham', after: '2026-08-30T10:07:00Z',
    fires: ['2026-08-30T10:10:00Z', '2026-08-30T10:15:00Z', '2026-08-30T10:20:00Z'] },

  // Day-of-month, including a month boundary and a southern-hemisphere zone
  // whose offset changes between two consecutive fires.
  { expr: '0 0 15 * *', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-09-15T00:00:00Z', '2026-10-15T00:00:00Z'] },
  { expr: '30 23 1 * *', tz: 'America/Sao_Paulo', after: '2026-01-15T00:00:00Z',
    fires: ['2026-02-02T02:30:00Z', '2026-03-02T02:30:00Z', '2026-04-02T02:30:00Z'] },
  { expr: '0 5 28 * *', tz: 'Asia/Kolkata', after: '2026-02-01T00:00:00Z',
    fires: ['2026-02-27T23:30:00Z', '2026-03-27T23:30:00Z'] },

  // Day of week, the field the two engines number differently. 2026-08-30 is a
  // Sunday, so digit N must land N days later — and 7 must land on the Sunday.
  { expr: '0 9 * * 0', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T09:00:00Z', '2026-09-06T09:00:00Z'] },
  { expr: '0 9 * * 1', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-31T09:00:00Z', '2026-09-07T09:00:00Z'] },
  { expr: '0 9 * * 2', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-09-01T09:00:00Z', '2026-09-08T09:00:00Z'] },
  { expr: '0 9 * * 3', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-09-02T09:00:00Z', '2026-09-09T09:00:00Z'] },
  { expr: '0 9 * * 4', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-09-03T09:00:00Z', '2026-09-10T09:00:00Z'] },
  { expr: '0 9 * * 5', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-09-04T09:00:00Z', '2026-09-11T09:00:00Z'] },
  { expr: '0 9 * * 6', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-09-05T09:00:00Z', '2026-09-12T09:00:00Z'] },
  { expr: '0 9 * * 7', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T09:00:00Z', '2026-09-06T09:00:00Z'] },
  { expr: '30 9 * * 1,3,5', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-31T09:30:00Z', '2026-09-02T09:30:00Z',
            '2026-09-04T09:30:00Z', '2026-09-07T09:30:00Z'] },
  // A step counts from Sunday in both engines, not from Monday in one of them.
  { expr: '0 9 * * */2', tz: 'UTC', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-30T09:00:00Z', '2026-09-01T09:00:00Z',
            '2026-09-03T09:00:00Z', '2026-09-05T09:00:00Z'] },
  { expr: '0 9 * * 1', tz: 'Asia/Kolkata', after: '2026-08-30T00:00:00Z',
    fires: ['2026-08-31T03:30:00Z', '2026-09-07T03:30:00Z'] },
  // A weekly schedule stepping over a fall-back: the wall time holds, the
  // instant moves by an hour.
  { expr: '30 8 * * 1', tz: 'Europe/Berlin', after: '2026-10-20T00:00:00Z',
    fires: ['2026-10-26T07:30:00Z', '2026-11-02T07:30:00Z', '2026-11-09T07:30:00Z'] },

  // A fixed time INSIDE a spring-forward gap. 02:30 does not exist in New York
  // on 2026-03-08, and the run happens anyway: the scheduler resolves it against
  // the pre-jump offset, so it lands at 03:30 EDT that once.
  { expr: '30 2 * * *', tz: 'America/New_York', after: '2026-03-07T12:00:00Z',
    fires: ['2026-03-08T07:30:00Z', '2026-03-09T06:30:00Z', '2026-03-10T06:30:00Z'] },
  // Lord Howe shifts by THIRTY minutes at 02:00, so 02:30 is the first instant
  // on the far side of the gap rather than inside it.
  { expr: '30 2 * * *', tz: 'Australia/Lord_Howe', after: '2026-10-03T12:00:00Z',
    fires: ['2026-10-03T15:30:00Z', '2026-10-04T15:30:00Z'] },
  // An ambiguous fall-back time resolves to the EARLIER of its two instants
  // (02:30 AEDT, not 02:30 AEST). Only the first fire is pinned: what the
  // backend does with the second is a divergence of its own, below.
  { expr: '30 2 * * *', tz: 'Australia/Sydney', after: '2026-04-04T12:00:00Z',
    fires: ['2026-04-04T15:30:00Z'] },
  { expr: '30 1 * * *', tz: 'America/New_York', after: '2026-10-31T12:00:00Z',
    fires: ['2026-11-01T05:30:00Z'] },
];

describe('nextCronOccurrence agrees with the backend scheduler', () => {
  for (const { expr, tz, after, fires } of APSCHEDULER_TRUTH) {
    test(`${expr} @ ${tz} from ${after}`, () => {
      expect(walk(expr, tz, after, fires.length)).toEqual(fires);
    });
  }
});

/* ─── Spring-forward, agreed ───────────────────────────────
   A wall time inside the gap used to be the worst disagreement in the product:
   this engine returned null and skipped the whole day, so the UI promised a run
   23 hours after the one the scheduler actually made. Both now resolve the gap
   against the offset in force before the jump. */
describe('a wall time inside a spring-forward gap', () => {
  test('fires an hour late rather than being skipped', () => {
    // 2026-03-08 in New York: 01:59 EST is followed by 03:00 EDT, so 02:30 never
    // happens on the clock — and the run still happens, at 03:30 EDT.
    const shown = nextCronOccurrence('30 2 * * *', 'America/New_York',
                                     at('2026-03-07T12:00:00Z'));
    expect(utc(shown)).toBe('2026-03-08T07:30:00Z');
    expect(wall(shown, 'America/New_York')).toBe('2026-03-08 03:30');
  });

  test('an ambiguous fall-back time takes the earlier of its two instants', () => {
    // Sydney, 2026-04-05: 02:30 AEDT (15:30Z) then 02:30 AEST (16:30Z). Which
    // one came out used to depend on where the half-day probe landed.
    expect(utc(nextCronOccurrence('30 2 * * *', 'Australia/Sydney',
                                  at('2026-04-04T12:00:00Z')))).toBe('2026-04-04T15:30:00Z');
    expect(utc(nextCronOccurrence('30 1 * * *', 'America/New_York',
                                  at('2026-10-31T12:00:00Z')))).toBe('2026-11-01T05:30:00Z');
  });
});

/* ─── Known divergences ────────────────────────────────────
   These assert what the frontend does TODAY next to what the backend actually
   fires. They are green on purpose: a red suite would be ignored rather than
   acted on. Both that remain are worth an hour once a year, on the two days a
   zone changes its offset, and both would cost a reimplementation of
   APScheduler's own quirks to close — its fall-back walk is not even monotonic.
   When one is fixed, rewrite the test to assert agreement and move the case up
   into APSCHEDULER_TRUTH. */
describe('KNOWN DIVERGENCE from the backend scheduler', () => {
  test('an hour cadence loses the fire that lands in the gap', () => {
    // Not only fixed times: `0 */2` wants local hours 0,2,4… and 02:00 does not
    // exist on 2026-03-08, so the minute walk never matches it. APScheduler
    // still fires, at 07:00Z — 03:00 EDT.
    const chain = walk('0 */2 * * *', 'America/New_York', '2026-03-08T06:01:00Z', 3);
    expect(chain).toEqual(['2026-03-08T08:00:00Z', '2026-03-08T10:00:00Z',
                           '2026-03-08T12:00:00Z']);
    const APSCHEDULER_FIRES_FIRST = '2026-03-08T07:00:00Z';
    expect(chain[0]).not.toBe(APSCHEDULER_FIRES_FIRST);
  });

  test('a fall-back repeat is one fire here, two on the backend', () => {
    // 2026-11-01 in New York: 01:30 happens twice, once EDT and once EST.
    const after = at('2026-10-31T12:00:00Z');
    const chain = walk('30 1 * * *', 'America/New_York', after.toISOString(), 2);
    // First fire agrees.
    expect(chain[0]).toBe('2026-11-01T05:30:00Z');
    // The builder's "Next: X, then Y" preview then jumps to the next day; the
    // backend fires 01:30 EST an hour after 01:30 EDT and only then moves on.
    expect(chain[1]).toBe('2026-11-02T06:30:00Z');
    const APSCHEDULER_SECOND_FIRE = '2026-11-01T06:30:00Z';
    expect(chain[1]).not.toBe(APSCHEDULER_SECOND_FIRE);
  });
});

/* ─── Out of range is out of grammar ───────────────────────
   The backend refuses these outright, so the workflow never runs. A preview
   that reads like an ordinary schedule is the whole reason nobody noticed: the
   contract is now null and the raw text, the same as any other expression this
   engine cannot evaluate. */
describe('an expression the server would refuse gets no preview', () => {
  const after = at('2026-08-30T00:00:00Z');
  const refused = [
    '60 9 * * *',      // rolled over to "daily at 10:00" and never ran
    '0 24 * * *',
    '99 99 * * *',     // read as "daily at 04:39", four days out
    '0 9 * * 8',
    '*/61 * * * *',    // a step wider than its own field, read as "every 61 min"
    '0 */25 * * *',
  ];
  for (const expr of refused) {
    test(expr, () => {
      expect(nextCronOccurrence(expr, 'UTC', after), expr).toBeNull();
      expect(cronLabel(expr, 'UTC'), expr).toBe(expr);
    });
  }
});

/* ─── Malformed and unsupported input ──────────────────────
   The contract is null, never a throw and never a wrong time: callers render
   the raw expression when they get null. */
describe('nextCronOccurrence returns null rather than throwing', () => {
  const after = at('2026-08-30T00:00:00Z');
  const rejected = [
    '',                    // nothing typed yet
    '   ',
    '0 9 * *',             // four fields
    '0 9 * * * *',         // six fields
    '0 9 * 6 *',           // a restricted month is out of grammar
    '0 9 1 * 1',           // dom AND dow — cron's OR semantics, deliberately unsupported
    // The backend runs both; this engine does not read them, so the raw text is
    // shown. That is the safe direction — never a confident wrong time.
    '0 9 * * mon',
    '0 9 * * 1-5',
    'a b c d e',
    '@daily',
    '*/ * * * *',
    '0 -1 * * *',
  ];
  for (const expr of rejected) {
    test(JSON.stringify(expr), () => {
      expect(() => nextCronOccurrence(expr, 'UTC', after)).not.toThrow();
      expect(nextCronOccurrence(expr, 'UTC', after)).toBeNull();
    });
  }

  test('*/0 does not divide by zero or spin', () => {
    // stepOf maps a 0 step to null, so the field is not a step and not a list.
    expect(nextCronOccurrence('*/0 * * * *', 'UTC', after)).toBeNull();
  });

  test('a day-of-month no month has is null, not a guess', () => {
    for (const expr of ['0 9 32 * *', '0 9 0 * *']) {
      expect(nextCronOccurrence(expr, 'UTC', after), expr).toBeNull();
    }
  });

  test('an unknown timezone falls back to UTC instead of crashing', () => {
    // A stale schedule_timezone from an older tzdata must degrade, not blank
    // the wallboard.
    expect(utc(nextCronOccurrence('0 9 * * *', 'Mars/Olympus_Mons', after)))
      .toBe('2026-08-30T09:00:00Z');
    expect(zoneTag('Mars/Olympus_Mons')).toBe('Mars/Olympus_Mons');
  });

  test('a null or empty timezone means UTC', () => {
    expect(utc(nextCronOccurrence('0 9 * * *', null, after))).toBe('2026-08-30T09:00:00Z');
    expect(utc(nextCronOccurrence('0 9 * * *', '', after))).toBe('2026-08-30T09:00:00Z');
    expect(utc(nextCronOccurrence('0 9 * * *', undefined, after))).toBe('2026-08-30T09:00:00Z');
  });

  test('surrounding whitespace is tolerated', () => {
    expect(utc(nextCronOccurrence('  0 9 * * *  ', 'UTC', after))).toBe('2026-08-30T09:00:00Z');
    expect(utc(nextCronOccurrence('0\t9  *   * *', 'UTC', after))).toBe('2026-08-30T09:00:00Z');
  });
});

/* ─── The per-minute memo ──────────────────────────────────
   The wallboard calls this once a second for every workflow. The cache key has
   to carry the zone as well as the expression, or one workflow's answer is
   served to another. */
describe('occurrence cache', () => {
  test('two zones asking in the same minute get their own answers', () => {
    const after = at('2026-08-30T00:00:30Z');
    expect(utc(nextCronOccurrence('0 9 * * *', 'UTC', after))).toBe('2026-08-30T09:00:00Z');
    expect(utc(nextCronOccurrence('0 9 * * *', 'Asia/Kolkata', after))).toBe('2026-08-30T03:30:00Z');
    // Back to the first, which must not have been overwritten.
    expect(utc(nextCronOccurrence('0 9 * * *', 'UTC', after))).toBe('2026-08-30T09:00:00Z');
  });

  test('a repeat call inside the same minute is stable', () => {
    const first = nextCronOccurrence('*/15 * * * *', 'UTC', at('2026-08-30T10:07:00Z'));
    const again = nextCronOccurrence('*/15 * * * *', 'UTC', at('2026-08-30T10:07:59Z'));
    expect(utc(first)).toBe(utc(again));
  });

  test('crossing a minute boundary re-resolves', () => {
    expect(utc(nextCronOccurrence('*/15 * * * *', 'UTC', at('2026-08-30T10:14:00Z'))))
      .toBe('2026-08-30T10:15:00Z');
    expect(utc(nextCronOccurrence('*/15 * * * *', 'UTC', at('2026-08-30T10:15:30Z'))))
      .toBe('2026-08-30T10:30:00Z');
  });

  test('the answer is strictly after `after`, so a chain cannot stall', () => {
    // The builder previews by feeding the first fire back in; returning it again
    // would render "Next: X, then X" forever.
    const first = nextCronOccurrence('0 9 * * *', 'UTC', at('2026-08-30T00:00:00Z'))!;
    const second = nextCronOccurrence('0 9 * * *', 'UTC', first)!;
    expect(second.getTime()).toBeGreaterThan(first.getTime());
  });
});

/* ─── Labels ───────────────────────────────────────────────
   The zone tag depends on the VIEWER's zone, which vitest.config.ts pins to
   UTC. Every case below is one the ScheduleBuilder can emit. */
describe('cronLabel', () => {
  test('sub-hourly cadences are zoneless', () => {
    expect(cronLabel('* * * * *', 'Asia/Kolkata')).toBe('every minute');
    expect(cronLabel('*/15 * * * *', 'Asia/Kolkata')).toBe('every 15 min');
    expect(cronLabel('*/1 * * * *', 'Asia/Kolkata')).toBe('every 1 min');
    expect(cronLabel('0 */1 * * *', 'Asia/Kolkata')).toBe('hourly');
    expect(cronLabel('0 */3 * * *', 'Asia/Kolkata')).toBe('every 3h');
  });

  test('fixed times read as wall clock in the workflow zone', () => {
    expect(cronLabel('0 9 * * *', 'UTC')).toBe('daily at 09:00');
    expect(cronLabel('5 0 * * *', 'UTC')).toBe('daily at 00:05');
    expect(cronLabel('0 0 15 * *', 'UTC')).toBe('monthly on day 15 at 00:00');
    expect(cronLabel('30 9 * * 1')).toBe('every Mon at 09:30');
    expect(cronLabel('30 9 * * 1,3,5', 'UTC')).toBe('Mon/Wed/Fri at 09:30');
    expect(cronLabel('0 * * * *', 'UTC')).toBe('hourly at :00');
    expect(cronLabel('17 * * * *', 'UTC')).toBe('hourly at :17');
  });

  test('a foreign zone is tagged, the viewer\'s own is not', () => {
    // The suite runs under TZ=UTC, so UTC is the viewer's zone.
    expect(cronLabel('0 9 * * *', 'Asia/Kolkata')).toBe('daily at 09:00 GMT+5:30');
    expect(cronLabel('0 9 * * *', 'UTC')).toBe('daily at 09:00');
    expect(cronLabel('0 9 * * *', null)).toBe('daily at 09:00');
  });

  test('an expression it cannot read is shown verbatim', () => {
    // Never a guess and never blank — the operator still sees what is saved.
    // `*/2` is in the list because a weekly label with the days left out
    // ("at 09:00") is worse than the expression itself.
    for (const expr of ['@daily', '0 9 * * mon', '0 9 * * 1-5', '0 9 1 * 1',
                        '0 9 * 6 *', 'nonsense', '0 9 * * */2']) {
      expect(cronLabel(expr, 'UTC'), expr).toBe(expr);
    }
  });

  test('the weekly label names the day that actually fires', () => {
    // Where an operator meets defect 1: the label and the fire it is built from
    // are checked against each other, so neither can move without the other.
    // getUTCDay() is 0=Sun..6=Sat, which is the crontab field exactly.
    const names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    for (let dow = 0; dow <= 6; dow++) {
      const expr = `0 9 * * ${dow}`;
      expect(nextCronOccurrence(expr, 'UTC', at('2026-08-30T00:00:00Z'))!.getUTCDay(), expr)
        .toBe(dow);
      expect(cronLabel(expr, 'UTC'), expr).toBe(`every ${names[dow]} at 09:00`);
    }
    // 7 is Sunday, not a workflow that never runs.
    expect(cronLabel('0 9 * * 7', 'UTC')).toBe('every Sun at 09:00');
    expect(cronLabel('0 9 * * 0,7', 'UTC')).toBe('every Sun at 09:00');
  });
});
