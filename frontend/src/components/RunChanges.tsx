import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronDown, Code2, FileDiff, GitCompare, Package, ScrollText, Terminal } from 'lucide-react';
import clsx from 'clsx';
import { api } from '../api';
import { formatDate } from '../format';
import {
  fileName, gitLine, headline, inputsNote, packageLine, quietNote, shortDuration, slower, taskChanged,
  type CellChange, type CodeChange, type CodeFile, type RunDiff, type TaskDiff,
} from '../runDiff';

/* ─── What changed since the last good run ───────────────────
   On a failed run it opens by itself: the first question after "it failed"
   is "what is different from yesterday". On any finished run it can be asked
   for, against any earlier run of the workflow. */

interface RunOption { id: number; status: string; created_at: string }

export function RunChanges({ runId, workflowId, failed }: { runId: number; workflowId: number; failed: boolean }) {
  const [open, setOpen] = useState(failed);
  const [baseline, setBaseline] = useState<number | null>(null);
  const [diff, setDiff] = useState<RunDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [options, setOptions] = useState<RunOption[]>([]);

  useEffect(() => {
    if (!open) return;
    setDiff(null); setError(null);
    void api<RunDiff>(`/runs/${runId}/compare${baseline ? `?with=${baseline}` : ''}`)
      .then(setDiff).catch(e => setError(e instanceof Error ? e.message : 'Could not compare'));
  }, [open, runId, baseline]);
  useEffect(() => {
    if (!open || options.length) return;
    void api<RunOption[]>(`/runs?workflow_id=${workflowId}&limit=40`)
      .then(rows => setOptions(rows.filter(r => r.id < runId && !['queued', 'running', 'waiting_approval'].includes(r.status))))
      .catch(() => {});
  }, [open, workflowId, runId]);

  if (!open) {
    return (
      <button type="button" className="changes-ask" onClick={() => setOpen(true)}>
        <GitCompare size={14} /> Compare with an earlier run
      </button>
    );
  }
  const points = diff ? headline(diff) : [];
  const tasks = diff?.tasks ?? [];
  const changed = tasks.filter(taskChanged);
  const same = tasks.length - changed.length;
  const quiet = diff && quietNote(diff);
  const noInputs = diff && inputsNote(diff);
  return (
    <section className={clsx('panel changes', failed && 'is-failed')} aria-label="What changed">
      <div className="panel-head">
        <div>
          <h2><GitCompare size={15} /> What changed</h2>
          <p>
            {diff?.baseline
              ? <>Since <Link to={`/runs/${diff.baseline.id}`}>run #{diff.baseline.id}</Link>
                  {baseline ? '' : ', the last one that succeeded'} · {formatDate(diff.baseline.created_at)}</>
              : diff?.note ?? 'Comparing…'}
          </p>
        </div>
        <label className="changes-pick">
          <span>Compare with</span>
          <select value={baseline ?? ''} onChange={e => setBaseline(e.target.value ? Number(e.target.value) : null)}>
            <option value="">Last successful run</option>
            {options.map(r => <option key={r.id} value={r.id}>#{r.id} · {r.status} · {formatDate(r.created_at)}</option>)}
          </select>
        </label>
      </div>
      {error && <p className="settings-error changes-body">{error}</p>}
      {diff?.baseline && (
        <div className="changes-body">
          {points.length > 0 && (
            <ul className="changes-points">
              {points.map((p, i) => <li key={i} className={`is-${p.tone}`}>{p.text}</li>)}
            </ul>
          )}
          {quiet && <p className="changes-quiet">{quiet}</p>}
          {noInputs && <p className="changes-quiet">{noInputs}</p>}
          {(diff.parameters ?? []).length > 0 && (
            <div className="changes-block">
              <h3>Parameters</h3>
              <table className="changes-table">
                <thead><tr><th>Name</th><th>Last good run</th><th>This run</th></tr></thead>
                <tbody>
                  {diff.parameters!.map(p => (
                    <tr key={p.name}><td><code>{p.name}</code></td>
                      <td className="is-before">{p.change === 'added' ? '—' : String(p.before)}</td>
                      <td className="is-after">{p.change === 'removed' ? '—' : String(p.after)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {changed.map(task => <TaskChanges key={task.task_id} task={task} openByDefault={changed.length <= 2} />)}
          {same > 0 && <p className="changes-quiet">{same} other task{same === 1 ? '' : 's'} ran the same way as last time.</p>}
        </div>
      )}
    </section>
  );
}

function Diff({ lines }: { lines: string[] }) {
  return (
    <pre className="changes-diff">
      {lines.map((line, i) => (
        <span key={i} className={clsx(
          line.startsWith('+++') || line.startsWith('---') ? 'is-file'
            : line.startsWith('@@') ? 'is-hunk' : line.startsWith('+') ? 'is-add' : line.startsWith('-') ? 'is-del' : null,
        )}>{line}{'\n'}</span>
      ))}
    </pre>
  );
}

function TaskChanges({ task, openByDefault }: { task: TaskDiff; openByDefault: boolean }) {
  const a = task.after, b = task.before;
  const logs = task.logs;
  // A task that never started has only its status to say: no body to open.
  const detail = Boolean((a?.error && a.status === 'failed') || task.code || task.environment
    || task.command || task.notebook || (logs && (logs.new_total || logs.changed_total || logs.gone_total)));
  const [open, setOpen] = useState(openByDefault && detail);
  return (
    <div className={clsx('changes-task', a?.status === 'failed' && 'is-failed')}>
      <button type="button" className="changes-task-head" onClick={() => detail && setOpen(o => !o)}
              aria-expanded={detail ? open : undefined} disabled={!detail}>
        <ChevronDown size={14} className={clsx('changes-chevron', open && 'is-open', !detail && 'is-hidden')} />
        <strong>{task.name}</strong>
        <span className="changes-status">
          {b ? <span className={`st-${b.status}`}>{b.status}</span> : <em>not run</em>}
          <span aria-hidden="true">→</span>
          {a ? <span className={`st-${a.status}`}>{a.status}</span> : <em>not run</em>}
        </span>
        {a && b && <span className={clsx('changes-time', slower(task) && 'is-slower')}>{shortDuration(b.duration)} → {shortDuration(a.duration)}</span>}
      </button>
      {open && (
        <div className="changes-task-body">
          {a?.error && a.status === 'failed' && <p className="changes-error">{a.error}</p>}
          {task.code && <CodeChanges code={task.code} />}
          {task.environment && (
            <div className="changes-block">
              <h4><Package size={13} /> Environment {task.environment.name && <em>{task.environment.name}</em>}</h4>
              <ul className="changes-list">
                {task.environment.renamed && <li>Now {task.environment.renamed.after}, was {task.environment.renamed.before}</li>}
                {task.environment.python && <li>Python {task.environment.python.before ?? '?'} → {task.environment.python.after ?? '?'}</li>}
                {task.environment.packages.map(p => <li key={p.name}><code>{packageLine(p)}</code></li>)}
                {task.environment.packages_unknown && <li className="changes-quiet">Packages could not be listed for one of the runs.</li>}
              </ul>
            </div>
          )}
          {task.command && (
            <div className="changes-block">
              <h4><Terminal size={13} /> Command</h4>
              <Diff lines={[`- ${task.command.before}`, `+ ${task.command.after}`]} />
            </div>
          )}
          {task.notebook && <NotebookChanges notebook={task.notebook} />}
          {logs && (logs.new_total > 0 || logs.changed_total > 0 || logs.gone_total > 0) && (
            <div className="changes-block">
              <h4><ScrollText size={13} /> Log
                <em>{[logs.new_total && `${logs.new_total} new`, logs.changed_total && `${logs.changed_total} changed`,
                      logs.gone_total && `${logs.gone_total} gone`].filter(Boolean).join(' · ')}</em></h4>
              {logs.changed.length > 0 && (
                <pre className="changes-diff">
                  {logs.changed.map((c, i) => (
                    <span key={i}><span className="is-del">- {c.before}{'\n'}</span><span className="is-add">+ {c.after}{'\n'}</span></span>
                  ))}
                </pre>
              )}
              {logs.new.length > 0 && (
                <pre className="changes-diff">
                  {logs.new.map((l, i) => (
                    <span key={i} className={clsx('is-add', l.text === logs.first_error && 'is-first-error')}>
                      + {l.stream === 'stderr' ? '[stderr] ' : ''}{l.text}{'\n'}
                    </span>
                  ))}
                  {logs.new_total > logs.new.length && <span className="is-hunk">… {logs.new_total - logs.new.length} more{'\n'}</span>}
                </pre>
              )}
              {logs.gone.length > 0 && (
                <details className="changes-gone">
                  <summary>{logs.gone_total} line{logs.gone_total === 1 ? '' : 's'} from last time missing this time</summary>
                  <pre className="changes-diff">{logs.gone.map((l, i) => <span key={i} className="is-del">- {l.text}{'\n'}</span>)}</pre>
                </details>
              )}
              {logs.truncated && <p className="changes-quiet">Only the last megabyte of each log was compared.</p>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ChangedFile({ file }: { file: CodeFile }) {
  return (
    <div className="changes-file">
      <div className="changes-file-head"><code>{file.path}</code><em>{file.change}</em></div>
      {file.diff ? <Diff lines={file.diff.slice(2)} /> : file.note && <p className="changes-quiet">{file.note}</p>}
    </div>
  );
}

function CodeChanges({ code }: { code: CodeChange }) {
  const used = code.files.filter(f => f.used);
  const other = code.files.filter(f => !f.used);
  const git = gitLine(code.git);
  return (
    <div className="changes-block">
      <h4><Code2 size={13} /> Code
        <em>{code.mode === 'project' ? `in ${fileName(code.root)}` : 'the script and the files it reads'}{git ? ` · ${git}` : ''}</em>
      </h4>
      {code.moved && <p className="changes-quiet">The code ran from {code.moved.after}; last time from {code.moved.before}.</p>}
      {used.map(file => <ChangedFile key={file.path} file={file} />)}
      {other.length > 0 && (
        <details className="changes-gone" open={used.length === 0}>
          <summary>{other.length} other file{other.length === 1 ? '' : 's'} in {fileName(code.root)} changed — not used by this task</summary>
          {other.map(file => <ChangedFile key={file.path} file={file} />)}
        </details>
      )}
      {code.note && <p className="changes-quiet">{code.note}</p>}
    </div>
  );
}

const REASON: Record<string, string> = { code: 'code changed', output: 'output changed', failed: 'failed', slower: 'slower' };

function NotebookChanges({ notebook }: { notebook: NonNullable<TaskDiff['notebook']> }) {
  if (notebook.missing) return <p className="changes-quiet">No executed notebook for {notebook.missing} to compare.</p>;
  if (!notebook.cells?.length) return null;
  return (
    <div className="changes-block">
      <h4><FileDiff size={13} /> Notebook cells</h4>
      {notebook.cells.map((cell, i) => <Cell key={i} cell={cell} />)}
    </div>
  );
}

function Cell({ cell }: { cell: CellChange }) {
  const label = cell.change === 'added' ? `New cell ${cell.after_index}`
    : cell.change === 'removed' ? `Cell ${cell.before_index} removed`
    : `Cell ${cell.after_index}`;
  return (
    <div className={clsx('changes-cell', cell.error && 'is-failed')}>
      <div className="changes-cell-head">
        <strong>{label}</strong>
        {cell.reasons?.map(r => <span key={r} className={`changes-tag is-${r}`}>{REASON[r]}</span>)}
        {cell.reasons?.includes('slower') && (
          <span className="changes-quiet">{shortDuration(cell.duration_before)} → {shortDuration(cell.duration_after)}</span>
        )}
      </div>
      {cell.error && <p className="changes-error">{cell.error}</p>}
      {cell.source_diff ? <Diff lines={cell.source_diff.slice(2)} />
        : (cell.change !== 'changed' && cell.source) ? <pre className="changes-diff">{cell.source}</pre> : null}
      {cell.output_after && (
        <div className="changes-outputs">
          <div><span>Last good run</span><pre>{cell.output_before?.join('\n') || '(no output)'}</pre></div>
          <div><span>This run</span><pre>{cell.output_after.join('\n') || '(no output)'}</pre></div>
        </div>
      )}
    </div>
  );
}
