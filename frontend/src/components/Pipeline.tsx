import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import { Clock, Hand, ShieldAlert, Workflow as WorkflowIcon } from 'lucide-react';
import { formatDuration } from '../format';
import { LoadingBar } from './ui';
import { PIPE_PAD as PAD, pipeColumns, pipeSinks, pipeSizes, type PipeTask } from '../pipeline';

export type { PipeTask };

/* ─── Pipeline ─────────────────────────────────────────────
   A workflow drawn as what it is: something starts it, tasks hand off to
   one another, and something may follow. Read left to right like everything
   else here. A task's column is one past its deepest dependency, so parallel
   branches stack; given a run's task statuses, the drawing is lit by that run
   — finished hand-offs solid, the one in progress carrying a spark toward the
   task that is running.

   Nodes are HTML over an SVG of edges, so names truncate, badges and the
   comet render as they do everywhere else, and each task is a real button. */

/** What starts the pipeline, or what follows it: a schedule, another
 *  workflow, or a person. */
export type PipeEnd = {
  key: string;
  kind: 'clock' | 'flow' | 'hand';
  label: string;
  sub?: ReactNode;
  tone?: 'success' | 'failed' | 'running' | 'waiting';
  to?: string;
};

const NODE_H = 86;
const END_H = 58;
const GAP_Y = 16;

const TYPE_GLYPH: Record<string, string> = { shell: '›_', python: 'Py', notebook: 'Nb', sql: 'SQL' };

const STATUS_WORD: Record<string, string> = {
  success: 'Succeeded', failed: 'Failed', running: 'Running', queued: 'Queued', skipped: 'Skipped',
  cancelled: 'Cancelled', awaiting_approval: 'Awaiting approval', approved: 'Approved', rejected: 'Rejected',
};

type Box = { x: number; y: number; w: number; h: number };

