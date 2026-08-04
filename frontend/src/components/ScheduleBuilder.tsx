import { useMemo, useState } from 'react';
import { cronLabel, nextCronOccurrence, viewerZone, zoneTag } from '../cron';

/* ─── Schedule builder ─────────────────────────────────────
   Dropdown-first schedule authoring; raw cron is the "Advanced"
   mode. Self-contained state that publishes through hidden inputs
   (`cron`, `schedule_timezone`), so the host <form onSubmit={FormData}>
   flow stays untouched. Parsing an existing cron back into a mode is
   best-effort — anything the dropdowns can't express opens as Advanced. */

type Mode = 'none' | 'minutes' | 'hourly' | 'daily' | 'weekly' | 'monthly' | 'custom';

interface State {
  mode: Mode;
  everyMinutes: number;   // minutes mode
  atMinute: number;       // hourly mode
  time: string;           // "HH:MM" — daily/weekly/monthly
  days: number[];         // weekly, cron dow numbers (0=Sun)
  dayOfMonth: number;     // monthly
  custom: string;         // raw cron — custom mode
}

const MINUTE_PRESETS = [1, 2, 5, 10, 15, 20, 30, 45];
const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_CRON = [1, 2, 3, 4, 5, 6, 0]; // Mon-first display → cron dow

const DEFAULTS: Omit<State, 'mode'> = {
  everyMinutes: 15, atMinute: 0, time: '09:00', days: [1], dayOfMonth: 1, custom: '',
};

function parseCron(cron: string): State {
  const custom: State = { mode: 'custom', ...DEFAULTS, custom: cron };
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return custom;
  const [min, hour, dom, month, dow] = parts;
  if (month !== '*') return custom;
  const step = min.match(/^\*\/(\d+)$/);
  const two = (n: number) => String(n).padStart(2, '0');

  if ((min === '*' || step) && hour === '*' && dom === '*' && dow === '*') {
    return { mode: 'minutes', ...DEFAULTS, everyMinutes: step ? Number(step[1]) : 1 };
  }
  if (!/^\d+$/.test(min)) return custom;
  const minute = Number(min);
  if (hour === '*' && dom === '*' && dow === '*') {
    return { mode: 'hourly', ...DEFAULTS, atMinute: minute };
  }
  if (!/^\d+$/.test(hour)) return custom;
  const time = `${two(Number(hour))}:${two(minute)}`;
  if (dom === '*' && dow === '*') return { mode: 'daily', ...DEFAULTS, time };
  if (dom === '*' && /^\d+(,\d+)*$/.test(dow)) {
    const days = dow.split(',').map(Number);
    if (days.every(d => d >= 0 && d <= 6)) return { mode: 'weekly', ...DEFAULTS, time, days };
  }
  if (dow === '*' && /^\d+$/.test(dom) && Number(dom) >= 1 && Number(dom) <= 28) {
    return { mode: 'monthly', ...DEFAULTS, time, dayOfMonth: Number(dom) };
  }
  return custom;
}

function toCron(s: State): string | null {
  const [h, m] = s.time.split(':').map(Number);
  switch (s.mode) {
    case 'none': return null;
    case 'minutes': return s.everyMinutes <= 1 ? '* * * * *' : `*/${s.everyMinutes} * * * *`;
    case 'hourly': return `${s.atMinute} * * * *`;
    case 'daily': return `${m} ${h} * * *`;
    case 'weekly': {
      const days = [...s.days].sort((a, b) => a - b);
      return days.length ? `${m} ${h} * * ${days.join(',')}` : null;
    }
    case 'monthly': return `${m} ${h} ${s.dayOfMonth} * *`;
    case 'custom': return s.custom.trim() || null;
  }
}

/* Browser zone and UTC pinned on top; the rest of the IANA registry after. */
function zoneOptions(current: string): string[] {
  const all: string[] = typeof Intl.supportedValuesOf === 'function'
    ? Intl.supportedValuesOf('timeZone') : [];
  const pinned = [viewerZone(), 'UTC'];
  if (current && !pinned.includes(current) ) pinned.push(current);
  return [...pinned, ...all.filter(z => !pinned.includes(z))];
}

