/* ─── Chain ────────────────────────────────────────────────
   Where a workflow sits between others: what it runs after (each with its
   last result, and whether it has succeeded since this one last started) and
   what runs after it. GET /workflows/{id}/chain.

   The drawing of it is the pipeline on the workflow page (components/
   Pipeline.tsx), whose start and end nodes are these workflows; this file
   holds the shape of the data and the one sentence that says where the
   chain stands. */

export interface ChainUpstream {
  id: number;
  name: string;
  enabled: boolean;
  last_run_id: number | null;
  last_status: string | null;
  last_finished_at: string | null;
  /** Succeeded since this workflow last started. */
  done: boolean;
}

export interface ChainInfo {
  after: ChainUpstream[];
  then: { id: number; name: string }[];
  queued: boolean;
}

/** One line on where the chain stands, or null for a workflow with no
 *  upstreams. Exported for the tests. */
export function chainSummary(info: ChainInfo, enabled: boolean): string | null {
  if (!info.after.length) return null;
  if (!enabled) return 'Switched off: it will not start on its own.';
  if (info.queued) return 'A run is queued.';
  const pending = info.after.filter(u => !u.done);
  if (!pending.length) return 'Every upstream has succeeded; starting.';
  const reasons = pending.map(u =>
    !u.enabled ? `${u.name} (switched off)`
    : u.last_status === 'failed' ? `${u.name} (its last run failed)`
    : u.last_status === 'running' ? `${u.name} (running now)`
    : u.name);
  return `Waiting for ${reasons.join(', ')}.`;
}
