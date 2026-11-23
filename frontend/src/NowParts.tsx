import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import clsx from 'clsx';
import { ArrowRight, Play, RefreshCw, ShieldAlert } from 'lucide-react';
import { post } from './api';
import { formatDuration, timeAgo } from './format';
import { useNow } from './hooks';
import { Button, LoadingBar } from './components/ui';
import { useToast } from './components/toast';
import { usePeek } from './components/RunPeek';
import type { OpenApproval } from './components/ApprovalGate';
import type { Failing } from './pulseModel';
import { describe, type Service } from './Services';
import { toneOf } from './serviceStatus';
import type { Run, Workflow } from './types';

/* ─── The Now page's bands ─────────────────────────────────
   Below the verdict and the timetable, the home screen answers two more
   questions in order: what needs me, and what is moving. (The third — how has
   it been going — is the charts at the foot of the page.) */

type Attention =
  | { kind: 'service'; key: string; at: number; service: Service }
  | { kind: 'approval'; key: string; at: number; rows: OpenApproval[] }
  | { kind: 'failing'; key: string; at: number; item: Failing<Workflow, Run> };

/** Down before waiting before failing — the verdict's order — and the
 *  longest-standing first inside each kind. */
const KIND_RANK = { service: 0, approval: 1, failing: 2 } as const;
const SHOWN = 6;

/** Everything that needs a person, one card each, every card carrying the one
 *  thing to do about it. Renders nothing when nothing does. */
export function AttentionBand({ approvals, failing, services, onChanged }: {
  approvals: OpenApproval[] | null;
  failing: Failing<Workflow, Run>[];
  services: Service[] | null;
  onChanged: () => void;
}) {
  const now = useNow(true, 15_000);
  const [all, setAll] = useState(false);
  const items = useMemo<Attention[]>(() => {
    const list: Attention[] = [];
    for (const service of services ?? []) {
      if (toneOf(service) === 'down') {
        list.push({ kind: 'service', key: `s${service.id}`, at: Date.parse(service.state_since ?? '') || 0, service });
      }
    }
    const byRun = new Map<number, OpenApproval[]>();
    for (const row of approvals ?? []) byRun.set(row.run_id, [...(byRun.get(row.run_id) ?? []), row]);
    for (const [runId, rows] of byRun) {
      list.push({ kind: 'approval', key: `a${runId}`, at: Math.min(...rows.map(r => Date.parse(r.created_at))), rows });
    }
    for (const item of failing) {
      list.push({ kind: 'failing', key: `f${item.flow.id}`, at: Date.parse(item.run.finished_at || item.run.created_at), item });
    }
    return list.sort((a, b) => KIND_RANK[a.kind] - KIND_RANK[b.kind] || a.at - b.at);
  }, [approvals, failing, services]);

  if (!items.length) return null;
  const shown = all ? items : items.slice(0, SHOWN);
  return (
    <section className="band attention" aria-label="Needs you">
      <header className="band-head">
        <h2>Needs you</h2>
        <p>{items.length === 1 ? 'One thing' : `${items.length} things`}, the most urgent first</p>
      </header>
      <div className="attn-grid">
        {shown.map((item, index) => (
          <AttentionCard key={item.key} item={item} now={now} index={index} onChanged={onChanged} />
        ))}
      </div>
      {items.length > SHOWN && (
        <button type="button" className="text-link attn-more" onClick={() => setAll(v => !v)}>
          {all ? 'Show fewer' : `Show all ${items.length}`}
        </button>
      )}
    </section>
  );
}

function AttentionCard({ item, now, index, onChanged }: { item: Attention; now: number; index: number; onChanged: () => void }) {
  const { toast } = useToast();
  const peek = usePeek();
  const navTo = useNavigate();
  const [busy, setBusy] = useState(false);
  const act = async (path: string, said: string, then?: (body: Run) => void) => {
    setBusy(true);
    try {
      const body = await post<Run>(path, {});
      toast(said, 'info');
      onChanged();
      then?.(body);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'That did not work', 'error');
    } finally { setBusy(false); }
  };
  const style = { '--i': index } as React.CSSProperties;

  if (item.kind === 'service') {
    const s = item.service;
    return (
      <article className="attn-card is-down" style={style}>
        <p className="attn-kind"><i aria-hidden="true" />Service down</p>
        <h3><Link to={`/services/${s.id}`} title={s.name}>{s.name}</Link></h3>
        <p className="attn-line">{describe(s, now).text || 'It keeps crashing.'}</p>
        <div className="attn-actions">
          <Button size="sm" disabled={busy} onClick={() => act(`/services/${s.id}/restart`, `Restarting ${s.name}`)}>
            <RefreshCw size={12} /> Restart
          </Button>
          <Link className="text-link" to={`/services/${s.id}`}>Logs <ArrowRight size={12} /></Link>
        </div>
      </article>
    );
  }

  if (item.kind === 'approval') {
    const [first] = item.rows;
    const waited = Math.max(0, (now - item.at) / 1000);
    return (
      <article className="attn-card is-waiting" style={style}>
        <p className="attn-kind"><i aria-hidden="true" />Waiting for you · {formatDuration(waited)}</p>
        <h3><Link to={`/workflows/${first.workflow_id}`} title={first.workflow_name}>{first.workflow_name}</Link></h3>
        <p className="attn-line">
          <b>{first.task_name ?? 'A task'}</b> needs a decision before it runs
          {item.rows.length > 1 && ` (and ${item.rows.length - 1} more)`}
          {first.prompt && <>: <q>{first.prompt}</q></>}
        </p>
        <div className="attn-actions">
          <Button size="sm" onClick={() => navTo(`/runs/${first.run_id}`)}><ShieldAlert size={12} /> Review</Button>
          <button type="button" className="text-link" onClick={() => peek.open(first.run_id)}>Run #{first.run_id}</button>
        </div>
      </article>
    );
  }

  const { flow, run } = item.item;
  return (
    <article className="attn-card is-failing" style={style}>
      <p className="attn-kind"><i aria-hidden="true" />Failing · {timeAgo(run.finished_at || run.created_at, now)}</p>
      <h3><Link to={`/workflows/${flow.id}`} title={flow.name}>{flow.name}</Link></h3>
      <p className="attn-line">Its latest run failed, and it has not succeeded since.</p>
      <div className="attn-actions">
        <Button size="sm" disabled={busy}
                onClick={() => act(`/runs/${run.id}/retry`, `Retrying ${flow.name}`, fresh => peek.open(fresh.id))}>
          <RefreshCw size={12} /> Retry
        </Button>
        <button type="button" className="text-link" onClick={() => peek.open(run.id)}>What happened <ArrowRight size={12} /></button>
      </div>
    </article>
  );
}