export function ScheduleBuilder({ initialCron, initialTimezone }: {
  initialCron: string | null;
  initialTimezone: string | null;
}) {
  const [state, setState] = useState<State>(() =>
    initialCron ? parseCron(initialCron) : { mode: 'none', ...DEFAULTS });
  // Existing UTC schedules stay UTC; brand-new ones default to the author's zone.
  const [tz, setTz] = useState<string>(() =>
    initialTimezone ?? (initialCron ? 'UTC' : viewerZone()));

  const cron = toCron(state);
  const zones = useMemo(() => zoneOptions(tz), [tz]);
  const set = (patch: Partial<State>) => setState(s => ({ ...s, ...patch }));

  const preview = useMemo(() => {
    if (!cron) return null;
    const first = nextCronOccurrence(cron, tz);
    if (!first) return state.mode === 'custom' ? 'Unrecognized expression — previews unavailable' : null;
    const second = nextCronOccurrence(cron, tz, first);
    const fmt = (d: Date) => d.toLocaleString(undefined, {
      weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
    return `Next: ${fmt(first)}${second ? `, then ${fmt(second)}` : ''}`;
  }, [cron, tz, state.mode]);

  return (
    <div className="sched-builder">
      <input type="hidden" name="cron" value={cron ?? ''} />
      <input type="hidden" name="schedule_timezone" value={cron ? tz : ''} />

      <div className="sched-row-main">
        <label className="field">
          <span>Schedule</span>
          <select value={state.mode} onChange={e => set({ mode: e.target.value as Mode })}>
            <option value="none">Manual runs only</option>
            <option value="minutes">Every few minutes</option>
            <option value="hourly">Hourly</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
            <option value="custom">Advanced (cron)</option>
          </select>
        </label>

        {state.mode !== 'none' && (
          <label className="field">
            <span>Timezone</span>
            <select value={tz} onChange={e => setTz(e.target.value)}>
              {zones.map(z => (
                <option key={z} value={z}>
                  {z === viewerZone() ? `${z} — your timezone` : z} ({zoneTag(z)})
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {state.mode === 'minutes' && (
        <label className="field">
          <span>Run every</span>
          <select value={state.everyMinutes} onChange={e => set({ everyMinutes: Number(e.target.value) })}>
            {MINUTE_PRESETS.map(n => (
              <option key={n} value={n}>{n === 1 ? 'minute' : `${n} minutes`}</option>
            ))}
          </select>
        </label>
      )}

      {state.mode === 'hourly' && (
        <label className="field">
          <span>At minute</span>
          <select value={state.atMinute} onChange={e => set({ atMinute: Number(e.target.value) })}>
            {Array.from({ length: 60 }, (_, i) => (
              <option key={i} value={i}>:{String(i).padStart(2, '0')}</option>
            ))}
          </select>
        </label>
      )}

      {(state.mode === 'daily' || state.mode === 'weekly' || state.mode === 'monthly') && (
        <div className="field-row">
          {state.mode === 'monthly' && (
            <label className="field">
              <span>On day of month</span>
              <select value={state.dayOfMonth} onChange={e => set({ dayOfMonth: Number(e.target.value) })}>
                {Array.from({ length: 28 }, (_, i) => (
                  <option key={i + 1} value={i + 1}>{i + 1}</option>
                ))}
              </select>
              <small>Days 1–28, so it fires every month.</small>
            </label>
          )}
          <label className="field">
            <span>At time</span>
            <input type="time" value={state.time} required
                   onChange={e => e.target.value && set({ time: e.target.value })} />
          </label>
        </div>
      )}

      {state.mode === 'weekly' && (
        <div className="field">
          <span>On days</span>
          <div className="sched-days" role="group" aria-label="Days of week">
            {DAY_NAMES.map((name, i) => {
              const dow = DAY_CRON[i];
              const on = state.days.includes(dow);
              return (
                <button key={name} type="button"
                        className={`day-chip${on ? ' on' : ''}`}
                        aria-pressed={on}
                        onClick={() => set({
                          days: on ? state.days.filter(d => d !== dow) : [...state.days, dow],
                        })}>
                  {name}
                </button>
              );
            })}
          </div>
          {state.days.length === 0 && <small className="sched-warn">Pick at least one day</small>}
        </div>
      )}

      {state.mode === 'custom' && (
        <label className="field">
          <span>Cron expression <em>five fields, evaluated in the timezone above</em></span>
          <input className="code-input" value={state.custom} placeholder="0 6 * * *"
                 onChange={e => set({ custom: e.target.value })} />
        </label>
      )}

      {state.mode !== 'none' && (
        <small className="sched-preview">
          {cron ? <>
            <b>{cronLabel(cron, tz)}</b>
            {preview ? ` · ${preview}` : ''}
          </> : ' '}
        </small>
      )}
    </div>
  );
}
