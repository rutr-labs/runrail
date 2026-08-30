import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import clsx from 'clsx';
import {
  ShieldAlert, ShieldCheck, Check, X, Terminal, CheckCircle2, ChevronRight,
  CornerDownRight, UserCheck, MessageSquare, CircleSlash, Hourglass, FolderOpen,
} from 'lucide-react';
import { api, post } from '../api';
import { Button, TaskTypeBadge } from './ui';
import { useToast } from './toast';

/* ─── Approval gates ───────────────────────────────────────
   A run parked on `waiting_approval` is stopped waiting for a person, so the
   decision surface is a card at the top of the run — never a button hidden in
   a toolbar. Everything the approver needs to decide lives in it: the note the
   workflow author wrote, the command that is about to run, what already
   succeeded upstream, what the approval releases downstream, and how long the
   run has been standing still.

   RunRail has no accounts. The name field is voluntary attribution and is
   labelled that way; it is never presented as identity or authentication.

   The gate is a shared surface — two tabs, two people — so a decision that
   lost the race comes back 409 and lands in an "already decided by X" state
   rather than an error toast. */

/* ─── Shapes ───────────────────────────────────────────────
   Deliberately structural and loose (every optional field is optional) so the
   host can pass its own Run / TaskRun / Task objects unchanged. */

export interface GateTaskRun {
  id: number;
  task_id: number;
  status: string;
  task_name?: string | null;
  task_type?: string | null;
  attempt?: number;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  rendered_command?: string | null;
  error_message?: string | null;
  approved_by?: string | null;
  approval_note?: string | null;
  approved_at?: string | null;
}

export interface GateRun {
  id: number;
  status: string;
  workflow_id?: number;
  created_at?: string;
  task_runs?: GateTaskRun[] | null;
}

/** A workflow task definition — used only to enrich the card (prompt fallback,
 *  command preview, upstream/downstream impact). Safe to omit. */
export interface GateTask {
  id: number;
  name: string;
  task_type?: string;
  command?: string | null;
  script_path?: string | null;
  notebook_path?: string | null;
  sql_path?: string | null;
  cwd?: string | null;
  depends_on_json?: string[] | null;
  requires_approval?: boolean;
  approval_prompt?: string | null;
}

/** A row from GET /api/approvals — a TaskRunOut plus its run/workflow context. */
export interface OpenApproval {
  id: number;
  task_run_id: number;
  run_id: number;
  workflow_id: number;
  workflow_name: string;
  prompt: string | null;
  task_id: number;
  task_name: string | null;
  task_type: string | null;
  status: string;
  created_at: string;
  rendered_command: string | null;
}

/* ─── Local formatting ─────────────────────────────────────
   App.tsx keeps formatDuration/timeAgo module-private; these mirror its
   output exactly so a gate reads like the rest of the run page. */

