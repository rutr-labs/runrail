import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, MouseEvent } from 'react';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import { ArrowRight, Play } from 'lucide-react';
import { formatDuration, timeAgo } from '../format';
import { useNow } from '../hooks';
import {
  axisStep, railAxis, railHorizonLabel, railWindow, railWindowLabel, runAt, untilLabel,
} from '../pulseRail';
import { buildLanes, countdown, runEnd, upcomingFires, type BoardFlow, type BoardRun } from '../board';
import { CometCanvas } from './ui';

/* ─── The timetable ────────────────────────────────────────
   One line per workflow on one shared clock. Left of "now" is what ran — a bar
   from each run's start to its finish, coloured by how it ended; right of
   "now" are the fires the schedule still owes. The whole sheet drifts left as
   time passes, so a window left open keeps telling the truth.

   It is the home screen's answer to three questions a run table makes you
   read for: what is moving, what broke and when, and what happens next. */

const TICK_MS = 5000;     // how often the sheet re-anchors to the clock
const MINUTE = 60_000;
const MIN_BAR = 5;        // px — a run too short to have a width is still a mark
/** How many runs the sheet can hold before its lines blur: spread over a dozen
 *  lines, far more than a single strip could. */
const LEGIBLE_RUNS = 170;

const STATUS_WORD: Record<string, string> = {
  success: 'Succeeded', failed: 'Failed', running: 'Running', queued: 'Queued',
  cancelled: 'Cancelled', waiting_approval: 'Waiting for approval', skipped: 'Skipped',
};

const clock = (t: number) =>
  new Date(t).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });

type Tip = { x: number; y: number; tone: string; title: string; lines: string[] };

