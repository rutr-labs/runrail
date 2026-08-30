import { useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';
import { api } from '../api';
import { formatDuration } from '../format';

/* ─── Duration trend sparkline ─────────────────────────────
   A task's recent SUCCESSFUL durations, with the median marked.

   The "slower than usual" verdict is never computed here. The server owns the
   rule (api/routes_insights.py::_task_stats — median plus a MAD-derived
   spread, and three separate guards so a 0.3s → 1.1s task never cries wolf)
   and returns `slow` next to the numbers it used. This component mirrors that
   rule for *drawing* only and states the server's own reasoning back in plain
   language, so the picture and the API can never disagree.

   Two failure modes this deliberately avoids:
     • inventing a threshold — a sparkline that decides for itself what is
       slow will contradict the flag the backend shows elsewhere;
     • accusing a task that has barely run — too little history renders as an
       explicit blank rail, never as a suspicion. */

/** Mirrors SLOW_SIGMA in api/routes_insights.py. Used solely to shade the band
 *  the server's own rule treats as unusual; the verdict still comes from
 *  `series.slow`. */
export const SLOW_SIGMA = 3;

/** Mirrors SLOW_MIN_SAMPLES in api/routes_insights.py — the server refuses to
 *  call anything slow below this, and the tooltip says so. */
export const SLOW_MIN_SAMPLES = 5;

/** Below two points there is no line to draw, only a dot; the placeholder says
 *  that plainly instead of implying a trend. */
export const MIN_SPARK_SAMPLES = 2;

/* GET /api/workflows/{id}/task-durations */
export interface TaskDurationSample {
  task_run_id: number;
  workflow_run_id: number;
  duration_seconds: number;
  created_at: string;
}

export interface TaskDurationSeries {
  task_id: number;
  task_name: string | null;
  /** Oldest first — the server orders them so no client-side reversal is needed. */
  samples: TaskDurationSample[];
  median: number;
  p90: number;
  /** MAD-derived, already floored by the server. */
  spread: number;
  last: number;
  slow: boolean;
  slow_ratio: number | null;
}

/** One fetch per workflow, indexed both ways: the workflow page has Task rows
 *  (task_id), the run page has TaskRun rows (task_id too, but task_name is the
 *  stable key when a task was recreated). Tasks with no successful history are
 *  simply absent from the response — `undefined` here means "no data", which
 *  is exactly what TrendSpark renders as a blank. */
export function useTaskDurations(workflowId?: number | string | null, windowSize = 20) {
  const [rows, setRows] = useState<TaskDurationSeries[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (workflowId === undefined || workflowId === null || workflowId === '') {
      setRows([]);
      return;
    }
    let stale = false; // ignore out-of-order responses when the id changes fast
    setError(null);
    api<TaskDurationSeries[]>(`/workflows/${workflowId}/task-durations?window=${windowSize}`)
      .then(data => { if (!stale) setRows(data); })
      .catch(e => {
        if (stale) return;
        setRows([]);
        setError(e instanceof Error ? e.message : 'Could not load duration history');
      });
    return () => { stale = true; };
  }, [workflowId, windowSize, nonce]);

  const byTaskId = useMemo(
    () => new Map((rows ?? []).map(r => [r.task_id, r])), [rows]);
  const byTaskName = useMemo(
    () => new Map((rows ?? []).flatMap(r => (r.task_name ? [[r.task_name, r] as const] : []))), [rows]);

  return { byTaskId, byTaskName, loading: rows === null, error, reload: () => setNonce(n => n + 1) };
}

type SparkSize = 'sm' | 'md';
const SIZES: Record<SparkSize, { w: number; h: number; r: number }> = {
  sm: { w: 76, h: 20, r: 2 },    // inline in the workflow task list
  md: { w: 116, h: 30, r: 2.6 }, // run page task cards, where there is room
};

export interface TrendSparkProps {
  /** `undefined` = the task has no successful history; renders the blank rail. */
  series?: TaskDurationSeries;
  /** Used in the tooltip; falls back to the series' own name. */
  taskName?: string;
  size?: SparkSize;
  /** Mark one specific task run on the line (the run page passes its own
   *  TaskRun id). A run that failed or is still going is not in the history at
   *  all, so an absent id simply goes unmarked. */
  highlightTaskRunId?: number;
  /** Show the median as text beside the line. */
  showLabel?: boolean;
  /** Show the "slower than usual" chip; defaults on for `md`. */
  showFlag?: boolean;
  className?: string;
}