const curve = (a: Box, b: Box) => {
  const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
  const bend = Math.max(24, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
};

const END_ICON = { clock: Clock, flow: WorkflowIcon, hand: Hand };

function EndNode({ end, box, side }: { end: PipeEnd; box: Box; side: 'start' | 'end' }) {
  const Icon = END_ICON[end.kind];
  const style: CSSProperties = { left: box.x, top: box.y, width: box.w, height: box.h };
  const body = (
    <>
      <span className="pipe-end-icon"><Icon size={15} /></span>
      <span className="pipe-end-text">
        <b title={end.label}>{end.label}</b>
        {end.sub && <small>{end.sub}</small>}
      </span>
    </>
  );
  const cls = clsx('pipe-end', `pipe-end--${side}`, `pipe-end--${end.kind}`, end.tone && `is-${end.tone}`);
  return end.to
    ? <Link className={cls} style={style} to={end.to}>{body}</Link>
    : <div className={cls} style={style}>{body}</div>;
}

export function Pipeline({ tasks, statuses, durations, starts = [], ends = [], selected, onSelect, label = 'Task pipeline' }: {
  tasks: PipeTask[];
  /** Task name → status in the run the drawing is lit by. */
  statuses?: Record<string, string>;
  /** Task name → seconds, in that run. */
  durations?: Record<string, number | null | undefined>;
  starts?: PipeEnd[];
  ends?: PipeEnd[];
  selected?: string | null;
  onSelect?: (name: string) => void;
  label?: string;
}) {
  // How much room there is decides how roomy the drawing can be.
  const scrollRef = useRef<HTMLDivElement>(null);
  const [room, setRoom] = useState(0);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => {
      const style = getComputedStyle(el);
      setRoom(el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [tasks.length === 0]);

  const plan = useMemo(() => {
    const columns = pipeColumns(tasks);
    const { node: NODE_W, end: END_W, gap: GAP_X } = pipeSizes(
      columns.length, (starts.length ? 1 : 0) + (ends.length ? 1 : 0), room || 1200);
    const tallest = Math.max(
      ...columns.map(c => c.length * NODE_H + (c.length - 1) * GAP_Y),
      starts.length * END_H + (starts.length - 1) * GAP_Y,
      ends.length * END_H + (ends.length - 1) * GAP_Y,
      NODE_H,
    );
    const height = tallest + PAD * 2;
    const stack = (count: number, h: number) => (height - (count * h + (count - 1) * GAP_Y)) / 2;

    let x = PAD;
    const startBoxes = starts.map((_, i) => ({ x, y: stack(starts.length, END_H) + i * (END_H + GAP_Y), w: END_W, h: END_H }));
    if (starts.length) x += END_W + GAP_X;
    const boxes = new Map<string, Box>();
    for (const column of columns) {
      column.forEach((name, i) => boxes.set(name, { x, y: stack(column.length, NODE_H) + i * (NODE_H + GAP_Y), w: NODE_W, h: NODE_H }));
      x += NODE_W + GAP_X;
    }
    const endBoxes = ends.map((_, i) => ({ x, y: stack(ends.length, END_H) + i * (END_H + GAP_Y), w: END_W, h: END_H }));
    const width = (ends.length ? x + END_W : x - GAP_X) + PAD;
    return { boxes, startBoxes, endBoxes, width, height, columns };
    // The ends are compared by what they are, not by identity: a parent
    // rebuilds those arrays on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasks, starts.length, ends.length, room]);

  if (!tasks.length) return null;
  const { boxes, startBoxes, endBoxes, width, height } = plan;
  const lit = Boolean(statuses && Object.keys(statuses).length);
  const sinks = pipeSinks(tasks);
  const roots = tasks.filter(t => !t.depends_on.some(d => boxes.has(d)));

  // Every hand-off: from what starts it, between tasks, and on to what follows.
  type Edge = { key: string; d: string; done: boolean; live: boolean };
  const edges: Edge[] = [];
  startBoxes.forEach((from, i) => roots.forEach(task => {
    const status = statuses?.[task.name];
    edges.push({
      key: `s${i}->${task.name}`, d: curve(from, boxes.get(task.name)!),
      done: lit && status != null && status !== 'queued', live: status === 'running',
    });
  }));
  for (const task of tasks) {
    for (const dep of task.depends_on) {
      const from = boxes.get(dep);
      if (!from) continue;
      edges.push({
        key: `${dep}->${task.name}`, d: curve(from, boxes.get(task.name)!),
        done: statuses?.[dep] === 'success' || statuses?.[dep] === 'approved',
        live: statuses?.[task.name] === 'running',
      });
    }
  }
  endBoxes.forEach((to, i) => sinks.forEach(name => {
    edges.push({ key: `${name}->e${i}`, d: curve(boxes.get(name)!, to), done: statuses?.[name] === 'success', live: false });
  }));

  return (
    <div className="pipe-scroll" ref={scrollRef}>
      <div className={clsx('pipe', lit && 'is-lit')} style={{ width, height }} role="group" aria-label={label}>
        <svg className="pipe-edges" width={width} height={height} aria-hidden="true">
          {edges.map(edge => (
            <g key={edge.key}>
              <path d={edge.d} className={clsx('pipe-edge', edge.done && 'is-done', edge.live && 'is-live')} />
              {/* A spark rides the hand-off into whatever is running now. */}
              {edge.live && (
                <circle r="3.5" className="pipe-spark">
                  <animateMotion dur="1.5s" repeatCount="indefinite" path={edge.d} />
                </circle>
              )}
            </g>
          ))}
        </svg>

        {starts.map((end, i) => <EndNode key={end.key} end={end} box={startBoxes[i]} side="start" />)}
        {ends.map((end, i) => <EndNode key={end.key} end={end} box={endBoxes[i]} side="end" />)}

        {tasks.map(task => {
          const box = boxes.get(task.name)!;
          const status = statuses?.[task.name];
          const seconds = durations?.[task.name];
          const cls = clsx('pipe-node', status ? `is-${status}` : 'is-idle', selected === task.name && 'is-selected',
            onSelect && 'is-clickable');
          const style: CSSProperties = { left: box.x, top: box.y, width: box.w, height: box.h };
          const body = (
            <>
              <span className="pipe-node-top">
                <span className="task-type-badge">{TYPE_GLYPH[task.task_type] ?? '?'}</span>
                <b title={task.name}>{task.name}</b>
                {task.gate && <ShieldAlert size={13} className="pipe-gate" aria-label="Waits for approval" />}
                <i className="pipe-lamp" aria-hidden="true" />
              </span>
              <span className="pipe-node-detail" title={task.detail ?? undefined}>{task.detail || task.task_type}</span>
              <span className="pipe-node-foot">
                <span>{status ? (STATUS_WORD[status] ?? status.replace(/_/g, ' ')) : lit ? 'Not reached' : task.task_type}</span>
                {seconds != null && <span>{formatDuration(seconds)}</span>}
                {Boolean(task.retries) && !status && <span>{task.retries} {task.retries === 1 ? 'retry' : 'retries'}</span>}
              </span>
              {status === 'running' && <LoadingBar size="sm" className="pipe-node-comet" />}
            </>
          );
          return onSelect
            ? <button key={task.name} type="button" className={cls} style={style} aria-pressed={selected === task.name}
                      onClick={() => onSelect(task.name)}>{body}</button>
            : <div key={task.name} className={cls} style={style}>{body}</div>;
        })}
      </div>
    </div>
  );
}
