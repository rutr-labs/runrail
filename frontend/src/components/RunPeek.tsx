import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link, useLocation } from 'react-router-dom';
import { ArrowRight, RefreshCw, X } from 'lucide-react';
import { api, post } from '../api';
import { formatDate, formatDuration, timeAgo } from '../format';
import { useNow, useVisibleInterval } from '../hooks';
import { groupAttempts } from '../attempts';
import type { Run, TaskRun, Workflow } from '../types';
import { Button, StatusBadge } from './ui';
import { useToast } from './toast';

/* ─── Run peek ─────────────────────────────────────────────
   A run, read without leaving where you are. Clicking a run on the timetable
   or in a list slides this sheet in from the right: its verdict, its tasks,
   and — for the task that broke or is running — the last lines it wrote. The
   full run page is one click further for everything else.

   It exists because the common question about a run is "what happened?", and
   answering it should not cost you your place in the list you were reading. */

const LIVE = (status: string) =>
  status === 'running' || status === 'queued' || status === 'waiting_approval';

const TAIL_LINES = 14;

type Peek = { open: (runId: number) => void; close: () => void };
const PeekContext = createContext<Peek>({ open: () => {}, close: () => {} });
export const usePeek = () => useContext(PeekContext);

export function PeekProvider({ children }: { children: ReactNode }) {
  const [runId, setRunId] = useState<number | null>(null);
  const location = useLocation();
  // Going anywhere closes it: the sheet belongs to the page it was opened on.
  useEffect(() => setRunId(null), [location.pathname]);
  const close = useCallback(() => setRunId(null), []);
  const value = useMemo(() => ({ open: setRunId, close }), [close]);
  return (
    <PeekContext.Provider value={value}>
      {children}
      {runId != null && <RunPeek key={runId} runId={runId} onClose={close} />}
    </PeekContext.Provider>
  );
}

function sentence(run: Run, now: number): string {
  const took = run.duration_seconds != null ? formatDuration(run.duration_seconds)
    : run.started_at ? formatDuration((now - new Date(run.started_at).getTime()) / 1000) : '—';
  const ended = timeAgo(run.finished_at || run.started_at || run.created_at, now);
  switch (run.status) {
    case 'success': return `Succeeded in ${took} · ${ended}`;
    case 'failed': return `Failed after ${took} · ${ended}`;
    case 'running': return `Running for ${took}`;
    case 'queued': return `Queued ${timeAgo(run.created_at, now)}`;
    case 'waiting_approval': return 'Waiting for your approval';
    case 'cancelled': return `Cancelled ${ended}`;
    default: return run.status.replace(/_/g, ' ');
  }
}

/** The task worth reading first: the one that failed, else the one running. */
function focusTask(taskRuns: TaskRun[]): TaskRun | undefined {
  const newest = (status: string) => [...taskRuns].reverse().find(t => t.status === status);
  return newest('failed') ?? newest('running');
}

