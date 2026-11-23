/* ─── The verdict ──────────────────────────────────────────
   One answer to "is everything all right?", worked out once and used by the
   whole app: the sentence on the home screen, the signal in the dock, and the
   colour of the light behind every page all read it from here, so they can
   never disagree. */

const DAY = 86_400_000;

type VerdictRun = {
  id: number;
  workflow_id: number;
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
};

type VerdictFlow = { id: number; name: string };

export type Mood = 'calm' | 'waiting' | 'failing' | 'down';

export type Failing<F extends VerdictFlow, R extends VerdictRun> = { flow: F; run: R };

/** Workflows whose most recent verdict is a failure, newest failure first.
 *
 *  "Failing" is about where a workflow stands now, not how many runs failed
 *  today: one that failed at nine and has succeeded every hour since is fine,
 *  and one that failed once an hour ago and has not run again is not. A run
 *  still in flight has no verdict yet, and a cancelled one never gets one, so
 *  both are looked past.
 *
 *  `runs` is newest first, as the API serves it. A failure older than `within`
 *  is history rather than a state, and stops counting. */
export function failingFlows<F extends VerdictFlow, R extends VerdictRun>(
  runs: R[], flows: F[], now: number, within = DAY,
): Failing<F, R>[] {
  const byId = new Map(flows.map(flow => [flow.id, flow]));
  const judged = new Set<number>();
  const failing: Failing<F, R>[] = [];
  for (const run of runs) {
    if (judged.has(run.workflow_id)) continue;
    if (run.status !== 'success' && run.status !== 'failed') continue;
    judged.add(run.workflow_id);
    if (run.status !== 'failed') continue;
    const at = new Date(run.finished_at || run.started_at || run.created_at).getTime();
    const flow = byId.get(run.workflow_id);
    if (flow && now - at <= within) failing.push({ flow, run });
  }
  return failing;
}

const settled = (run: VerdictRun) => run.status === 'success' || run.status === 'failed';

/** Workflows that failed within `within` but have no settled run in the
 *  newest page — so the page alone cannot say whether they recovered. Capped,
 *  because each one costs a request. */
export function unjudgedFailures(
  recent: VerdictRun[], failed: VerdictRun[], now: number, within = DAY, cap = 12,
): number[] {
  const judged = new Set(recent.filter(settled).map(run => run.workflow_id));
  const ask = new Set<number>();
  for (const run of failed) {
    const at = new Date(run.finished_at || run.started_at || run.created_at).getTime();
    if (now - at <= within && !judged.has(run.workflow_id)) ask.add(run.workflow_id);
  }
  return [...ask].slice(0, cap);
}

/** Several lists of runs as one, newest first, each run once. Run ids only
 *  grow, so the id is the order. */
export function newestFirst<R extends VerdictRun>(...lists: R[][]): R[] {
  const byId = new Map<number, R>();
  for (const list of lists) for (const run of list) if (!byId.has(run.id)) byId.set(run.id, run);
  return [...byId.values()].sort((a, b) => b.id - a.id);
}

/** What needs a person, in the order they should hear about it. */
export function moodOf(state: { down: number; waiting: number; failing: number }): Mood {
  if (state.down) return 'down';
  if (state.waiting) return 'waiting';
  if (state.failing) return 'failing';
  return 'calm';
}

const count = (n: number, one: string, many: string) => (n === 1 ? one : `${n} ${many}`);

/** The home screen's headline. */
export function verdictLine(state: { down: number; waiting: number; failing: number }): string {
  switch (moodOf(state)) {
    case 'down': return `${count(state.down, 'A service is', 'services are')} down.`;
    case 'waiting': return `${count(state.waiting, 'A run is', 'runs are')} waiting for you.`;
    case 'failing': return `${count(state.failing, 'A workflow is', 'workflows are')} failing.`;
    default: return 'Everything is on track.';
  }
}

/** The same verdict in two or three words, for the dock. */
export function signalLabel(state: { down: number; waiting: number; failing: number }): string {
  switch (moodOf(state)) {
    case 'down': return `${state.down} down`;
    case 'waiting': return `${state.waiting} waiting`;
    case 'failing': return `${state.failing} failing`;
    default: return 'All clear';
  }
}