export function Board<F extends BoardFlow, R extends BoardRun>({ runs, flows, complete, failing, onRun, onNowX, onOpenRun, max = 12 }: {
  runs: R[];
  flows: F[];
  /** True when `runs` is the whole history rather than the newest page of it. */
  complete: boolean;
  /** Workflows whose latest verdict is a failure. */
  failing: Set<number>;
  onRun: (id: number) => void;
  /** Where "now" falls, in px from the board's left edge — so the page around
   *  the board can line itself up on the same instant. */
  onNowX?: (x: number) => void;
  /** Open a run beside the board instead of leaving for its page. */
  onOpenRun?: (id: number) => void;
  max?: number;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState({ left: 0, width: 0 });
  const [tip, setTip] = useState<Tip | null>(null);
  const now = useNow(true, TICK_MS);
  const minute = Math.floor(now / MINUTE);

  useLayoutEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    // offsetLeft / clientWidth: layout values, which an ancestor's entrance
    // transform cannot skew the way getBoundingClientRect would.
    // The line can overhang the board by its hover padding, so its own offset
    // is part of where the track starts.
    const measure = () => setBox({
      left: el.offsetLeft + ((el.offsetParent as HTMLElement | null)?.offsetLeft ?? 0),
      width: el.clientWidth,
    });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const { width } = box;

  // The scale is latched: it only moves when the run count clearly leaves the
  // band the current window suits (see railWindow).
  const windowRef = useRef<number | undefined>(undefined);
  const past = useMemo(() => {
    const next = railWindow(runs, now, complete, windowRef.current, LEGIBLE_RUNS);
    windowRef.current = next;
    return next;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs, complete, minute]);
  const horizon = past / 2;

  const owed = useMemo(
    () => upcomingFires(flows, minute * MINUTE, horizon + MINUTE),
    [flows, minute, horizon]);

  // Past takes two thirds, the future one third, at the same px-per-ms: one
  // axis, so a scheduled fire glides straight into the run it becomes.
  const nowX = Math.round((width * 2) / 3);
  const scale = nowX / past;                     // px per ms
  const start = now - past;
  const end = now + horizon;
  const beam = width ? box.left + nowX : 0;
  useEffect(() => { if (beam) onNowX?.(beam); }, [beam]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Everything on the sheet is placed against a fixed origin and the sheet
  // itself is moved, so a tick is one transform rather than a hundred.
  const key = `${width}:${past}`;
  const origin = useRef({ key: '', t: 0 });
  if (origin.current.key !== key) origin.current = { key, t: now };
  const t0 = origin.current.t;
  const at = (t: number) => (t - t0) * scale;
  const drift: CSSProperties = { transform: `translateX(${(nowX - at(now)).toFixed(2)}px)` };

  const { lanes, quiet } = useMemo(
    () => buildLanes(flows, runs, owed, { start, now, failing, max }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [flows, runs, owed, failing, max, Math.floor(now / TICK_MS)]);

  const step = axisStep(past) * Math.max(1, Math.ceil(72 / (axisStep(past) * scale || 72)));
  const marks = width ? railAxis(start - step, end + step, step, new Date(now).getTimezoneOffset()) : [];

  const inWindow = runs.filter(r => runAt(r) >= start && runAt(r) <= now);
  const failedCount = inWindow.filter(r => r.status === 'failed').length;
  const dueCount = [...owed.values()].reduce((sum, times) => sum + times.filter(t => t <= end).length, 0);

  const showTip = (event: MouseEvent, tone: string, title: string, lines: string[]) => {
    const body = bodyRef.current;
    if (!body) return;
    const host = body.getBoundingClientRect();
    const mark = event.currentTarget.getBoundingClientRect();
    setTip({
      x: Math.min(Math.max(mark.left + mark.width / 2 - host.left, 120), host.width - 120),
      y: mark.top - host.top,
      tone, title, lines,
    });
  };

  return (
    <section className="board" aria-label="Timetable">
      <header className="board-head">
        <div className="board-title">
          <h2>Timetable</h2>
          <p>
            {railWindowLabel(past)} · {inWindow.length} run{inWindow.length === 1 ? '' : 's'}
            {failedCount > 0 && <em> · {failedCount} failed</em>}
            <span> · {railHorizonLabel(horizon).toLowerCase()} · {dueCount || 'nothing'} scheduled</span>
          </p>
        </div>
        <ul className="board-legend" aria-hidden="true">
          <li><i className="bar is-success" />Succeeded</li>
          <li><i className="bar is-failed" />Failed</li>
          <li><i className="bar is-running" />Running</li>
          <li><i className="fire" />Scheduled</li>
        </ul>
      </header>

      <div className="board-body" ref={bodyRef} onMouseLeave={() => setTip(null)}>
        {/* The clock: its own row, so the labels never sit on a line. */}
        <div className="lane lane--axis" aria-hidden="true">
          <span className="lane-name" />
          <div className="lane-track" ref={trackRef}>
            {width > 0 && (
              <div className="lane-stage" key={key} style={drift}>
                {marks.map(t => (
                  <span key={t} className={clsx('board-mark', Math.abs(at(t) - at(now)) < 40 && 'is-hidden')}
                        style={{ left: at(t) }}>{clock(t)}</span>
                ))}
              </div>
            )}
          </div>
          <span className="lane-next">Next</span>
        </div>

        {lanes.map((lane, index) => {
          const next = lane.fires[0] ?? null;
          return (
            <div key={lane.flow.id} className={clsx('lane', `is-${lane.state}`)} style={{ '--i': index } as CSSProperties}>
              <div className="lane-name">
                <i className="lane-signal" aria-hidden="true" />
                <Link to={`/workflows/${lane.flow.id}`} title={lane.flow.name}>{lane.flow.name}</Link>
                <button type="button" className="lane-run" onClick={() => onRun(lane.flow.id)}
                        title={`Run ${lane.flow.name} now`} aria-label={`Run ${lane.flow.name} now`}>
                  <Play size={11} />
                </button>
              </div>
              {/* Decorative for assistive tech: the feed under the board says
                  the same things in a form that can be read in order. */}
              <div className="lane-track" aria-hidden="true">
                {width > 0 && (
                  <div className="lane-stage" key={key} style={drift}>
                    {lane.runs.map(run => {
                      const from = Math.max(runAt(run), start - 10 * MINUTE);
                      const live = run.status === 'running' || run.status === 'waiting_approval';
                      const span = Math.max(MIN_BAR, at(runEnd(run, now)) - at(from));
                      return (
                        <Link key={run.id} to={`/runs/${run.id}`} tabIndex={-1}
                              className={clsx('bar', `is-${run.status}`, live && 'is-live')}
                              style={{ left: at(from), width: span }}
                              onClick={e => {
                                // A modified click still opens the run's page in a new tab.
                                if (!onOpenRun || e.metaKey || e.ctrlKey || e.shiftKey) return;
                                e.preventDefault();
                                setTip(null);
                                onOpenRun(run.id);
                              }}
                              onMouseEnter={e => showTip(e, run.status, lane.flow.name, [
                                `#${run.id} · ${STATUS_WORD[run.status] ?? run.status}`,
                                [
                                  run.duration_seconds != null ? formatDuration(run.duration_seconds) : null,
                                  `started ${timeAgo(run.started_at || run.created_at, now)}`,
                                ].filter(Boolean).join(' · '),
                              ])}>
                          {run.status === 'running' && <CometCanvas kind="fill" />}
                        </Link>
                      );
                    })}
                    {lane.fires.filter(t => t > now && t <= end + step).map(t => (
                      <span key={t} className="fire" style={{ left: at(t) }}
                            onMouseEnter={e => showTip(e, 'scheduled', lane.flow.name,
                              [`Scheduled for ${clock(t)}`, untilLabel(t - now)])} />
                    ))}
                  </div>
                )}
              </div>
              <span className="lane-next" title={next ? `Next run at ${clock(next)}` : undefined}>
                {next ? countdown(next - now) : '—'}
              </span>
            </div>
          );
        })}

        {/* One overlay for what every line shares: the hour rules, the shaded
            future and the now-line. */}
        {width > 0 && (
          <div className="board-overlay" aria-hidden="true" style={{ left: box.left, width }}>
            <div className="lane-stage" key={key} style={drift}>
              {marks.map(t => <i key={t} className="board-rule" style={{ left: at(t) }} />)}
            </div>
            <span className="board-future" style={{ left: nowX }} />
            <span className="board-now" style={{ left: nowX }}><b>{clock(now)}</b></span>
          </div>
        )}

        {tip && (
          <div className={`board-tip is-${tip.tone}`} style={{ left: tip.x, top: tip.y }} role="presentation">
            <b>{tip.title}</b>
            {tip.lines.filter(Boolean).map(line => <span key={line}>{line}</span>)}
          </div>
        )}

        {lanes.length === 0 && (
          <p className="board-empty">Nothing ran in this window and nothing is scheduled. Runs appear here the moment they start.</p>
        )}
      </div>

      {quiet > 0 && (
        <footer className="board-foot">
          <span>{quiet} quiet workflow{quiet === 1 ? '' : 's'} not shown</span>
          <Link to="/workflows">All workflows <ArrowRight size={13} /></Link>
        </footer>
      )}
    </section>
  );
}
