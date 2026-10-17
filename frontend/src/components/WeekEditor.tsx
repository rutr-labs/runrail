import { useMemo, useRef, useState } from 'react';
import { Moon, Plus, Sun, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import { wallParts } from '../cron';
import {
  DAYS, DAY_SHORT, crossesMidnight, nextChange, isActive, weekSegments, weeklyHours,
  type Day, type ServiceWindow,
} from '../serviceWindows';

/* ─── Week editor ──────────────────────────────────────────
   When a scheduled service runs: any number of windows, each a set of days
   and a wall-clock span in the service's zone. Two views of one list, kept in
   step: a week grid you can read at a glance (and drag across to add a
   window), and rows you can edit precisely. Hovering either highlights the
   same window in both.

   A window's days are where it STARTS: Friday 22:00 → 06:00 runs into
   Saturday morning, and is drawn that way — one colour continuing onto the
   next row — because that is exactly where people get overnight schedules
   wrong. */

const SNAP_MINUTES = 30;
const PRESETS: { label: string; icon?: typeof Sun; windows: ServiceWindow[] }[] = [
  { label: 'Weekdays 9–5', icon: Sun, windows: [{ days: ['mon', 'tue', 'wed', 'thu', 'fri'], start: '09:00', end: '17:00' }] },
  { label: 'Every night 22–06', icon: Moon, windows: [{ days: [...DAYS], start: '22:00', end: '06:00' }] },
  { label: 'Weekends', windows: [{ days: ['sat', 'sun'], start: '00:00', end: '00:00' }] },
];

const hhmm = (minutes: number) =>
  `${String(Math.floor(minutes / 60) % 24).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;

function formatInZone(date: Date, zone: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: zone, weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
  } catch {
    return date.toISOString().slice(0, 16).replace('T', ' ');
  }
}

export function untilLabel(target: Date, now: number): string {
  const minutes = Math.max(0, Math.round((target.getTime() - now) / 60_000));
  if (minutes < 1) return 'in under a minute';
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `in ${hours}h${minutes % 60 ? ` ${minutes % 60}m` : ''}`;
  const days = Math.floor(hours / 24);
  return `in ${days}d${hours % 24 ? ` ${hours % 24}h` : ''}`;
}

/** What the schedule does next, in words, for this zone. */
export function scheduleSummary(windows: ServiceWindow[], zone: string, now = Date.now()): string {
  if (!windows.length) return 'No windows yet — it will never start on its own.';
  const at = new Date(now);
  const change = nextChange(windows, zone, at);
  const active = isActive(windows, zone, at);
  if (!change) return active ? 'Covers the whole week — always running.' : 'Never runs.';
  return active
    ? `Running now · stops ${formatInZone(change, zone)} (${untilLabel(change, now)})`
    : `Next start ${formatInZone(change, zone)} (${untilLabel(change, now)})`;
}

/** Read-only week, for cards and the service page. */
export function WeekStrip({ windows, zone, compact = false }: {
  windows: ServiceWindow[]; zone: string; compact?: boolean;
}) {
  return <WeekGrid windows={windows} zone={zone} compact={compact} />;
}

function WeekGrid({ windows, zone, compact, hovered, onHover, onCreate }: {
  windows: ServiceWindow[];
  zone: string;
  compact?: boolean;
  hovered?: number | null;
  onHover?: (index: number | null) => void;
  onCreate?: (day: Day, from: number, to: number) => void;
}) {
  const rows = useMemo(() => weekSegments(windows), [windows]);
  const now = useMemo(() => {
    try {
      const w = wallParts(new Date(), zone);
      return { row: (w.dow + 6) % 7, minute: w.h * 60 + w.mi };
    } catch { return null; }
  }, [zone]);
  const [drag, setDrag] = useState<{ row: number; from: number; to: number } | null>(null);
  const tracks = useRef<(HTMLDivElement | null)[]>([]);

  const minuteAt = (row: number, clientX: number) => {
    const track = tracks.current[row];
    if (!track) return 0;
    const box = track.getBoundingClientRect();
    const raw = ((clientX - box.left) / box.width) * 1440;
    return Math.max(0, Math.min(1440, Math.round(raw / SNAP_MINUTES) * SNAP_MINUTES));
  };

  return (
    <div className={clsx('week-grid', compact && 'week-grid--compact', onCreate && 'week-grid--editable')}
         onPointerUp={() => {
           if (drag && onCreate) {
             const from = Math.min(drag.from, drag.to);
             const to = Math.max(drag.from, drag.to);
             if (to - from >= SNAP_MINUTES) onCreate(DAYS[drag.row], from, to === 1440 ? 0 : to);
           }
           setDrag(null);
         }}
         onPointerLeave={() => setDrag(null)}>
      {!compact && (
        <div className="week-axis" aria-hidden="true">
          <span />
          <div className="week-axis-scale">
            {[0, 6, 12, 18, 24].map(h => <i key={h} style={{ left: `${(h / 24) * 100}%` }}>{String(h).padStart(2, '0')}</i>)}
          </div>
        </div>
      )}
      {DAYS.map((day, row) => (
        <div className="week-row" key={day}>
          <span className="week-day">{compact ? DAY_SHORT[day][0] : DAY_SHORT[day]}</span>
          <div
            className="week-track"
            ref={el => { tracks.current[row] = el; }}
            onPointerDown={e => {
              if (!onCreate || (e.target as HTMLElement).closest('.week-bar')) return;
              const minute = minuteAt(row, e.clientX);
              setDrag({ row, from: minute, to: minute });
              (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
            }}
            onPointerMove={e => {
              if (drag && drag.row === row) setDrag({ ...drag, to: minuteAt(row, e.clientX) });
            }}
          >
            {[6, 12, 18].map(h => <i key={h} className="week-tick" style={{ left: `${(h / 24) * 100}%` }} />)}
            {rows[row].map((seg, i) => (
              <span
                key={i}
                className={clsx('week-bar', `week-bar--c${seg.index % 6}`,
                                hovered != null && (hovered === seg.index ? 'is-hot' : 'is-dim'))}
                style={{ left: `${(seg.from / 1440) * 100}%`, width: `${((seg.to - seg.from) / 1440) * 100}%` }}
                title={`${hhmm(seg.from)}–${hhmm(seg.to)}`}
                onPointerEnter={() => onHover?.(seg.index)}
                onPointerLeave={() => onHover?.(null)}
              />
            ))}
            {drag && drag.row === row && drag.to !== drag.from && (
              <span className="week-bar week-bar--ghost" style={{
                left: `${(Math.min(drag.from, drag.to) / 1440) * 100}%`,
                width: `${(Math.abs(drag.to - drag.from) / 1440) * 100}%`,
              }}>
                {!compact && <em>{hhmm(Math.min(drag.from, drag.to))}–{hhmm(Math.max(drag.from, drag.to))}</em>}
              </span>
            )}
            {now && now.row === row && (
              <span className="week-now" style={{ left: `${(now.minute / 1440) * 100}%` }} title="Now" />
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Editable schedule. Controlled: the host owns `windows`. */
export function WeekEditor({ windows, zone, onChange }: {
  windows: ServiceWindow[];
  zone: string;
  onChange: (next: ServiceWindow[]) => void;
}) {
  const [hovered, setHovered] = useState<number | null>(null);
  const update = (index: number, patch: Partial<ServiceWindow>) =>
    onChange(windows.map((w, i) => (i === index ? { ...w, ...patch } : w)));
  const toggleDay = (index: number, day: Day) => {
    const days = windows[index].days;
    update(index, { days: days.includes(day) ? days.filter(d => d !== day) : DAYS.filter(d => d === day || days.includes(d)) });
  };
  const hours = weeklyHours(windows);

  return (
    <div className="week-editor">
      <WeekGrid
        windows={windows} zone={zone} hovered={hovered} onHover={setHovered}
        onCreate={(day, from, to) => onChange([...windows, { days: [day], start: hhmm(from), end: hhmm(to) }])}
      />
      <p className="week-hint">
        Drag across a day to add a window, or add one below. Times are in <b>{zone}</b>.
        {windows.length > 0 && <> · {hours % 1 ? hours.toFixed(1) : hours} h a week</>}
      </p>

      <ol className="window-rows">
        {windows.map((w, index) => {
          const overnight = crossesMidnight(w) && w.start !== w.end;
          return (
            <li key={index}
                className={clsx('window-row', hovered === index && 'is-hot', !w.days.length && 'is-invalid')}
                onPointerEnter={() => setHovered(index)} onPointerLeave={() => setHovered(null)}>
              <span className={clsx('window-swatch', `week-bar--c${index % 6}`)} aria-hidden="true" />
              <div className="window-days" role="group" aria-label={`Window ${index + 1} days`}>
                {DAYS.map(day => (
                  <button key={day} type="button"
                          className={clsx('day-chip', w.days.includes(day) && 'on')}
                          aria-pressed={w.days.includes(day)}
                          aria-label={DAY_SHORT[day]}
                          onClick={() => toggleDay(index, day)}>
                    {DAY_SHORT[day].slice(0, 2)}
                  </button>
                ))}
              </div>
              <div className="window-times">
                <input type="time" step={300} value={w.start} aria-label={`Window ${index + 1} start`}
                       onChange={e => update(index, { start: e.target.value })} required />
                <span className="window-arrow">→</span>
                <input type="time" step={300} value={w.end} aria-label={`Window ${index + 1} end`}
                       onChange={e => update(index, { end: e.target.value })} required />
                {overnight && <span className="window-tag"><Moon size={11} /> next day</span>}
                {w.start === w.end && <span className="window-tag">all day</span>}
              </div>
              <button type="button" className="window-remove" aria-label={`Remove window ${index + 1}`}
                      onClick={() => onChange(windows.filter((_, i) => i !== index))}>
                <Trash2 size={13} />
              </button>
              {!w.days.length && <small className="window-error">Pick at least one day.</small>}
            </li>
          );
        })}
      </ol>

      <div className="window-add">
        <button type="button" className="btn btn-ghost btn-sm"
                onClick={() => onChange([...windows, { days: ['mon', 'tue', 'wed', 'thu', 'fri'], start: '09:00', end: '17:00' }])}>
          <Plus size={13} /> Add window
        </button>
        <span className="window-presets-label">or start from</span>
        {PRESETS.map(p => (
          <button key={p.label} type="button" className="preset-chip" onClick={() => onChange(p.windows)}>
            {p.icon && <p.icon size={12} />} {p.label}
          </button>
        ))}
      </div>
      <p className="week-summary">{scheduleSummary(windows, zone)}</p>
    </div>
  );
}

/** Today's windows as one slim 24-hour bar with a now-marker, in the service's
 *  zone — for a row in a list, where a whole week would be noise. */
export function TodayBar({ windows, zone, className }: {
  windows: ServiceWindow[]; zone: string; className?: string;
}) {
  let row = 0;
  let minute = 0;
  try {
    const w = wallParts(new Date(), zone);
    row = (w.dow + 6) % 7;
    minute = w.h * 60 + w.mi;
  } catch { /* unknown zone: draw it as UTC-less, still useful */ }
  const segments = weekSegments(windows)[row];
  const label = segments.length
    ? `Today: ${segments.map(s => `${hhmm(s.from)}–${s.to === 1440 ? '24:00' : hhmm(s.to)}`).join(', ')} (${zone})`
    : `Not scheduled today (${zone})`;
  return (
    <div className={clsx('today-bar', className)} title={label} aria-label={label} role="img">
      {segments.map((s, i) => (
        <span key={i} className={clsx('today-bar-seg', `week-bar--c${s.index % 6}`,
                                      minute >= s.from && minute < s.to && 'is-now')}
              style={{ left: `${(s.from / 1440) * 100}%`, width: `${((s.to - s.from) / 1440) * 100}%` }} />
      ))}
      <span className="today-bar-now" style={{ left: `${(minute / 1440) * 100}%` }} />
    </div>
  );
}
