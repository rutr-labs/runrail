import { Link } from 'react-router-dom';
import { Activity, CheckCircle2, Clock, X } from 'lucide-react';

/* ─── Chain strip ──────────────────────────────────────────
   Where a workflow sits between others: what it runs after (each with its
   last result, and whether it has succeeded since this one last started) and
   what runs after it. It answers "why hasn't this run yet?" on the page
   where the question comes up. GET /workflows/{id}/chain. */

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

function UpstreamChip({ upstream }: { upstream: ChainUpstream }) {
  const state = upstream.done ? 'done'
    : upstream.last_status === 'running' ? 'running'
    : upstream.last_status === 'failed' ? 'failed' : 'waiting';
  const icon = state === 'done' ? <CheckCircle2 size={12} />
    : state === 'running' ? <Activity size={12} />
    : state === 'failed' ? <X size={12} /> : <Clock size={12} />;
  const when = upstream.last_finished_at
    ? new Date(upstream.last_finished_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : null;
  const title = [
    state === 'done' ? 'Succeeded since this workflow last started'
      : state === 'waiting' ? 'Not succeeded since this workflow last started' : null,
    upstream.last_status ? `Last run ${upstream.last_status}${when ? ` · ${when}` : ''}` : 'No runs yet',
  ].filter(Boolean).join('\n');
  return (
    <Link className={`chain-chip ${state}`} to={`/workflows/${upstream.id}`} title={title}>
      {icon}<span>{upstream.name}</span>
    </Link>
  );
}

export function ChainStrip({ info, enabled }: { info: ChainInfo; enabled: boolean }) {
  if (!info.after.length && !info.then.length) return null;
  const summary = chainSummary(info, enabled);
  return (
    <div className="chain-strip">
      {info.after.length > 0 && (
        <div className="chain-row">
          <span className="chain-label">Runs after</span>
          <div className="chain-chips">
            {info.after.map(u => <UpstreamChip key={u.id} upstream={u} />)}
          </div>
          {summary && <small className="chain-summary">{summary}</small>}
        </div>
      )}
      {info.then.length > 0 && (
        <div className="chain-row">
          <span className="chain-label">Then</span>
          <div className="chain-chips">
            {info.then.map(w => (
              <Link key={w.id} className="chain-chip next" to={`/workflows/${w.id}`}><span>{w.name}</span></Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
