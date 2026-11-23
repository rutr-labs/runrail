/* ─── Pipeline: where things go ────────────────────────────
   The arithmetic behind components/Pipeline.tsx, kept apart from the drawing
   so it can be tested without a browser: which column a task sits in, the
   order inside a column, where the pipeline ends, and how wide everything can
   afford to be. */

/** Padding around the drawing, in px. */
export const PIPE_PAD = 10;
const PAD = PIPE_PAD;

export type PipeTask = {
  name: string;
  task_type: string;
  depends_on: string[];
  /** One line of what it runs: the command or the script path. */
  detail?: string | null;
  /** Parks the run for a person before it starts. */
  gate?: boolean;
  retries?: number;
};

/** Node, end and gap widths for `columns` task columns in `room` px. The
 *  drawing is roomy when it can be and tightens in steps before it scrolls:
 *  a five-task pipeline should be read whole, not panned across. */
export function pipeSizes(columns: number, ends: number, room: number): { node: number; end: number; gap: number } {
  const steps = [{ end: 196, gap: 68 }, { end: 176, gap: 52 }, { end: 156, gap: 40 }, { end: 144, gap: 30 }];
  let last = { node: 164, ...steps[steps.length - 1] };
  for (const step of steps) {
    const gaps = Math.max(0, columns + ends - 1) * step.gap;
    const node = Math.floor((room - PAD * 2 - ends * step.end - gaps) / Math.max(1, columns));
    if (node >= 204) return { node: Math.min(244, node), ...step };
    last = { node: Math.max(164, node), ...step };
  }
  return last;
}

/** Columns of task names, left to right, each ordered to keep hand-offs from
 *  crossing more than they must. */
export function pipeColumns(tasks: PipeTask[]): string[][] {
  const byName = new Map(tasks.map(t => [t.name, t]));
  const level = new Map<string, number>();
  const levelOf = (name: string, seen: Set<string>): number => {
    if (level.has(name)) return level.get(name)!;
    if (seen.has(name)) return 0;               // cycle safety — the API rejects real cycles
    seen.add(name);
    const deps = (byName.get(name)?.depends_on ?? []).filter(d => byName.has(d));
    const value = deps.length ? 1 + Math.max(...deps.map(d => levelOf(d, seen))) : 0;
    level.set(name, value);
    return value;
  };
  tasks.forEach(t => levelOf(t.name, new Set()));

  const columns: string[][] = [];
  for (const task of tasks) (columns[level.get(task.name) ?? 0] ??= []).push(task.name);
  // Each column after the first is sorted by where its dependencies sit in
  // the columns before it (their mean row), so a branch stays level with the
  // task it came from.
  const row = new Map<string, number>();
  columns.forEach((column, i) => {
    if (i > 0) {
      const weight = (name: string) => {
        const rows = (byName.get(name)?.depends_on ?? []).map(d => row.get(d)).filter((r): r is number => r != null);
        return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Infinity;
      };
      const order = new Map(column.map((name, index) => [name, index]));
      column.sort((a, b) => weight(a) - weight(b) || order.get(a)! - order.get(b)!);
    }
    column.forEach((name, index) => row.set(name, index - (column.length - 1) / 2));
  });
  return columns.filter(Boolean);
}

/** Tasks nothing else waits for: where the pipeline ends. */
export function pipeSinks(tasks: PipeTask[]): string[] {
  const awaited = new Set(tasks.flatMap(t => t.depends_on));
  return tasks.filter(t => !awaited.has(t.name)).map(t => t.name);
}
