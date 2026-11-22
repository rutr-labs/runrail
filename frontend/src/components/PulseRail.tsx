import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import { nextCronOccurrence } from '../cron';
import { formatDuration, timeAgo } from '../format';
import { useNow } from '../hooks';
import {
  axisStep, bucketize, railAxis, railHorizonLabel, railWeight, railWindow,
  railWindowLabel, runAt, untilLabel,
  type RailFire, type RailRun,
} from '../pulseRail';
import { CometCanvas } from './ui';

/* ─── The rail ─────────────────────────────────────────────
   One line of time across the top of the dashboard. Left of "now", every
   recent run is a dot standing on the rail where it started, coloured by how
   it ended; right of "now", hollow dots are the fires the schedule still owes.
   The strip drifts left as the clock moves, so a page left open keeps telling
   the truth without anyone reloading it.

   It answers at a glance what the run table answers by reading: was the last
   stretch quiet or busy, did anything fail and when, and what is about to
   happen. Details live in the readout above the strip rather than in
   per-dot tooltips — one place to look, and it never covers a neighbour. */

type RailFlow = {
  id: number;
  name: string;
  enabled: boolean;
  schedule_cron?: string | null;
  schedule_timezone?: string | null;
  snoozed?: boolean;
  snooze_pauses_runs?: boolean;
};

const PITCH = 10;       // px between columns
const ROWS = 7;         // dots a column shows before it summarises
const TICK_MS = 5000;   // how often the strip re-anchors to the clock
const MINUTE = 60_000;
const FIRES_PER_FLOW = 36;

const STATUS_WORD: Record<string, string> = {
  success: 'succeeded', failed: 'failed', running: 'running', queued: 'queued',
  cancelled: 'cancelled', waiting_approval: 'waiting for approval', skipped: 'skipped',
};

const clock = (t: number) =>
  new Date(t).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });

/** Every fire the schedule owes between now and the horizon. */
function upcomingFires(flows: RailFlow[], now: number, horizon: number): RailFire[] {
  const fires: RailFire[] = [];
  const end = now + horizon;
  for (const flow of flows) {
    if (!flow.enabled || !flow.schedule_cron) continue;
    if (flow.snoozed && flow.snooze_pauses_runs) continue;
    let cursor = new Date(now);
    for (let i = 0; i < FIRES_PER_FLOW; i++) {
      const next = nextCronOccurrence(flow.schedule_cron, flow.schedule_timezone, cursor);
      if (!next || next.getTime() > end) break;
      fires.push({ at: next.getTime(), workflowId: flow.id, name: flow.name });
      cursor = new Date(next.getTime() + 1000);
    }
  }
  return fires;
}

type Readout = { tone?: string; lead: string; rest: string };

