import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import {
  SkipForward, RotateCcw, Undo2, Timer, CheckCircle2, XCircle, MinusCircle,
  CornerDownRight, AlertTriangle, RefreshCw, Info, GitBranch,
} from 'lucide-react';
import { api, post } from '../api';
import { Button, CancelButton, Modal, LoadingBar } from './ui';
import { useToast } from './toast';

/* ─── Resume ───────────────────────────────────────────────
   Resuming re-executes a failed run in place: same run id, same `ds`, same
   run key, same artifacts directory — only what did not succeed runs again.
   That is a destructive-sounding word for a careful operation, so the dialog
   shows the plan BEFORE anything happens: what is kept, what re-runs, and why
   each task re-runs.

   The plan is advisory (the worker recomputes it when it claims the run), and
   it is recomputed server-side on every force toggle: forcing one task also
   pushes everything downstream of it out of the reuse set, and watching that
   happen is how the dialog teaches the dependency rule. */

export interface ResumePlanReuse {
  task: string;
  task_run_id: number;
  duration_seconds: number | null;
}

export interface ResumePlanRerun {
  task: string;
  /** One of: 'failed' | 'you chose to' | 'upstream re-running' | 'did not run'. */
  reason: string;
}

export interface ResumePlan {
  resumable: boolean;
  reuse: ResumePlanReuse[];
  rerun: ResumePlanRerun[];
  seconds_reused: number;
}

