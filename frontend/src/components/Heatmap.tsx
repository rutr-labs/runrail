import { useEffect, useState } from 'react';
import { api } from '../api';

/** GitHub-style activity grid: one cell per day, weeks as columns.
 *  Green intensity tracks run volume; any failures tint the day amber/red. */

type DayStat = { date: string; success: number; failed: number; other: number };

const CELL = 12;
const GAP = 3;
const DAY_MS = 86_400_000;

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

export function RunHeatmap({ workflowId, weeks = 16 }: { workflowId?: number; weeks?: number }) {
  const [stats, setStats] = useState<Record<string, DayStat>>({});
  useEffect(() => {
    const params = new URLSearchParams({ days: String(weeks * 7) });
    if (workflowId) params.set('workflow_id', String(workflowId));
    api<DayStat[]>(`/stats/daily?${params}`)
      .then(rows => setStats(Object.fromEntries(rows.map(r => [r.date, r]))))
      .catch(() => {});
  }, [workflowId, weeks]);

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  // Last cell is today; columns start on Sunday like a calendar.
  const end = today.getTime();
  const start = end - (weeks * 7 - 1) * DAY_MS;
  const firstSunday = start - new Date(start).getDay() * DAY_MS;

  const columns: { key: number; days: (DayStat | undefined)[] }[] = [];
  const monthLabels: { index: number; label: string }[] = [];
  let lastMonth = -1;
  for (let week = 0; ; week++) {
    const weekStart = firstSunday + week * 7 * DAY_MS;
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
    <div className="run-heatmap">
      <div className="run-heatmap-months" style={{ paddingLeft: 26 }}>
        {monthLabels.map(({ index, label }, i) => (
          <span key={i} style={{ left: index * (CELL + GAP) }}>{label}</span>
        ))}
      </div>
      <div className="run-heatmap-body">
        <div className="run-heatmap-dows">
          {['', 'Mon', '', 'Wed', '', 'Fri', ''].map((d, i) => <span key={i}>{d}</span>)}
        </div>
        <div className="run-heatmap-grid" style={{ gap: GAP }}>
          {columns.map(col => (
            <div key={col.key} className="run-heatmap-col" style={{ gap: GAP }}>
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
  );
}