export function TrendSpark({
  series, taskName, size = 'sm', highlightTaskRunId,
  showLabel = false, showFlag, className,
}: TrendSparkProps) {
  const box = SIZES[size];
  const samples = series?.samples ?? [];
  const label = taskName ?? series?.task_name ?? 'This task';

  /* Not enough history: a neutral rail and a sentence explaining the absence.
     Never a warning colour, never a shrug that reads as a verdict. */
  if (!series || samples.length < MIN_SPARK_SAMPLES) {
    const tip = samples.length === 1
      ? `${label}: one successful run so far (${formatDuration(samples[0].duration_seconds)}). Not enough history to show a trend.`
      : `${label}: no successful runs recorded yet, so there is nothing to compare against.`;
    return (
      <span className={clsx('trend-spark', `trend-spark--${size}`, 'trend-spark--empty', className)}
            title={tip}>
        <svg className="trend-spark-svg" width={box.w} height={box.h}
             viewBox={`0 0 ${box.w} ${box.h}`} role="img" aria-label={tip}>
          <line className="trend-spark-rail" x1={1.5} y1={box.h / 2} x2={box.w - 1.5} y2={box.h / 2} />
          {samples.length === 1 && (
            <circle className="trend-spark-dot" cx={box.w / 2} cy={box.h / 2} r={box.r} />
          )}
        </svg>
        {showLabel && (
          <span className="trend-spark-label trend-spark-label--muted">
            {samples.length === 1 ? '1 run' : 'No history'}
          </span>
        )}
      </span>
    );
  }

  const values = samples.map(s => s.duration_seconds);
  const top = Math.max(...values);
  const bottom = Math.min(...values);
  const flat = top === bottom; // a task that always takes the same time draws a centre line
  const padY = 3;
  const plotH = box.h - padY * 2;
  const yOf = (value: number) =>
    flat ? box.h / 2 : padY + (1 - (Math.min(Math.max(value, bottom), top) - bottom) / (top - bottom)) * plotH;
  const xOf = (index: number) => 1.5 + (index * (box.w - 3)) / (samples.length - 1);

  const points = samples.map((s, i) => `${xOf(i).toFixed(1)},${yOf(s.duration_seconds).toFixed(1)}`).join(' ');
  const medianY = yOf(series.median);
  // The server's own boundary. Drawn only when some sample actually crosses it,
  // so a healthy task is not decorated with a band it never approaches.
  const threshold = series.median + SLOW_SIGMA * series.spread;
  const bandH = threshold < top ? yOf(threshold) : 0;

  const marked = highlightTaskRunId == null
    ? -1
    : samples.findIndex(s => s.task_run_id === highlightTaskRunId);
  const lastIndex = samples.length - 1;
  /* `slow` describes the NEWEST sample only. While a viewer is looking at an
     older run, painting the whole spark amber would pin the latest run's
     slowness on the run in front of them. */
  const slow = series.slow && (marked === -1 || marked === lastIndex);
  const tip = describe(series, label, marked, slow);

  return (
    <span className={clsx('trend-spark', `trend-spark--${size}`, slow && 'trend-spark--slow', className)}
          title={tip}>
      <svg className="trend-spark-svg" width={box.w} height={box.h}
           viewBox={`0 0 ${box.w} ${box.h}`} role="img" aria-label={tip}>
        {bandH > 0 && (
          <rect className="trend-spark-band" x={0} y={0} width={box.w} height={bandH} />
        )}
        <polygon className="trend-spark-area"
                 points={`${points} ${(box.w - 1.5).toFixed(1)},${box.h} 1.5,${box.h}`} />
        <line className="trend-spark-median" x1={0} y1={medianY} x2={box.w} y2={medianY} />
        <polyline className="trend-spark-line" points={points} />
        <circle className="trend-spark-last" cx={xOf(lastIndex)} cy={yOf(series.last)} r={box.r} />
        {marked >= 0 && marked !== lastIndex && (
          <circle className="trend-spark-mark" cx={xOf(marked)}
                  cy={yOf(samples[marked].duration_seconds)} r={box.r + 1} />
        )}
      </svg>
      {showLabel && (
        <span className="trend-spark-label">
          {formatDuration(series.median)}<em> median</em>
        </span>
      )}
      {(showFlag ?? size === 'md') && slow && (
        <span className="trend-spark-flag">slower than usual</span>
      )}
    </span>
  );
}

/** Plain-language explanation of what the server said — never a claim this
 *  component made up on its own. */
function describe(series: TaskDurationSeries, label: string, marked: number, slow: boolean): string {
  const count = series.samples.length;
  const typical = `typically ${formatDuration(series.median)} across ${count} successful run${count === 1 ? '' : 's'}`;
  const lines: string[] = [];

  if (marked >= 0) {
    lines.push(`${label}: this run took ${formatDuration(series.samples[marked].duration_seconds)}; ${typical}.`);
    if (marked !== series.samples.length - 1) {
      lines.push(`The most recent successful run took ${formatDuration(series.last)}.`);
    }
  } else {
    lines.push(`${label}: last successful run took ${formatDuration(series.last)}; ${typical}.`);
  }

  if (slow) {
    lines.push(
      `Flagged slower than usual: more than ${SLOW_SIGMA}× the usual spread `
      + `(±${formatDuration(series.spread)}) above the median`
      + (series.slow_ratio ? `, and ${series.slow_ratio}× the median` : '') + '.');
  } else if (count < SLOW_MIN_SAMPLES) {
    lines.push(`Fewer than ${SLOW_MIN_SAMPLES} successful runs — too little history to call anything unusual.`);
  }
  lines.push('Only successful runs count towards the baseline.');
  return lines.join('\n');
}
