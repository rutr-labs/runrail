/* ─── Attempts ─────────────────────────────────────────────
   A task that retried, or that ran again after a resume, leaves several rows
   with the same name. Every surface that lists them — the task cards, the
   notebook reports, the timeline — used to print that name once per row, so
   "build_report, build_report" was all a reader had to tell them apart. This is
   the one place that decides how attempts are grouped, ordered and named, so
   those surfaces cannot drift into disagreeing. */

export interface AttemptLike {
  id: number;
  task_id?: number | null;
  task_name?: string | null;
  attempt: number;
  resume_index?: number | null;
  status: string;
}

/** Approval decisions are rows too (attempt 0). They are a decision about a
 *  task, not an execution of it, so they never count as an attempt. */
export const GATE_STATUSES = new Set(['awaiting_approval', 'approved', 'rejected']);
export const isGateRow = (row: { status: string }) => GATE_STATUSES.has(row.status);

/** Execution order: resume segment first, then the attempt within it. */
export const byAttempt = (a: AttemptLike, b: AttemptLike) =>
  (a.resume_index ?? 0) - (b.resume_index ?? 0) || a.attempt - b.attempt || a.id - b.id;

/** "Attempt 2", or — once a run has been resumed — which segment it was in,
 *  because attempt numbering starts again after a resume. */
export function attemptLabel(row: AttemptLike, resumed: boolean): string {
  const segment = row.resume_index ?? 0;
  if (!resumed || segment === 0) return `Attempt ${row.attempt}`;
  return `Resume ${segment} · attempt ${row.attempt}`;
}

/** Short form for tight spaces such as a timeline bar. */
export function attemptTag(row: AttemptLike, resumed: boolean): string {
  const segment = row.resume_index ?? 0;
  return resumed && segment > 0 ? `R${segment}·${row.attempt}` : `#${row.attempt}`;
}

export interface AttemptGroup<T extends AttemptLike> {
  key: string;
  name: string;
  /** Executions, oldest first. The last one is what the task ended as. */
  attempts: T[];
  /** Approval decisions for this task, oldest first. */
  gates: T[];
  /** True when any row came from a resumed segment. */
  resumed: boolean;
}

/** Rows grouped per task, in the order the tasks first appeared. */
export function groupAttempts<T extends AttemptLike>(rows: T[]): AttemptGroup<T>[] {
  const groups = new Map<string, AttemptGroup<T>>();
  for (const row of rows) {
    const key = row.task_id != null ? `id:${row.task_id}` : `name:${row.task_name ?? row.id}`;
    let group = groups.get(key);
    if (!group) {
      group = { key, name: row.task_name ?? `Task #${row.task_id ?? row.id}`,
                attempts: [], gates: [], resumed: false };
      groups.set(key, group);
    }
    (isGateRow(row) ? group.gates : group.attempts).push(row);
    if ((row.resume_index ?? 0) > 0) group.resumed = true;
  }
  for (const group of groups.values()) {
    group.attempts.sort(byAttempt);
    group.gates.sort(byAttempt);
  }
  return [...groups.values()];
}
