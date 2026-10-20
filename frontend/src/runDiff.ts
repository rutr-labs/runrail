/* ─── Run comparison ─────────────────────────────────────────
   The shape of /runs/{id}/compare, and the sentences the run page builds
   from it: a headline of what differs, and which tasks are worth opening.
   The server decides what differs (runrail.rundiff); this decides what to
   say first. */

export interface RunRef { id: number; status: string; trigger: string; created_at: string; started_at: string | null; duration: number | null }
export interface ParamChange { name: string; change: 'added' | 'removed' | 'changed'; before?: unknown; after?: unknown }
export interface TaskSide { task_run_id: number; status: string; duration: number | null; exit_code: number | null; attempts: number; error: string | null }
export interface SourceChange {
  path: string; change: 'added' | 'removed' | 'modified' | 'missing' | 'found';
  diff?: string[]; note?: string; size_before?: number; size_after?: number;
}
export interface PackageChange { name: string; before: string | null; after: string | null }
export interface EnvironmentChange {
  name?: string; renamed?: { before: string; after: string }; python?: { before: string | null; after: string | null };
  rebuilt_at?: string; packages: PackageChange[]; packages_unknown?: boolean; before?: string | null; after?: string | null;
}
export interface LogLine { stream: string; line: number; text: string }
export interface LogChanges {
  new: LogLine[]; new_total: number; gone: LogLine[]; gone_total: number;
  changed: { stream: string; line: number; before: string; after: string }[]; changed_total: number;
  first_error: string | null; truncated: boolean;
}
export interface CellChange {
  before_index: number | null; after_index: number | null;
  change: 'added' | 'removed' | 'changed'; reasons?: ('code' | 'output' | 'failed' | 'slower')[];
  source?: string; source_diff?: string[]; output_before?: string[]; output_after?: string[];
  duration_before?: number | null; duration_after?: number | null; error?: string;
}
export interface NotebookChanges { cells?: CellChange[]; cells_before?: number; cells_after?: number; failed_cell?: number | null; missing?: string }
export interface TaskDiff {
  task_id: number; name: string; type: string | null;
  before: TaskSide | null; after: TaskSide | null;
  command?: { before: string; after: string };
  sources?: SourceChange[]; environment?: EnvironmentChange; inputs_unknown?: boolean;
  logs?: LogChanges; notebook?: NotebookChanges;
}
export interface RunDiff { run: RunRef; baseline: RunRef | null; note?: string; parameters?: ParamChange[]; tasks?: TaskDiff[] }

export type Tone = 'bad' | 'warn' | 'info';
export interface Point { tone: Tone; text: string }

const SLOWER_FACTOR = 2;
const SLOWER_SECONDS = 10;

/** "load.py" from "scripts/etl/load.py". */
export const fileName = (path: string) => path.split(/[\\/]/).pop() || path;

export function shortDuration(seconds: number | null | undefined): string {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Package lines as people say them: "pandas 2.1.0 → 2.2.1", "+ pyarrow 15.0.0". */
export function packageLine(p: PackageChange): string {
  const version = (line: string | null) => line?.split('==')[1] ?? line ?? '';
  if (!p.before) return `+ ${p.name} ${version(p.after)}`.trim();
  if (!p.after) return `− ${p.name} ${version(p.before)}`.trim();
  return `${p.name} ${version(p.before)} → ${version(p.after)}`;
}

export function slower(task: TaskDiff): boolean {
  const a = task.after?.duration, b = task.before?.duration;
  return a != null && b != null && b > 0 && a >= b * SLOWER_FACTOR && a - b >= SLOWER_SECONDS;
}

/** Whether a task differs in anything the page shows. */
export function taskChanged(task: TaskDiff): boolean {
  if (!task.before || !task.after) return true;
  const logs = task.logs;
  return task.before.status !== task.after.status || Boolean(task.command) || Boolean(task.sources?.length)
    || Boolean(task.environment) || Boolean(logs && (logs.new_total || logs.changed_total || logs.gone_total))
    || Boolean(task.notebook?.cells?.length || task.notebook?.missing) || slower(task);
}

/** The headline: what differs, most telling first. */
export function headline(diff: RunDiff): Point[] {
  if (!diff.baseline) return [];
  const points: Point[] = [];
  for (const task of diff.tasks ?? []) {
    if (task.after && task.before && task.after.status !== task.before.status && task.after.status === 'failed') {
      points.push({ tone: 'bad', text: `${task.name} failed (passed last time)` });
    }
    if (!task.before && task.after) points.push({ tone: 'info', text: `${task.name} is new` });
    if (task.before && !task.after) points.push({ tone: 'warn', text: `${task.name} did not run` });
  }
  const params = diff.parameters ?? [];
  if (params.length) {
    points.push({ tone: 'warn', text: params.length === 1 ? `parameter ${params[0].name} changed` : `${params.length} parameters changed` });
  }
  const seen = new Set<string>();
  for (const task of diff.tasks ?? []) {
    for (const source of task.sources ?? []) {
      const name = fileName(source.path);
      if (seen.has(name)) continue;
      seen.add(name);
      points.push({ tone: 'warn', text: source.change === 'modified' ? `${name} changed` : `${name} ${source.change}` });
    }
    const env = task.environment;
    if (env?.python) points.push({ tone: 'warn', text: `Python ${env.python.before ?? '?'} → ${env.python.after ?? '?'}` });
    if (env?.packages.length) {
      const key = env.packages.map(packageLine).join('|');
      if (!seen.has(key)) {
        seen.add(key);
        points.push({ tone: 'warn', text: env.packages.length === 1 ? packageLine(env.packages[0])
          : `${env.packages.length} packages changed (${packageLine(env.packages[0])}, …)` });
      }
    }
    if (task.command) points.push({ tone: 'info', text: `${task.name}: command changed` });
    if (task.notebook?.failed_cell) points.push({ tone: 'bad', text: `${task.name}: cell ${task.notebook.failed_cell} failed` });
    if (slower(task)) {
      points.push({ tone: 'warn', text: `${task.name} took ${shortDuration(task.after!.duration)} (was ${shortDuration(task.before!.duration)})` });
    }
    const logs = task.logs;
    if (logs?.new_total) points.push({ tone: 'info', text: `${task.name}: ${logs.new_total} new log line${logs.new_total === 1 ? '' : 's'}` });
  }
  return points;
}

/** Said when nothing above explains the difference. */
export function quietNote(diff: RunDiff): string | null {
  if (!diff.baseline || headline(diff).length) return null;
  return 'Nothing differs in parameters, code, packages or logs. The cause may be outside RunRail: the data, a database, the network.';
}

/** Said when the comparison is missing what older runs never recorded. */
export function inputsNote(diff: RunDiff): string | null {
  return diff.tasks?.some(t => t.inputs_unknown)
    ? 'Scripts and packages are not compared: one of these runs is from before RunRail recorded them.'
    : null;
}
