import { ReactNode, useState } from 'react';
import { AlarmClock, AlertTriangle, Hourglass } from 'lucide-react';
import clsx from 'clsx';

/* ─── Watchdog fields ──────────────────────────────────────
   The two schedule watchdogs, for the workflow create/edit modal.

   Both are opt-in (NULL = off), so neither is a number box sitting there with
   a default in it — an empty box reads as "not configured yet" when it really
   means "this alarm does not exist". A toggle states the choice, and only then
   reveals a value.

   Self-contained state published through hidden inputs, exactly like
   ScheduleBuilder, so the host's <form onSubmit={FormData}> flow is untouched.
   The visible controls carry no `name` — only the hidden inputs publish — but
   they do carry `required`/`min`, so an emptied box fails native validation
   instead of silently saving the watchdog away. */

const UNITS: { value: number; label: string }[] = [
  { value: 1, label: 'minutes' },
  { value: 60, label: 'hours' },
  { value: 1440, label: 'days' },
];

/** Split stored minutes into the largest whole unit for editing. */
function splitMinutes(minutes?: number | null): { amount: string; unit: number } {
  if (!minutes) return { amount: '', unit: 1 };
  for (const unit of [1440, 60]) {
    if (minutes % unit === 0) return { amount: String(minutes / unit), unit };
  }
  return { amount: String(minutes), unit: 1 };
}

const asMinutes = (amount: string, unit: number): number | null => {
  const n = Number(amount);
  return amount.trim() && Number.isFinite(n) && n >= 1 ? Math.round(n * unit) : null;
};

/** Reads what this component published. Spread into the workflow create/edit
 *  body — the two keys are already named exactly as WorkflowIn expects. */
export function watchdogValues(f: FormData): {
  missed_run_grace_minutes: number | null;
  sla_minutes: number | null;
} {
  const read = (key: string) => {
    const raw = f.get(key);
    return raw ? Number(raw) : null;
  };
  return {
    missed_run_grace_minutes: read('missed_grace_minutes'),
    sla_minutes: read('sla_minutes'),
  };
}

/* ─── One opt-in watchdog ────────────────────────────────── */
function Watchdog({
  name, icon, title, plainLanguage, defaultMinutes, initial, units, footnote, warning,
  summary,
}: {
  name: string;
  icon: ReactNode;
  title: string;
  /** The whole semantics in one sentence, always visible — on and off. */
  plainLanguage: string;
  defaultMinutes: number;
  initial?: number | null;
  units: { value: number; label: string }[];
  footnote: ReactNode;
  warning?: ReactNode;
  /** Restates the configured value back to the operator once it has one. */
  summary: (minutes: number) => string;
}) {
  const start = splitMinutes(initial);
  const [on, setOn] = useState(Boolean(initial));
  const [amount, setAmount] = useState(start.amount || String(defaultMinutes));
  const [unit, setUnit] = useState(start.amount ? start.unit : 1);
  const minutes = asMinutes(amount, unit);

  return (
    <div className={clsx('watchdog', on && 'is-on')}>
      <input type="hidden" name={name} value={on && minutes ? String(minutes) : ''} />

      <label className="watchdog-head">
        <span className="watchdog-glyph">{icon}</span>
        <span className="watchdog-text">
          <strong>{title}</strong>
          <small>{plainLanguage}</small>
        </span>
        <span className="toggle">
          <input
            type="checkbox"
            checked={on}
            onChange={e => {
              setOn(e.target.checked);
              // Re-arming after a clear-out should never leave an empty box behind.
              if (e.target.checked && !amount.trim()) { setAmount(String(defaultMinutes)); setUnit(1); }
            }}
          />
          <span />
        </span>
      </label>

      {on && (
        <div className="watchdog-body">
          <div className="watchdog-duration">
            <input
              type="number"
              min={1}
              step={1}
              required
              value={amount}
              onChange={e => setAmount(e.target.value)}
              aria-label={`${title} — amount`}
            />
            <select value={unit} onChange={e => setUnit(Number(e.target.value))} aria-label={`${title} — unit`}>
              {units.map(u => <option key={u.value} value={u.value}>{u.label}</option>)}
            </select>
            <span className="watchdog-summary">
              {minutes ? summary(minutes) : 'Enter a number, or switch this off.'}
            </span>
          </div>
          {warning && <p className="watchdog-warn"><AlertTriangle size={12} /> {warning}</p>}
          <p className="watchdog-note">{footnote}</p>
        </div>
      )}
    </div>
  );
}

/* ─── Both fields ────────────────────────────────────────── */
export function WatchdogFields({
  missedGraceMinutes = null, slaMinutes = null, hasSchedule = true,
}: {
  missedGraceMinutes?: number | null;
  slaMinutes?: number | null;
  /** Pass Boolean(workflow.schedule_cron). The missed-run check needs an
   *  expected fire time to be late for, so without a schedule it never fires —
   *  and says so. Defaults to true, which simply keeps quiet when unknown. */
  hasSchedule?: boolean;
}) {
  return (
    <div className="watchdogs">
      <span className="watchdogs-label">Alerts</span>

      <Watchdog
        name="missed_grace_minutes"
        icon={<AlarmClock size={14} />}
        title="Tell me if it doesn’t run"
        plainLanguage="Alert me if it hasn’t run within this long of when it should have."
        defaultMinutes={30}
        initial={missedGraceMinutes}
        units={UNITS}
        summary={m => `Silent for ${describe(m)} past a scheduled run → alert.`}
        warning={hasSchedule ? undefined : (
          <>This workflow has no schedule, so there is no expected time to be late for.
            Add one above for this check to do anything.</>
        )}
        footnote={<>
          Any run counts, however it was triggered. You get one alert when the schedule
          goes quiet and one when it starts running again. It keeps watching even while
          the workflow is paused — “someone disabled it and forgot” is the usual cause
          of a dead pipeline.
        </>}
      />

      <Watchdog
        name="sla_minutes"
        icon={<Hourglass size={14} />}
        title="Tell me if a run takes too long"
        plainLanguage="Alert me if a run hasn’t finished within this long of starting."
        defaultMinutes={60}
        initial={slaMinutes}
        units={UNITS}
        summary={m => `Still going ${describe(m)} after the run was created → alert.`}
        footnote={<>
          Measured from when the run was <em>created</em>, not when a worker picked it
          up, so time spent waiting in the queue counts — that is what catches a run
          that never started at all. One alert while the run is still going, and a note
          if it eventually finishes late. Backfills and runs waiting on an approval are
          exempt.
        </>}
      />

      <p className="watchdogs-foot">
        Both post to this workflow’s failure webhook, or the server-wide one if it has
        none. Snoozing the workflow mutes them.
      </p>
    </div>
  );
}

/** "90 minutes" → "1h 30m", for restating a value in the unit the operator thinks in. */
function describe(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours}h ${rest}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const spare = hours % 24;
  return spare ? `${days}d ${spare}h` : `${days}d`;
}