/** The middle of a workflow's recent successful durations, in seconds. */
function typicalSeconds(runs: Run[], workflowId: number): number | null {
  const durations = runs
    .filter(r => r.workflow_id === workflowId && r.status === 'success' && r.duration_seconds != null)
    .slice(0, 9)
    .map(r => r.duration_seconds!)
    .sort((a, b) => a - b);
  return durations.length >= 3 ? durations[Math.floor(durations.length / 2)] : null;
}

const JUST_FINISHED = 6;

/** What is running, each against how long that workflow usually takes, and
 *  what has just finished. A run opens beside the page. */
export function InFlight({ runs, flows }: { runs: Run[]; flows: Workflow[] }) {
  const peek = usePeek();
  const live = runs.filter(r => r.status === 'running' || r.status === 'queued');
  const now = useNow(live.some(r => r.status === 'running'));
  const name = (id: number) => flows.find(w => w.id === id)?.name ?? `Workflow ${id}`;
  const running = live.filter(r => r.status === 'running')
    .sort((a, b) => Date.parse(a.started_at || a.created_at) - Date.parse(b.started_at || b.created_at));
  const queued = live.filter(r => r.status === 'queued');
  const finished = runs.filter(r => r.status === 'success' || r.status === 'failed' || r.status === 'cancelled')
    .slice(0, JUST_FINISHED);

  return (
    <section className="panel in-flight">
      <div className="panel-head">
        <div>
          <h2>In flight</h2>
          <p>{running.length || queued.length
            ? [running.length && `${running.length} running`, queued.length && `${queued.length} queued`].filter(Boolean).join(' · ')
            : 'Nothing running right now'}</p>
        </div>
        <Link className="panel-link" to="/runs?status=live">All live <ArrowRight size={13} /></Link>
      </div>

      {running.map(run => {
        const elapsed = Math.max(0, (now - Date.parse(run.started_at || run.created_at)) / 1000);
        const usual = typicalSeconds(runs, run.workflow_id);
        const over = usual ? elapsed / usual : 0;
        return (
          <button key={run.id} type="button" className={clsx('flight-row', over > 1.5 && 'is-long')} onClick={() => peek.open(run.id)}>
            <span className="flight-main">
              <b title={name(run.workflow_id)}>{name(run.workflow_id)}</b>
              <small>
                #{run.id} · {formatDuration(elapsed)}
                {usual != null && (over > 1.5
                  ? <em> · {over.toFixed(1)}× its usual {formatDuration(usual)}</em>
                  : ` · usually ${formatDuration(usual)}`)}
              </small>
            </span>
            {/* Against its usual duration: the comet fills toward it and turns
                amber once the run is running past it. */}
            <span className="flight-bar" aria-hidden="true">
              <span className="flight-fill" style={{ width: `${usual ? Math.min(100, (elapsed / usual) * 100) : 100}%` }}>
                <LoadingBar size="sm" overRatio={usual ? Math.max(0, Math.min(1, over - 1)) : undefined} />
              </span>
            </span>
          </button>
        );
      })}
      {queued.slice(0, 4).map(run => (
        <button key={run.id} type="button" className="flight-row is-queued" onClick={() => peek.open(run.id)}>
          <span className="flight-main">
            <b title={name(run.workflow_id)}>{name(run.workflow_id)}</b>
            <small>#{run.id} · queued {timeAgo(run.created_at, now)}</small>
          </span>
          <span className="flight-wait">waiting for a slot</span>
        </button>
      ))}
      {queued.length > 4 && <p className="flight-more">and {queued.length - 4} more queued</p>}
      {!live.length && (
        <p className="flight-empty"><Play size={13} /> Runs appear here the moment they start.</p>
      )}

      {finished.length > 0 && (
        <>
          <h3 className="flight-sub">Just finished</h3>
          {finished.map(run => (
            <button key={run.id} type="button" className={`done-row is-${run.status}`} onClick={() => peek.open(run.id)}>
              <i className="pipe-lamp" aria-hidden="true" />
              <b title={name(run.workflow_id)}>{name(run.workflow_id)}</b>
              <span>{run.duration_seconds != null ? formatDuration(run.duration_seconds) : run.status}</span>
              <small>{timeAgo(run.finished_at || run.created_at, now)}</small>
            </button>
          ))}
        </>
      )}
    </section>
  );
}