function RunPeek({ runId, onClose }: { runId: number; onClose: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [flow, setFlow] = useState<Workflow | null>(null);
  const [missing, setMissing] = useState(false);
  const [tail, setTail] = useState<{ taskRunId: number; text: string } | null>(null);
  const [closing, setClosing] = useState(false);
  const [busy, setBusy] = useState(false);
  const { toast } = useToast();
  const live = Boolean(run && LIVE(run.status));
  const now = useNow(live);

  const load = () => {
    api<Run>(`/runs/${runId}`).then(setRun).catch(() => setMissing(true));
  };
  useEffect(load, [runId]);
  useVisibleInterval(live, load, 2000);
  useEffect(() => {
    if (run) api<Workflow>(`/workflows/${run.workflow_id}`).then(setFlow).catch(() => {});
  }, [run?.workflow_id]);

  const close = () => {
    if (closing) return;
    setClosing(true);
    window.setTimeout(onClose, 180);
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      // A dialog opened over the sheet gets Escape first.
      if (document.querySelector('.modal-shade, .cmdk-shade')) return;
      e.preventDefault();
      close();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closing]);

  const groups = useMemo(() => groupAttempts(run?.task_runs ?? []), [run?.task_runs]);
  const focus = useMemo(() => focusTask(run?.task_runs ?? []), [run?.task_runs]);

  // The last lines of the task worth reading. stderr first — that is where a
  // failure says why — then stdout if stderr is empty.
  useEffect(() => {
    if (!focus) { setTail(null); return; }
    let cancelled = false;
    const read = (stream: string) =>
      fetch(`/api/task-runs/${focus.id}/${stream}`).then(r => (r.ok ? r.text() : '')).catch(() => '');
    void read('stderr').then(async text => {
      const body = text.trim() ? text : await read('stdout');
      if (cancelled) return;
      const lines = body.replace(/\s+$/, '').split('\n');
      setTail({ taskRunId: focus.id, text: lines.slice(-TAIL_LINES).join('\n') });
    });
    return () => { cancelled = true; };
    // Re-read while it runs; a finished task's output does not change.
  }, [focus?.id, focus?.status, focus?.status === 'running' ? Math.floor(now / 4000) : 0]);

  const act = async (path: string, done: string) => {
    setBusy(true);
    try {
      await post(path, {});
      toast(done, 'info');
      load();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'That did not work', 'error');
    } finally { setBusy(false); }
  };

  const name = flow?.name ?? (run ? `Workflow ${run.workflow_id}` : '');
  return createPortal(
    <div className={`peek-shade${closing ? ' closing' : ''}`} onMouseDown={e => e.target === e.currentTarget && close()}>
      <aside className={`peek${run ? ` is-${run.status}` : ''}`} role="dialog" aria-label={`Run ${runId}`}>
        <header className="peek-head">
          <p className="hero-eyebrow">
            Run #{runId}{run && <><i>·</i>{run.trigger_type}<i>·</i>{formatDate(run.created_at)}</>}
          </p>
          <button type="button" className="peek-close" onClick={close} aria-label="Close"><X size={16} /></button>
          {run ? (
            <>
              <h2><Link to={`/workflows/${run.workflow_id}`} title={name}>{name}</Link></h2>
              <p className="hero-status"><i aria-hidden="true" />{sentence(run, now)}</p>
              <div className="peek-actions">
                <Link className="btn btn-primary btn-md" to={`/runs/${run.id}`}>Open the full run <ArrowRight size={14} /></Link>
                {live
                  ? <Button variant="ghost" disabled={busy} onClick={() => act(`/runs/${run.id}/cancel`, 'Cancelling the run')}>Cancel</Button>
                  : <Button variant="ghost" disabled={busy} onClick={() => act(`/runs/${run.id}/retry`, 'A new run is queued')}
                            title="Queue a new run with this run's parameters">
                      <RefreshCw size={13} /> {run.status === 'failed' ? 'Retry' : 'Run again'}
                    </Button>}
              </div>
            </>
          ) : missing ? (
            <h2>This run is gone</h2>
          ) : (
            <div className="skeleton-line" style={{ height: 34, width: '70%', marginTop: 14 }} />
          )}
        </header>

        {run && (
          <div className="peek-body">
            <section>
              <h3>Tasks</h3>
              {groups.length === 0 && (
                <p className="peek-empty">{run.status === 'queued' ? 'Waiting for a worker to pick it up.' : 'No task has started.'}</p>
              )}
              <ol className="peek-tasks">
                {groups.map(group => {
                  const last = group.attempts[group.attempts.length - 1] ?? group.gates[group.gates.length - 1];
                  return (
                    <li key={group.key} className={`is-${last.status}`}>
                      <i aria-hidden="true" />
                      <Link to={`/runs/${run.id}#task-${encodeURIComponent(group.name)}`} title={group.name}>{group.name}</Link>
                      {group.attempts.length > 1 && <small>{group.attempts.length} attempts</small>}
                      <span>{last.duration_seconds != null ? formatDuration(last.duration_seconds) : last.status.replace(/_/g, ' ')}</span>
                    </li>
                  );
                })}
              </ol>
            </section>

            {focus && (
              <section>
                <h3>{focus.status === 'failed' ? 'Where it stopped' : 'What it is writing'}</h3>
                <p className="peek-note">
                  <StatusBadge value={focus.status} /> <b>{focus.task_name}</b>
                  {focus.exit_code != null && focus.status === 'failed' && <span>exit code {focus.exit_code}</span>}
                </p>
                {focus.error_message && <p className="peek-error">{focus.error_message}</p>}
                {tail?.taskRunId === focus.id && tail.text
                  ? <pre className="peek-log">{tail.text}</pre>
                  : <p className="peek-empty">{tail?.taskRunId === focus.id ? 'It has written nothing yet.' : 'Reading its output…'}</p>}
              </section>
            )}
          </div>
        )}
      </aside>
    </div>,
    document.body,
  );
}
