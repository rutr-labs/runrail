/* ─── The timetable: who gets a line, and in what order ─────
   The home screen draws one line per workflow on a shared time axis: what ran
   to the left of "now", what the schedule owes to the right. This file holds
   the decisions that are awkward to check by eye — which workflows earn a line
   when there are more of them than rows, the order they sit in, and what state
   a line is in. The drawing lives in components/Board.tsx. */

import { nextCronOccurrence } from './cron';
import { runAt, type RailRun } from './pulseRail';

export type BoardFlow = {
  id: number;
  name: string;
  enabled: boolean;
  schedule_cron?: string | null;
  schedule_timezone?: string | null;
  snoozed?: boolean;
  snooze_pauses_runs?: boolean;
};

export type BoardRun = RailRun & { finished_at?: string | null };

/** Whether the schedule will actually fire: enabled and not muted with pause. */
export const fires = (flow: BoardFlow): boolean =>
  flow.enabled && Boolean(flow.schedule_cron) && !(flow.snoozed && flow.snooze_pauses_runs);

/** Every fire each workflow owes between `now` and `now + horizon`, ascending. */
export function upcomingFires(
  flows: BoardFlow[], now: number, horizon: number, perFlow = 48,
): Map<number, number[]> {
  const owed = new Map<number, number[]>();
  const end = now + horizon;
  for (const flow of flows) {
    if (!fires(flow)) continue;
    const times: number[] = [];
    let cursor = new Date(now);
    for (let i = 0; i < perFlow; i++) {
      const next = nextCronOccurrence(flow.schedule_cron!, flow.schedule_timezone, cursor);
      if (!next || next.getTime() > end) break;
      times.push(next.getTime());
      // +1s, not +1min: a full minute would skip the very next occurrence of
      // an every-minute schedule.
      cursor = new Date(next.getTime() + 1000);
    }
    if (times.length) owed.set(flow.id, times);
  }
  return owed;
}

/** When a run stopped occupying its line: its finish, or `now` while live. */
export function runEnd(run: BoardRun, now: number): number {
  if (run.finished_at) return new Date(run.finished_at).getTime();
  if (run.duration_seconds != null) return runAt(run) + run.duration_seconds * 1000;
  return run.status === 'running' || run.status === 'waiting_approval' ? now : runAt(run);
}

export type LaneState = 'failing' | 'waiting' | 'running' | 'queued' | 'ok' | 'idle' | 'paused';

export type Lane<F extends BoardFlow, R extends BoardRun> = {
  flow: F;
  /** Runs that touch the window, oldest first. */
  runs: R[];
  /** Fires the schedule owes inside the horizon, ascending. */
  fires: number[];
  state: LaneState;
};

/** Needs a person, then moving, then merely present. */
const RANK: Record<LaneState, number> = {
  failing: 0, waiting: 1, running: 2, queued: 3, ok: 4, idle: 5, paused: 6,
};

export function laneState(flow: BoardFlow, runs: BoardRun[], failing: boolean): LaneState {
  if (runs.some(r => r.status === 'waiting_approval')) return 'waiting';
  if (failing) return 'failing';
  if (runs.some(r => r.status === 'running')) return 'running';
  if (runs.some(r => r.status === 'queued')) return 'queued';
  if (!flow.enabled || (flow.snoozed && flow.snooze_pauses_runs)) return 'paused';
  return runs.length ? 'ok' : 'idle';
}

/** The lines the board draws.
 *
 *  A workflow earns a line by having something to show: a run inside the
 *  window, a fire inside the horizon, or a failure nobody has cleared. When
 *  more earn one than there are rows, the rows go to whatever needs a person
 *  first, then to the busiest.
 *
 *  The order on screen is deliberately duller than the order of selection:
 *  lines that need a person sit on top, and every other line is alphabetical.
 *  Sorting by activity would reshuffle the board each time a run started,
 *  which on a busy install is every few seconds. */
export function buildLanes<F extends BoardFlow, R extends BoardRun>(
  flows: F[], runs: R[], owed: Map<number, number[]>,
  opts: { start: number; now: number; failing: Set<number>; max: number },
): { lanes: Lane<F, R>[]; quiet: number } {
  const byFlow = new Map<number, R[]>();
  for (const run of runs) {
    if (runAt(run) > opts.now || runEnd(run, opts.now) < opts.start) continue;
    const list = byFlow.get(run.workflow_id);
    if (list) list.push(run); else byFlow.set(run.workflow_id, [run]);
  }

  const candidates: Lane<F, R>[] = [];
  for (const flow of flows) {
    const mine = (byFlow.get(flow.id) ?? []).sort((a, b) => runAt(a) - runAt(b));
    const due = owed.get(flow.id) ?? [];
    const state = laneState(flow, mine, opts.failing.has(flow.id));
    if (!mine.length && !due.length && state !== 'failing') continue;
    candidates.push({ flow, runs: mine, fires: due, state });
  }

  const name = (a: Lane<F, R>, b: Lane<F, R>) =>
    a.flow.name.localeCompare(b.flow.name, undefined, { numeric: true });
  const chosen = [...candidates]
    .sort((a, b) =>
      RANK[a.state] - RANK[b.state]
      || b.runs.length - a.runs.length
      || (a.fires[0] ?? Infinity) - (b.fires[0] ?? Infinity)
      || name(a, b))
    .slice(0, opts.max);

  const urgent = (lane: Lane<F, R>) => (lane.state === 'failing' || lane.state === 'waiting' ? 0 : 1);
  chosen.sort((a, b) => urgent(a) - urgent(b) || (urgent(a) === 0 ? RANK[a.state] - RANK[b.state] : 0) || name(a, b));
  return { lanes: chosen, quiet: flows.length - chosen.length };
}

/** "04:12" under an hour, "2h 05m" past it — a countdown that keeps its width. */
export function countdown(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const two = (n: number) => String(n).padStart(2, '0');
  if (total < 3600) return `${two(Math.floor(total / 60))}:${two(total % 60)}`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours < 24) return `${hours}h ${two(minutes)}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}