function formatSpan(seconds?: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function elapsedSince(value?: string | null, now = Date.now()): number | null {
  if (!value) return null;
  const started = new Date(value).getTime();
  return Number.isNaN(started) ? null : Math.max(0, (now - started) / 1000);
}

/** Ticks every second so "waiting 4m 12s" counts up while nobody decides. */
function useTicker(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

/* ─── Graph walk ───────────────────────────────────────────
   What the approval releases, and what it stands on. */
function relatives(tasks: GateTask[], start: string, direction: 'up' | 'down'): string[] {
  const edges = new Map<string, string[]>();
  for (const task of tasks) {
    const deps = task.depends_on_json ?? [];
    if (direction === 'up') edges.set(task.name, [...deps]);
    else for (const dep of deps) edges.set(dep, [...(edges.get(dep) ?? []), task.name]);
  }
  const seen = new Set<string>();
  const queue = [start];
  while (queue.length) {
    for (const next of edges.get(queue.shift()!) ?? []) {
      if (seen.has(next) || next === start) continue;
      seen.add(next);
      queue.push(next);
    }
  }
  return [...seen];
}

/** The command the approval releases. Prefers a real rendered command from an
 *  earlier attempt in this run; otherwise the task's source, marked as a
 *  template because `{{ ds }}`-style placeholders resolve at execution. */
function commandFor(task?: GateTask, prior?: GateTaskRun): { text: string; rendered: boolean } | null {
  if (prior?.rendered_command) return { text: prior.rendered_command, rendered: true };
  if (!task) return null;
  const source = task.command || task.script_path || task.notebook_path || task.sql_path;
  return source ? { text: source, rendered: false } : null;
}

/* ─── The card ─────────────────────────────────────────── */

export interface ApprovalGateProps {
  /** The run being viewed. Gates are read from `run.task_runs`. */
  run: GateRun;
  /** The workflow's current tasks, for the command preview and impact lists. */
  tasks?: GateTask[];
  /** Called after a decision lands (or is found already decided) so the host
   *  can refetch the run. */
  onDecided?: () => void;
  className?: string;
}

interface SettledGate {
  gate: GateTaskRun;
  row: GateTaskRun;
  /** True when the 409 path decided it: someone else got there first. */
  stale: boolean;
}

/** Renders the whole approval section, or nothing when no gate is open.
 *  Safe to mount unconditionally on the run detail page. */
export function ApprovalGate({ run, tasks, onDecided, className }: ApprovalGateProps) {
  const open = useMemo(
    () => (run.task_runs ?? []).filter(t => t.status === 'awaiting_approval'),
    [run.task_runs]
  );
  // A decided gate leaves run.task_runs on the host's very next refetch, which
  // would rip the outcome — including "already approved by X" — off the screen
  // before it was read. Settled cards therefore live here and stay until the
  // person dismisses them.
  const [settled, setSettled] = useState<Record<number, SettledGate>>({});
  const gates = open.filter(gate => !settled[gate.id]);
  const settledCards = Object.values(settled);
  const signature = gates.map(g => g.id).join(',');

  // The prompt the workflow author wrote lives on the Task, and /approvals
  // already joins it in — one fetch instead of asking the host to thread it.
  const [prompts, setPrompts] = useState<Record<number, string | null>>({});
  useEffect(() => {
    if (!signature) return;
    let alive = true;
    api<OpenApproval[]>('/approvals')
      .then(rows => {
        if (!alive) return;
        setPrompts(Object.fromEntries(rows.map(r => [r.task_run_id, r.prompt])));
      })
      .catch(() => { /* the card still renders; the prompt falls back to the task */ });
    return () => { alive = false; };
  }, [signature]);

  const now = useTicker(gates.length > 0);
  if (!gates.length && !settledCards.length) return null;

  const oldest = gates.reduce<number | null>((longest, gate) => {
    const waited = elapsedSince(gate.created_at ?? gate.started_at, now);
    return waited != null && (longest == null || waited > longest) ? waited : longest;
  }, null);
  const done = gates.length === 0;

  return (
    <section
      id="approval-gate"
      className={clsx('approval-gate', done && 'approval-gate--settled', className)}
      role="region"
      aria-labelledby="approval-gate-title"
    >
      <div className="approval-gate-aura" aria-hidden="true" />
      <header className="approval-gate-head">
        <span className="approval-gate-glyph">
          {done ? <ShieldCheck size={20} /> : <ShieldAlert size={20} />}
        </span>
        <div className="approval-gate-headtext">
          <span className="approval-gate-eyebrow">{done ? 'DECISION RECORDED' : 'AWAITING APPROVAL'}</span>
          <h2 id="approval-gate-title">
            {done
              ? 'This gate has been decided'
              : gates.length === 1
              ? 'This run is paused for your approval'
              : `${gates.length} approvals are holding this run`}
          </h2>
          <p>
            {done
              ? 'The run is no longer waiting on a person.'
              : 'Nothing downstream runs until someone decides. The run keeps its slot and its parameters while it waits.'}
          </p>
        </div>
        {!done && oldest != null && (
          <span className="approval-gate-timer" title="Time since the gate opened">
            <Hourglass size={12} /> waiting {formatSpan(oldest)}
          </span>
        )}
      </header>

      <div className="approval-gate-list">
        {gates.map(gate => (
          <GateDecision
            key={gate.id}
            gate={gate}
            run={run}
            tasks={tasks}
            prompt={prompts[gate.id]}
            now={now}
            onSettled={entry => setSettled(current => ({ ...current, [entry.gate.id]: entry }))}
            onDecided={onDecided}
          />
        ))}
        {settledCards.map(entry => (
          <SettledDecision
            key={entry.gate.id}
            entry={entry}
            runId={run.id}
            onDismiss={() => setSettled(({ [entry.gate.id]: _removed, ...rest }) => rest)}
          />
        ))}
      </div>
    </section>
  );
}

function SettledDecision({ entry, runId, onDismiss }: {
  entry: SettledGate; runId: number; onDismiss: () => void;
}) {
  const approved = entry.row.status === 'approved';
  const taskName = entry.gate.task_name ?? entry.row.task_name ?? `Task #${entry.gate.task_id}`;
  return (
    <article className="approval-gate-item approval-gate-item--settled" aria-live="polite">
      <div className={clsx('approval-gate-settled', approved ? 'is-approved' : 'is-rejected')}>
        <span className="approval-gate-settled-icon">
          {approved ? <ShieldCheck size={17} /> : <CircleSlash size={17} />}
        </span>
        <div className="approval-gate-settled-body">
          <b>
            {entry.stale ? 'Already ' : ''}{approved ? 'approved' : 'rejected'}
            {entry.row.approved_by ? <> by <em>{entry.row.approved_by}</em></> : null}
          </b>
          <p>
            {entry.stale
              ? 'This gate was decided elsewhere before your click landed — nothing you did changed it.'
              : approved
              ? `${taskName} is released; the run picks up where it stopped.`
              : `Everything downstream of ${taskName} is skipped and run #${runId} lands cancelled.`}
          </p>
          {entry.row.approval_note && (
            <p className="approval-gate-settled-note">“{entry.row.approval_note}”</p>
          )}
        </div>
        <button type="button" className="approval-gate-dismiss" onClick={onDismiss}>Dismiss</button>
      </div>
    </article>
  );
}

function GateDecision({ gate, run, tasks, prompt, now, onSettled, onDecided }: {
  gate: GateTaskRun;
  run: GateRun;
  tasks?: GateTask[];
  prompt?: string | null;
  now: number;
  onSettled: (entry: SettledGate) => void;
  onDecided?: () => void;
}) {
  const { toast } = useToast();
  const [name, setName] = useState('');
  const [note, setNote] = useState('');
  const [noteOpen, setNoteOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null);

  const taskName = gate.task_name ?? `Task #${gate.task_id}`;
  const task = tasks?.find(t => t.id === gate.task_id || t.name === gate.task_name);
  const prior = (run.task_runs ?? []).find(
    t => t.task_id === gate.task_id && t.id !== gate.id && Boolean(t.rendered_command)
  );
  const command = commandFor(task, prior);
  const waited = elapsedSince(gate.created_at ?? gate.started_at, now);
  // /approvals is the authority on the prompt, but the task definition covers
  // the first paint and the case where that fetch never lands.
  const question = prompt ?? task?.approval_prompt ?? null;

  const succeeded = (run.task_runs ?? [])
    .filter(t => t.status === 'success' && t.task_name)
    .map(t => ({ name: t.task_name as string, seconds: t.duration_seconds ?? null }));
  const upstream = tasks && gate.task_name ? relatives(tasks, gate.task_name, 'up') : null;
  // Only the successes this gate actually stands on, when the graph is known.
  const standsOn = upstream ? succeeded.filter(s => upstream.includes(s.name)) : succeeded;
  const releases = tasks && gate.task_name ? relatives(tasks, gate.task_name, 'down') : [];

  const decide = async (approved: boolean) => {
    setBusy(approved ? 'approve' : 'reject');
    // approved_by is required once a body is sent, so a bare decision posts no
    // body at all; a note without a name is recorded honestly as Anonymous.
    const attribution = name.trim();
    const body = attribution || note.trim()
      ? { approved_by: attribution || 'Anonymous', note: note.trim() || null }
      : null;
    try {
      const row = await post<GateTaskRun>(
        `/task-runs/${gate.id}/${approved ? 'approve' : 'reject'}`, body
      );
      onSettled({ gate, row, stale: false });
      toast(
        approved ? `Approved — ${taskName} is released` : `Rejected — run #${run.id} will cancel`,
        approved ? 'success' : 'info'
      );
    } catch (error) {
      // 409: someone else decided first. api() surfaces only FastAPI's `detail`,
      // and the backend phrases the conflict as "This gate is already <status>".
      // Re-read the row so the card can name who decided it instead of shouting
      // an error at a tab that simply lost the race.
      const message = error instanceof Error ? error.message : '';
      if (/already/i.test(message)) {
        const row = await api<GateTaskRun>(`/task-runs/${gate.id}`).catch(() => null);
        onSettled({ gate, row: row ?? { ...gate, status: 'approved' }, stale: true });
      } else {
        toast(message || 'Could not record the decision', 'error');
      }
    } finally {
      setBusy(null);
      setConfirming(false);
      onDecided?.();
    }
  };

  return (
    <article className="approval-gate-item">
      <div className="approval-gate-task">
        {(gate.task_type || task?.task_type) && (
          <TaskTypeBadge type={(gate.task_type || task?.task_type) as string} />
        )}
        <b className="approval-gate-task-name" title={taskName}>{taskName}</b>
        <span className="approval-gate-task-sep">needs a decision before it runs</span>
        {waited != null && (
          <span className="approval-gate-waited">
            <Hourglass size={11} /> {formatSpan(waited)}
          </span>
        )}
      </div>

      <blockquote className={clsx('approval-gate-prompt', !question && 'approval-gate-prompt--empty')}>
        {question || 'The workflow author left no note on this gate. Read the command and the upstream results before deciding.'}
      </blockquote>

      {command && (
        <div className="approval-gate-cmd">
          <span className="approval-gate-cmd-label">
            <Terminal size={11} />
            {command.rendered ? 'Command about to run' : 'Command about to run · template'}
          </span>
          <pre>{command.text}</pre>
          {!command.rendered && (
            <small>Placeholders such as <code>{'{{ ds }}'}</code> resolve when the task starts.</small>
          )}
          {task?.cwd && (
            <small className="approval-gate-cmd-cwd"><FolderOpen size={10} /> {task.cwd}</small>
          )}
        </div>
      )}

      <div className="approval-gate-context">
        <div className="approval-gate-ctx">
          <span className="approval-gate-ctx-label"><CheckCircle2 size={11} /> Already succeeded</span>
          {standsOn.length ? (
            <div className="approval-gate-chips">
              {standsOn.slice(0, 10).map(item => (
                <span key={item.name} className="approval-chip approval-chip--done">
                  <Check size={10} /> {item.name}
                  {item.seconds != null && <i>{formatSpan(item.seconds)}</i>}
                </span>
              ))}
              {standsOn.length > 10 && (
                <span className="approval-chip approval-chip--more">+{standsOn.length - 10} more</span>
              )}
            </div>
          ) : (
            <p className="approval-gate-ctx-empty">Nothing has finished ahead of this gate yet.</p>
          )}
        </div>
        <div className="approval-gate-ctx">
          <span className="approval-gate-ctx-label"><CornerDownRight size={11} /> Approving releases</span>
          <div className="approval-gate-chips">
            <span className="approval-chip approval-chip--next">{taskName}</span>
            {releases.slice(0, 9).map(downstream => (
              <span key={downstream} className="approval-chip approval-chip--downstream">{downstream}</span>
            ))}
            {releases.length > 9 && (
              <span className="approval-chip approval-chip--more">+{releases.length - 9} more</span>
            )}
          </div>
          {releases.length > 0 && (
            <p className="approval-gate-ctx-empty">
              {releases.length} further task{releases.length === 1 ? '' : 's'} depend{releases.length === 1 ? 's' : ''} on this decision.
            </p>
          )}
        </div>
      </div>

      <div className="approval-gate-attribution">
        <label className="approval-gate-name">
          <span><UserCheck size={11} /> Your name <em>optional — attribution only, not a sign-in</em></span>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            maxLength={120}
            placeholder="e.g. Priya"
            autoComplete="off"
          />
        </label>
        {noteOpen ? (
          <label className="approval-gate-note">
            <span><MessageSquare size={11} /> Note <em>optional — stored with the decision</em></span>
            <textarea
              value={note}
              onChange={e => setNote(e.target.value)}
              maxLength={2000}
              rows={2}
              placeholder="Why you approved or rejected this"
            />
            {!name.trim() && note.trim() && (
              <small>Without a name this note is recorded as <b>Anonymous</b>.</small>
            )}
          </label>
        ) : (
          <button type="button" className="approval-gate-note-toggle" onClick={() => setNoteOpen(true)}>
            <MessageSquare size={11} /> Add a note
          </button>
        )}
      </div>

      {confirming ? (
        <div className="approval-gate-confirm" role="alertdialog" aria-label="Confirm rejection">
          <span className="approval-gate-confirm-text">
            <CircleSlash size={13} />
            Rejecting skips everything downstream and lands run #{run.id} as <b>cancelled</b>. It cannot be undone — only a resume or a fresh run gets you back.
          </span>
          <div className="approval-gate-confirm-actions">
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)} disabled={busy !== null}>
              Keep waiting
            </Button>
            <Button variant="danger" size="sm" onClick={() => decide(false)} disabled={busy !== null}>
              <X size={12} /> {busy === 'reject' ? 'Rejecting…' : 'Reject and cancel run'}
            </Button>
          </div>
        </div>
      ) : (
        <div className="approval-gate-actions">
          <Button
            className="approval-approve"
            onClick={() => decide(true)}
            disabled={busy !== null}
            title={`Let ${taskName} run`}
          >
            <Check size={14} /> {busy === 'approve' ? 'Approving…' : 'Approve and continue'}
          </Button>
          <Button
            variant="ghost"
            className="approval-reject"
            onClick={() => setConfirming(true)}
            disabled={busy !== null}
            title="Refuse this task and cancel the run"
          >
            <X size={13} /> Reject
          </Button>
          <span className="approval-gate-actions-hint">
            Approve runs the command above. Reject asks once more, then cancels the run.
          </span>
        </div>
      )}
    </article>
  );
}

