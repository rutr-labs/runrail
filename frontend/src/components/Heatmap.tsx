import { useEffect, useState } from 'react';
import { api } from '../api';

/** Activity grid: one cell per day, weeks as columns running Monday → Sunday.
 *  Green intensity tracks run volume; any failures tint the day amber/red.
 *  Optional 4w/8w/16w/6m range selector; the choice persists in localStorage. */

type DayStat = { date: string; success: number; failed: number; other: number };

const CELL = 12;
const GAP = 3;
const DAY_MS = 86_400_000;

/** Selectable history ranges. 6m = 26 weeks = 182 days (API caps days at 366). */
const RANGES = [
  { label: '4w', weeks: 4 },
  { label: '8w', weeks: 8 },
  { label: '16w', weeks: 16 },
  { label: '6m', weeks: 26 },
] as const;

const WEEKS_STORAGE_KEY = 'runrail.heatmap.weeks';

function storedWeeks(fallback: number): number {
  try {
    const parsed = Number(localStorage.getItem(WEEKS_STORAGE_KEY));
    return RANGES.some(r => r.weeks === parsed) ? parsed : fallback;
  } catch {
    return fallback; // localStorage unavailable (private mode, etc.)
  }
}

function cellColor(stat: DayStat | undefined): { background: string; opacity?: number } {
  if (!stat || stat.success + stat.failed + stat.other === 0) {
    return { background: 'var(--hm-empty, rgba(127,127,127,0.10))' };
  }
  const total = stat.success + stat.failed;
  if (stat.failed > 0) {
    return stat.failed >= Math.max(1, total / 2)
      ? { background: 'var(--danger)', opacity: 0.85 }
      : { background: 'var(--warning)', opacity: 0.85 };
  }
  const intensity = stat.success >= 100 ? 1 : stat.success >= 25 ? 0.75 : stat.success >= 5 ? 0.5 : 0.3;
  return { background: 'var(--success)', opacity: intensity };
}

export function RunHeatmap({ workflowId, weeks: defaultWeeks = 16, selectable = false }: {
  workflowId?: number;
  /** Fixed range when not selectable; initial fallback when selectable. */
  weeks?: number;
  /** Show the 4w/8w/16w/6m range control; the choice persists across sessions. */
  selectable?: boolean;
}) {
  const [stats, setStats] = useState<Record<string, DayStat>>({});
  const [weeks, setWeeks] = useState(() => (selectable ? storedWeeks(defaultWeeks) : defaultWeeks));

  const pickWeeks = (w: number) => {
    setWeeks(w);
    try { localStorage.setItem(WEEKS_STORAGE_KEY, String(w)); } catch { /* non-fatal */ }
  };

  useEffect(() => {
    let stale = false; // ignore out-of-order responses when switching ranges quickly
    const params = new URLSearchParams({ days: String(weeks * 7) });
    if (workflowId) params.set('workflow_id', String(workflowId));
    api<DayStat[]>(`/stats/daily?${params}`)
      .then(rows => { if (!stale) setStats(Object.fromEntries(rows.map(r => [r.date, r]))); })
      .catch(() => {});
    return () => { stale = true; };
  }, [workflowId, weeks]);

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  // Last cell is today; each column is one Monday → Sunday week.
  const end = today.getTime();
  const start = end - (weeks * 7 - 1) * DAY_MS;
  const firstMonday = start - ((new Date(start).getDay() + 6) % 7) * DAY_MS;

  const columns: { key: number; days: (DayStat | undefined)[] }[] = [];
  const monthLabels: { index: number; label: string }[] = [];
  let lastMonth = -1;
  for (let week = 0; ; week++) {
    const weekStart = firstMonday + week * 7 * DAY_MS;
    if (weekStart > end) break;
    const days = Array.from({ length: 7 }, (_, dow) => {
      const t = weekStart + dow * DAY_MS;
      if (t < start || t > end) return undefined;
      return stats[new Date(t).toISOString().slice(0, 10)] ?? { date: '', success: 0, failed: 0, other: 0 };
    });
    const month = new Date(weekStart).getMonth();
    if (month !== lastMonth) {
      monthLabels.push({ index: week, label: new Date(weekStart).toLocaleDateString(undefined, { month: 'short' }) });
      lastMonth = month;
    }
    columns.push({ key: weekStart, days });
  }

  return (
    <div className="run-heatmap-wrap">
      {selectable && (
        <div className="segmented slim run-heatmap-range" role="group" aria-label="Activity range">
          {RANGES.map(r => (
            <button
              key={r.weeks}
              type="button"
              className={r.weeks === weeks ? 'active' : ''}
              aria-pressed={r.weeks === weeks}
              title={`Last ${r.weeks} weeks`}
              onClick={() => pickWeeks(r.weeks)}
            >
              {r.label}
            </button>
          ))}
        </div>
      )}
      <div className="run-heatmap">
        <div className="run-heatmap-months" style={{ paddingLeft: 26 }}>
          {monthLabels.map(({ index, label }, i) => (
            <span key={i} style={{ left: index * (CELL + GAP) }}>{label}</span>
          ))}
        </div>
        <div className="run-heatmap-body">
          <div className="run-heatmap-dows">
            {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(d => <span key={d}>{d}</span>)}
          </div>
          {/* Keyed on the range so switching replays the columnar wave-in. */}
          <div key={weeks} className="run-heatmap-grid" style={{ gap: GAP }}>
            {columns.map((col, colIndex) => (
              <div key={col.key} className="run-heatmap-col"
                   style={{ gap: GAP, animationDelay: `${colIndex * 10}ms` }}>
                {col.days.map((stat, dow) => {
                  const date = new Date(col.key + dow * DAY_MS);
                  const tip = stat === undefined ? undefined
                    : `${date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}` +
                      (stat.success + stat.failed + stat.other
                        ? ` · ${stat.success} ok${stat.failed ? ` · ${stat.failed} failed` : ''}${stat.other ? ` · ${stat.other} other` : ''}`
                        : ' · no runs');
                  return (
                    <span key={dow} className="run-heatmap-cell" title={tip}
                          style={{ width: CELL, height: CELL,
                                   visibility: stat === undefined ? 'hidden' : 'visible',
                                   ...cellColor(stat) }} />
                  );
                })}
              </div>
            ))}
          </div>
        </div>
        <div className="run-heatmap-legend">
          <span>Fewer</span>
          {[0.3, 0.5, 0.75, 1].map(o => (
            <span key={o} className="run-heatmap-cell" style={{ width: CELL, height: CELL, background: 'var(--success)', opacity: o }} />
          ))}
          <span>More</span>
          <span className="run-heatmap-cell" style={{ width: CELL, height: CELL, background: 'var(--warning)', opacity: 0.85, marginLeft: 10 }} />
          <span>Mixed</span>
          <span className="run-heatmap-cell" style={{ width: CELL, height: CELL, background: 'var(--danger)', opacity: 0.85 }} />
          <span>Failing</span>
        </div>
      </div>
    </div>
  );
}
