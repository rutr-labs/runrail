import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, MouseEvent, ReactNode } from 'react';
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
   read for: what is moving, what broke and when, and what happens next.

   Given a `range`, the same sheet draws a fixed stretch of time instead — the
   Runs page's timeline, where any day or week can be looked at. Nothing drifts
   then, and "now" is marked only if it falls inside. */

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
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
/** Label spacings for a fixed range, finest first; the first that leaves room
 *  for a label wins. */
const RANGE_STEPS = [15 * MINUTE, 30 * MINUTE, HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY];
const dayLabel = (t: number) =>
  new Date(t).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' });
const isMidnight = (t: number) => { const d = new Date(t); return d.getHours() === 0 && d.getMinutes() === 0; };
const stamp = (t: number) =>
  new Date(t).toLocaleString(undefined, { weekday: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

type Tip = { x: number; y: number; tone: string; title: string; lines: string[] };

export function Board<F extends BoardFlow, R extends BoardRun>({ runs, flows, complete, failing, onRun, onNowX, onOpenRun, max = 12, range, heading = 'Timetable', summary, moreLink = true }: {
  runs: R[];
  flows: F[];
  /** True when `runs` is the whole history rather than the newest page of it. */
  complete: boolean;
  /** Workflows whose latest verdict is a failure. */
  failing: Set<number>;
  /** Start a workflow from its line. Left out where nobody is at a keyboard. */
  onRun?: (id: number) => void;
  /** Where "now" falls, in px from the board's left edge — so the page around
   *  the board can line itself up on the same instant. */
  onNowX?: (x: number) => void;
  /** Open a run beside the board instead of leaving for its page. */
  onOpenRun?: (id: number) => void;
  max?: number;
  /** A fixed stretch of time to draw instead of the window around now. */
  range?: { start: number; end: number };
  heading?: ReactNode;
  /** Replaces the line under the heading. */
  summary?: ReactNode;
  /** Offer the full timeline under the sheet. */
  moreLink?: boolean;
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
  const fixed = range != null;
  const start = fixed ? range.start : now - past;
  const end = fixed ? range.end : now + horizon;
  // The latest instant a run can have started by: now, or the range's end.
  const upTo = Math.min(now, end);

  const owed = useMemo(
    () => (end > now ? upcomingFires(flows, minute * MINUTE, end - minute * MINUTE + MINUTE) : new Map<number, number[]>()),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [flows, minute, fixed ? end : horizon]);

  // Around now, the past takes two thirds and the future one third, at the
  // same px-per-ms: one axis, so a scheduled fire glides straight into the run
  // it becomes. A fixed range simply fills the width.
  const scale = fixed ? width / Math.max(1, end - start) : Math.round((width * 2) / 3) / past;   // px per ms
  const nowX = fixed ? (now > start && now < end ? Math.round((now - start) * scale) : null) : Math.round((width * 2) / 3);
  const beam = width && !fixed && nowX != null ? box.left + nowX : 0;
  useEffect(() => { if (beam) onNowX?.(beam); }, [beam]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Everything on the sheet is placed against a fixed origin and the sheet
  // itself is moved, so a tick is one transform rather than a hundred. A fixed
  // range does not move.
  const key = fixed ? `${width}:${start}:${end}` : `${width}:${past}`;
  const origin = useRef({ key: '', t: 0 });
  if (origin.current.key !== key) origin.current = { key, t: fixed ? start : now };
  const t0 = origin.current.t;
  const at = (t: number) => (t - t0) * scale;
  const drift: CSSProperties | undefined = fixed ? undefined : { transform: `translateX(${((nowX ?? 0) - at(now)).toFixed(2)}px)` };

  const { lanes, quiet } = useMemo(
    () => buildLanes(flows, runs, owed, { start, now: upTo, failing, max }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [flows, runs, owed, failing, max, start, end, Math.floor(now / TICK_MS)]);

  const step = fixed
    ? RANGE_STEPS.find(s => s * scale >= 88) ?? RANGE_STEPS[RANGE_STEPS.length - 1]
    : axisStep(past) * Math.max(1, Math.ceil(72 / (axisStep(past) * scale || 72)));
  const marks = width ? railAxis(start - step, end + step, step, new Date(now).getTimezoneOffset()) : [];
  const markLabel = (t: number) => (fixed && (step >= DAY || isMidnight(t)) ? dayLabel(t) : clock(t));

  const inWindow = runs.filter(r => runAt(r) >= start && runAt(r) <= upTo);
  const failedCount = inWindow.filter(r => r.status === 'failed').length;
  const dueCount = [...owed.values()].reduce((sum, times) => sum + times.filter(t => t <= end).length, 0);
  // Per line, for a fixed range: how many ran and how many of those failed.
  const tally = (lane: (typeof lanes)[number]) => {
    const mine = lane.runs.filter(r => runAt(r) >= start);
    return { n: mine.length, failed: mine.filter(r => r.status === 'failed').length };
  };

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
          <h2>{heading}</h2>
          <p>
            {summary ?? (
              <>
                {fixed ? '' : `${railWindowLabel(past)} · `}{inWindow.length.toLocaleString()} run{inWindow.length === 1 ? '' : 's'}
                {failedCount > 0 && <em> · {failedCount} failed</em>}
                {end > now && (fixed
                  ? <span> · {dueCount || 'nothing'} scheduled ahead</span>
                  : <span> · {railHorizonLabel(horizon).toLowerCase()} · {dueCount || 'nothing'} scheduled</span>)}
              </>
            )}
          </p>
        </div>
        <ul className="board-legend" aria-hidden="true">
          <li><i className="bar is-success" />Succeeded</li>
          <li><i className="bar is-failed" />Failed</li>
          <li><i className="bar is-running" />Running</li>
          {end > now && <li><i className="fire" />Scheduled</li>}
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
                  <span key={t} className={clsx('board-mark', nowX != null && Math.abs(at(t) - at(now)) < 40 && 'is-hidden',
                                                fixed && isMidnight(t) && 'is-day')}
                        style={{ left: at(t) }}>{markLabel(t)}</span>
                ))}
              </div>
            )}
          </div>
          <span className="lane-next">{fixed ? 'Runs' : 'Next'}</span>
        </div>

        {lanes.map((lane, index) => {
          const next = lane.fires[0] ?? null;
          return (
            <div key={lane.flow.id} className={clsx('lane', `is-${lane.state}`)} style={{ '--i': index } as CSSProperties}>
              <div className="lane-name">
                <i className="lane-signal" aria-hidden="true" />
                <Link to={`/workflows/${lane.flow.id}`} title={lane.flow.name}>{lane.flow.name}</Link>
                {onRun && (
                  <button type="button" className="lane-run" onClick={() => onRun(lane.flow.id)}
                          title={`Run ${lane.flow.name} now`} aria-label={`Run ${lane.flow.name} now`}>
                    <Play size={11} />
                  </button>
                )}
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
                                  fixed ? `started ${stamp(runAt(run))}` : `started ${timeAgo(run.started_at || run.created_at, now)}`,
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
              {fixed ? (() => {
                const { n, failed } = tally(lane);
                return (
                  <span className="lane-next" title={`${n} run${n === 1 ? '' : 's'} in this range${failed ? `, ${failed} failed` : ''}`}>
                    {n.toLocaleString()}{failed > 0 && <em> · {failed}</em>}
                  </span>
                );
              })() : (
                <span className="lane-next" title={next ? `Next run at ${clock(next)}` : undefined}>
                  {next ? countdown(next - now) : '—'}
                </span>
              )}
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
            {nowX != null && (
              <>
                <span className="board-future" style={{ left: nowX }} />
                <span className="board-now" style={{ left: nowX }}><b>{clock(now)}</b></span>
              </>
            )}
          </div>
        )}

        {tip && (
          <div className={`board-tip is-${tip.tone}`} style={{ left: tip.x, top: tip.y }} role="presentation">
            <b>{tip.title}</b>
            {tip.lines.filter(Boolean).map(line => <span key={line}>{line}</span>)}
          </div>
        )}

        {lanes.length === 0 && (
          <p className="board-empty">{fixed
            ? 'Nothing ran in this stretch of time.'
            : 'Nothing ran in this window and nothing is scheduled. Runs appear here the moment they start.'}</p>
        )}
      </div>

      {(quiet > 0 || (moreLink && !fixed)) && (
        <footer className="board-foot">
          <span>{quiet > 0 && (fixed
            ? `${quiet} workflow${quiet === 1 ? '' : 's'} had nothing in this range`
            : `${quiet} quiet workflow${quiet === 1 ? '' : 's'} not shown`)}</span>
          {fixed
            ? <Link to="/workflows">All workflows <ArrowRight size={13} /></Link>
            : <Link to="/runs?view=timeline">Earlier runs on the timeline <ArrowRight size={13} /></Link>}
        </footer>
      )}
    </section>
  );
}