/* ─── Where people actually look ───────────────────────────
   A gate nobody sees is a run nobody finishes, so the open gates surface on
   the dashboard too. */

/** Polls GET /api/approvals and follows the WebSocket. `null` until the first
 *  response, so callers can tell "loading" from "none open". */
export function useOpenApprovals(intervalMs = 15000): OpenApproval[] | null {
  const [rows, setRows] = useState<OpenApproval[] | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => {
      api<OpenApproval[]>('/approvals')
        .then(next => { if (alive) setRows(next); })
        .catch(() => { if (alive) setRows(current => current ?? []); });
    };
    load();
    const timer = window.setInterval(load, intervalMs);
    return () => { alive = false; window.clearInterval(timer); };
  }, [intervalMs]);
  return rows;
}

export interface ApprovalInboxProps {
  /** Maximum runs listed; the rest collapse into a "see all" line. */
  limit?: number;
  /** Render an empty panel instead of nothing when no gate is open. */
  showEmpty?: boolean;
  className?: string;
}

/** Compact "waiting for you" panel. Renders nothing when no gate is open, so
 *  it can be dropped into a dashboard column unconditionally. */
export function ApprovalInbox({ limit = 6, showEmpty = false, className }: ApprovalInboxProps) {
  const rows = useOpenApprovals();
  const now = useTicker(Boolean(rows?.length));

  const byRun = useMemo(() => {
    const groups = new Map<number, OpenApproval[]>();
    for (const row of rows ?? []) groups.set(row.run_id, [...(groups.get(row.run_id) ?? []), row]);
    return [...groups.values()];
  }, [rows]);

  if (rows === null) return null;
  if (!byRun.length) {
    return showEmpty ? (
      <div className={clsx('panel', className)}>
        <div className="panel-head"><div><h2>Waiting for you</h2><p>Runs paused on an approval gate</p></div></div>
        <div className="approval-inbox-empty">
          <ShieldCheck size={18} /> No approvals pending — nothing is waiting on a person.
        </div>
      </div>
    ) : null;
  }

  return (
    <div className={clsx('panel', 'approval-inbox', className)}>
      <div className="panel-head">
        <div>
          <h2>Waiting for you</h2>
          <p>
            {byRun.length} run{byRun.length === 1 ? '' : 's'} paused on an approval gate
          </p>
        </div>
        <span className="approval-inbox-count">{byRun.length}</span>
      </div>
      <div className="approval-inbox-list">
        {byRun.slice(0, limit).map(group => {
          const head = group[0];
          const waited = elapsedSince(
            group.reduce((oldest, row) => (row.created_at < oldest.created_at ? row : oldest), head).created_at,
            now
          );
          const names = group.map(row => row.task_name ?? `#${row.task_id}`).join(', ');
          return (
            <Link key={head.run_id} to={`/runs/${head.run_id}`} className="approval-inbox-row">
              <span className="approval-inbox-pip" aria-hidden="true" />
              <div className="approval-inbox-body">
                <div className="approval-inbox-name" title={head.workflow_name}>{head.workflow_name}</div>
                <div className="approval-inbox-meta" title={names}>
                  #{head.run_id} · {group.length > 1 ? `${group.length} gates · ` : ''}{names}
                  {waited != null && ` · waiting ${formatSpan(waited)}`}
                </div>
              </div>
              <span className="approval-inbox-cta">Review</span>
              <ChevronRight size={14} />
            </Link>
          );
        })}
        {byRun.length > limit && (
          <div className="approval-inbox-more">+{byRun.length - limit} more waiting</div>
        )}
      </div>
    </div>
  );
}