function formatSpan(seconds?: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

type ReasonIcon = typeof XCircle;

const REASONS: Record<string, { label: string; tone: string; icon: ReasonIcon; hint: string }> = {
  'failed': {
    label: 'Failed', tone: 'danger', icon: XCircle,
    hint: 'This task errored last time — the resume runs it again.',
  },
  'you chose to': {
    label: 'You forced it', tone: 'accent', icon: RotateCcw,
    hint: 'You pulled this task out of the reuse set.',
  },
  'upstream re-running': {
    label: 'Upstream re-running', tone: 'warning', icon: CornerDownRight,
    hint: 'Something it depends on re-runs, so its earlier success no longer proves anything.',
  },
  'did not run': {
    label: 'Never ran', tone: 'queued', icon: MinusCircle,
    hint: 'Skipped, cancelled, or still waiting on a gate when the run stopped.',
  },
};

const describe = (reason: string) =>
  REASONS[reason] ?? { label: reason, tone: 'queued', icon: MinusCircle, hint: '' };

export interface ResumeDialogProps {
  runId: number;
  /** Current run status — lets the dialog explain a non-resumable run precisely. */
  runStatus?: string;
  /** Enables an "Open workflow" link when the task graph no longer resolves. */
  workflowId?: number;
  onClose: () => void;
  /** Called with the reopened run after a successful resume. */
  onResumed?: (run: { id: number; status: string }) => void;
}

export function ResumeDialog({ runId, runStatus, workflowId, onClose, onResumed }: ResumeDialogProps) {
  const { toast } = useToast();
  const [plan, setPlan] = useState<ResumePlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);
  const [forced, setForced] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const query = useMemo(
    () => forced.map(task => `rerun=${encodeURIComponent(task)}`).join('&'),
    [forced]
  );

  const settled = useRef(false);
  const load = useCallback(() => {
    let alive = true;
    // The previous plan stays on screen while a toggle recomputes, so the
    // lists never collapse and reflow under the pointer.
    if (settled.current) setReloading(true);
    api<ResumePlan>(`/runs/${runId}/resume-plan${query ? `?${query}` : ''}`)
      .then(next => { if (!alive) return; settled.current = true; setPlan(next); setPlanError(null); })
      .catch(error => {
        if (!alive) return;
        setPlanError(error instanceof Error ? error.message : 'Could not read the resume plan');
      })
      .finally(() => { if (alive) { setLoading(false); setReloading(false); } });
    return () => { alive = false; };
  }, [runId, query]);

  useEffect(load, [load]);

  const force = (task: string) => setForced(current => [...current, task]);
  const unforce = (task: string) => setForced(current => current.filter(name => name !== task));

  const submit = async () => {
    setBusy(true);
    setSubmitError(null);
    try {
      const run = await post<{ id: number; status: string }>(`/runs/${runId}/resume`, { rerun: forced });
      toast(`Run #${runId} resumed — ${plan?.rerun.length ?? 0} task${plan?.rerun.length === 1 ? '' : 's'} re-running`);
      onResumed?.(run);
      onClose();
    } catch (error) {
      // 409 when someone resumed it first, or the run left failed/cancelled.
      setSubmitError(error instanceof Error ? error.message : 'Could not resume this run');
      load();
    } finally {
      setBusy(false);
    }
  };

  const reuse = plan?.reuse ?? [];
  const rerun = plan?.rerun ?? [];
  const blocked = Boolean(planError) || (plan != null && !plan.resumable);

  return (
    <Modal
      wide
      title={`Resume run #${runId}`}
      subtitle="Pick the run back up in place — only what did not succeed runs again."
      onClose={onClose}
    >
      <div className="modal-body resume-body">
        <div className="resume-compare">
          <div className="resume-compare-card is-chosen">
            <b><SkipForward size={13} /> Resume · this dialog</b>
            <span>
              Keeps run #{runId}. Same <code>ds</code>, same run key, same artifacts folder.
              Successful tasks are reused; the rest execute again.
            </span>
          </div>
          <div className="resume-compare-card">
            <b><RefreshCw size={13} /> Retry · the other button</b>
            <span>
              Queues a <em>new</em> run with a new id and today's date, and starts every task
              from scratch with this run's parameters.
            </span>
          </div>
        </div>

        {loading ? (
          <div className="resume-loading">
            <div style={{ width: 220 }}><LoadingBar /></div>
            <p>Working out what a resume would do…</p>
          </div>
        ) : planError ? (
          <div className="resume-blocked resume-blocked--graph">
            <AlertTriangle size={16} />
            <div>
              <b>This run's task graph no longer resolves</b>
              <p>
                The workflow was edited since the run started, and a resume executes the
                workflow as it is <em>now</em>:
              </p>
              <pre>{planError}</pre>
              <p>
                Fix the dependencies on the workflow, or use <b>Retry</b> to start a fresh run.
              </p>
              {workflowId != null && (
                <Link className="btn btn-ghost btn-sm" to={`/workflows/${workflowId}`}>
                  <GitBranch size={12} /> Open workflow
                </Link>
              )}
            </div>
          </div>
        ) : plan && !plan.resumable ? (
          <div className="resume-blocked">
            <Info size={16} />
            <div>
              <b>Run #{runId} is {runStatus ?? 'not resumable'}</b>
              <p>
                Only a failed or cancelled run can be resumed — a live run is still going, and a
                successful one has nothing left to do. Use <b>Retry</b> for a fresh run.
              </p>
            </div>
          </div>
        ) : (
          <>
            <div className={clsx('resume-savings', !reuse.length && 'resume-savings--none')}>
              <Timer size={15} />
              {reuse.length ? (
                <span>
                  Reuses <b>{reuse.length}</b> finished task{reuse.length === 1 ? '' : 's'} —
                  about <b>{formatSpan(plan?.seconds_reused ?? 0)}</b> of work this run does not repeat.
                  <b> {rerun.length}</b> task{rerun.length === 1 ? '' : 's'} will execute.
                </span>
              ) : (
                <span>
                  Nothing can be reused — all <b>{rerun.length}</b> task{rerun.length === 1 ? '' : 's'} execute again.
                </span>
              )}
            </div>

            <div className={clsx('resume-columns', reloading && 'is-reloading')}>
              <section className="resume-col resume-col--reuse">
                <h4>
                  <CheckCircle2 size={13} /> Reused
                  <span className="resume-count">{reuse.length}</span>
                </h4>
                <p className="resume-col-sub">Kept from the earlier attempt — not executed again.</p>
                {reuse.length ? (
                  <ul className="resume-rows">
                    {reuse.map(item => (
                      <li key={item.task} className="resume-row resume-row--reuse">
                        <CheckCircle2 size={12} className="resume-row-icon" />
                        <span className="resume-row-name" title={item.task}>{item.task}</span>
                        <span className="resume-row-dur">{formatSpan(item.duration_seconds)}</span>
                        <button
                          type="button"
                          className="resume-row-action"
                          onClick={() => force(item.task)}
                          title={`Force ${item.task} to run again`}
                        >
                          <RotateCcw size={11} /> Re-run
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="resume-col-empty">No task from this run can be carried over.</p>
                )}
              </section>

              <section className="resume-col resume-col--rerun">
                <h4>
                  <RotateCcw size={13} /> Will re-run
                  <span className="resume-count">{rerun.length}</span>
                </h4>
                <p className="resume-col-sub">Executed again when you resume, in dependency order.</p>
                {rerun.length ? (
                  <ul className="resume-rows">
                    {rerun.map(item => {
                      const reason = describe(item.reason);
                      const Icon = reason.icon;
                      const chosen = item.reason === 'you chose to';
                      return (
                        <li key={item.task} className={clsx('resume-row', 'resume-row--rerun', chosen && 'is-forced')}>
                          <Icon size={12} className={clsx('resume-row-icon', `tone-${reason.tone}`)} />
                          <span className="resume-row-name" title={item.task}>{item.task}</span>
                          <span className={clsx('resume-reason', `tone-${reason.tone}`)} title={reason.hint}>
                            {reason.label}
                          </span>
                          {chosen && (
                            <button
                              type="button"
                              className="resume-row-action"
                              onClick={() => unforce(item.task)}
                              title={`Put ${item.task} back in the reuse set`}
                            >
                              <Undo2 size={11} /> Keep
                            </button>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p className="resume-col-empty">Nothing left to run — this run has no unfinished work.</p>
                )}
              </section>
            </div>

            {forced.length > 0 && (
              <p className="resume-hint">
                <CornerDownRight size={12} />
                Forcing a task also re-runs everything downstream of it — a stale success
                below a re-run is not evidence about the new result.
              </p>
            )}

            <div className="callout resume-note">
              This plan is advisory: the worker recomputes it when it claims the run, so a
              workflow edited in between executes the newer graph.
            </div>
          </>
        )}

        {submitError && (
          <div className="resume-error" role="alert">
            <AlertTriangle size={14} /> {submitError}
          </div>
        )}

        <div className="modal-actions">
          <CancelButton />
          <Button onClick={submit} disabled={busy || loading || blocked || !plan}>
            <SkipForward size={13} />
            {busy ? 'Resuming…' : rerun.length ? `Resume — run ${rerun.length} task${rerun.length === 1 ? '' : 's'}` : 'Resume run'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/* ─── Convenience trigger ──────────────────────────────────
   Owns its own open state so a host page can add resume with one line.
   Renders nothing unless the run is actually resumable. */

export interface ResumeButtonProps {
  run: { id: number; status: string; workflow_id?: number };
  onResumed?: (run: { id: number; status: string }) => void;
  size?: 'sm' | 'md';
  variant?: 'primary' | 'ghost' | 'danger' | 'secondary';
  className?: string;
}

export function ResumeButton({ run, onResumed, size = 'md', variant = 'primary', className }: ResumeButtonProps) {
  const [open, setOpen] = useState(false);
  if (run.status !== 'failed' && run.status !== 'cancelled') return null;
  return (
    <>
      <Button
        variant={variant}
        size={size}
        className={className}
        onClick={() => setOpen(true)}
        title="Pick this run back up — reuse what succeeded, re-run the rest"
      >
        <SkipForward size={13} /> Resume
      </Button>
      {open && (
        <ResumeDialog
          runId={run.id}
          runStatus={run.status}
          workflowId={run.workflow_id}
          onClose={() => setOpen(false)}
          onResumed={onResumed}
        />
      )}
    </>
  );
}