export function PulseRail({ runs, flows, complete }: {
  runs: RailRun[];
  flows: RailFlow[];
  /** True when `runs` is the whole history rather than the newest page of it. */
  complete: boolean;
}) {
  const plotRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [hover, setHover] = useState<Readout | null>(null);
  const now = useNow(true, TICK_MS);
  const minute = Math.floor(now / MINUTE);

  useLayoutEffect(() => {
    const el = plotRef.current;
    if (!el) return;
    // clientWidth, not getBoundingClientRect: the layout width, which an
    // ancestor's entrance transform (panels scale in from 98.5%) cannot skew.
    const measure = () => setWidth(el.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // The scale is latched: it only moves when the run count clearly leaves the
  // band the current window suits (see railWindow).
  const windowRef = useRef<number | undefined>(undefined);
  const past = useMemo(() => {
    const next = railWindow(runs, now, complete, windowRef.current);
    windowRef.current = next;
    return next;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, complete, minute]);
  const horizon = past / 2;

  const flowName = useMemo(() => new Map(flows.map(f => [f.id, f.name])), [flows]);
  const fires = useMemo(
    () => upcomingFires(flows, minute * MINUTE, horizon + MINUTE),
    [flows, minute, horizon]);

  // Past takes two thirds, the future one third: the same px-per-ms on both
  // sides, so the strip is one axis and a fire glides straight into a run.
  const nowX = Math.round((width * 2) / 3);
  const scale = nowX / past;                       // px per ms
  const bucketMs = PITCH / (scale || 1);
  const x = (t: number) => nowX + (t - now) * scale;

  const start = now - past;
  const end = now + horizon;
  const recent = runs.filter(r => {
    const at = runAt(r);
    return at >= start && at <= now;
  });
  const due = fires.filter(f => f.at > now && f.at <= end);

  const pastColumns = width ? [...bucketize(recent, runAt, bucketMs)] : [];
  const futureColumns = width ? [...bucketize(due, f => f.at, bucketMs)] : [];
  // Clock labels need about 64px each; on a narrow strip every other one (or
  // every third) is dropped rather than letting them run into each other.
  const step = axisStep(past) * Math.max(1, Math.ceil(64 / (axisStep(past) * scale || 64)));
  const marks = width ? railAxis(start, end, step, new Date(now).getTimezoneOffset()) : [];

  const running = recent.filter(r => r.status === 'running');
  const failed = recent.filter(r => r.status === 'failed').length;
  const cometFrom = running.length
    ? Math.max(0, Math.min(nowX - 96, x(Math.min(...running.map(runAt)))))
    : 0;

  const runReadout = (run: RailRun): Readout => ({
    tone: run.status,
    lead: flowName.get(run.workflow_id) ?? `Workflow ${run.workflow_id}`,
    rest: [
      `#${run.id}`,
      STATUS_WORD[run.status] ?? run.status,
      run.duration_seconds != null ? formatDuration(run.duration_seconds) : null,
      timeAgo(run.started_at || run.created_at, now),
    ].filter(Boolean).join(' · '),
  });
  const fireReadout = (group: RailFire[]): Readout => {
    const names = group.map(f => f.name);
    return {
      tone: 'scheduled',
      lead: names.length > 2 ? `${names.slice(0, 2).join(', ')} +${names.length - 2}` : names.join(', '),
      rest: `${clock(group[0].at)} · ${untilLabel(group[0].at - now)}`,
    };
  };

  const column = (key: number): CSSProperties =>
    ({ transform: `translateX(${(x((key + 0.5) * bucketMs) - PITCH / 2).toFixed(2)}px)` });

  return (
    <section className={clsx('rail', running.length > 0 && 'is-live')}>
      <header className="rail-head">
        {hover ? (
          <p className="rail-readout is-hover" aria-hidden="true">
            <i className={`rail-dot is-${hover.tone}`} />
            <b>{hover.lead}</b>
            <span>{hover.rest}</span>
          </p>
        ) : (
          <p className="rail-readout">
            <b>{railWindowLabel(past)}</b>
            <span>
              {recent.length} run{recent.length === 1 ? '' : 's'}
              {failed > 0 && <em> · {failed} failed</em>}
              {running.length > 0 && <strong> · {running.length} running</strong>}
            </span>
          </p>
        )}
        <p className="rail-readout rail-readout--next">
          <b>{railHorizonLabel(horizon)}</b>
          <span>{due.length ? `${due.length} scheduled` : 'nothing scheduled'}</span>
        </p>
      </header>

      {/* Decorative for assistive tech: the summary above and the run table
          below carry the same facts in a form that can be read in order. */}
      <div className="rail-plot" ref={plotRef} aria-hidden="true" onMouseLeave={() => setHover(null)}>
        {width > 0 && (
          // Keyed on the measured width and the scale: a resize or a window
          // change re-lays the strip out in place instead of gliding there.
          <div className="rail-stage" key={`${width}:${past}`}>
            <span className="rail-line rail-line--past" style={{ width: nowX }} />
            <span className="rail-line rail-line--next" style={{ left: nowX }} />

            {running.length > 0 && (
              <span className="rail-comet" style={{ left: cometFrom, width: nowX - cometFrom }}>
                <CometCanvas kind="fill" />
              </span>
            )}

            {pastColumns.map(([key, items]) => {
              const stack = [...items].sort((a, b) => railWeight(a.status) - railWeight(b.status) || a.id - b.id);
              const shown = stack.length > ROWS ? stack.slice(0, ROWS - 1) : stack;
              return (
                <div key={`p${key}`} className="rail-col" style={column(key)}>
                  {shown.map(run => (
                    <Link key={run.id} to={`/runs/${run.id}`} tabIndex={-1}
                          className={`rail-dot is-${run.status}`}
                          onMouseEnter={() => setHover(runReadout(run))} />
                  ))}
                  {stack.length > shown.length && (
                    <span className="rail-more">+{stack.length - shown.length}</span>
                  )}
                </div>
              );
            })}

            {futureColumns.map(([key, group]) => {
              const shown = Math.min(group.length, group.length > ROWS ? ROWS - 1 : ROWS);
              return (
                <div key={`f${key}`} className="rail-col rail-col--next" style={column(key)}
                     onMouseEnter={() => setHover(fireReadout(group))}>
                  {Array.from({ length: shown }, (_, i) => <span key={i} className="rail-dot is-scheduled" />)}
                  {group.length > shown && <span className="rail-more">+{group.length - shown}</span>}
                </div>
              );
            })}

            {marks.map(t => (
              <span key={t} className={clsx('rail-mark', Math.abs(x(t) - nowX) < 34 && 'is-hidden')}
                    style={{ transform: `translateX(${x(t).toFixed(2)}px)` }}>
                <i />{clock(t)}
              </span>
            ))}

            <span className="rail-now" style={{ left: nowX }}>
              <b>now</b>
              <i />
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
