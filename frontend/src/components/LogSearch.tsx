import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, CheckCircle2, ChevronRight, Search, SlidersHorizontal, Terminal,
} from 'lucide-react';
import clsx from 'clsx';
import { api } from '../api';
import { formatBytes, formatDate, timeAgo } from '../format';
import { Button, EmptyState, LoadingBar, PageHeader, StatusBadge } from './ui';

/* ─── Log search ───────────────────────────────────────────
   Bounded grep across the logs runs already wrote. There is no index: the
   server walks the newest task logs in scope and stops when it hits one of its
   bounds (match limit, wall clock, file cap).

   Everything below the results is about that last sentence. A truncated scan
   that renders like a full one is the single worst thing this page can do —
   "this error first appeared on Tuesday" is a conclusion people act on, and it
   is only true when `stats.complete` is true. So:
     • "first appeared" framing is shown ONLY for a complete scan;
     • an incomplete scan says plainly that results are partial and which bound
       stopped it;
     • zero matches from an incomplete scan is reported as "nothing in what was
       scanned", never as "nothing happened".
   The per-file byte cap and unreadable files get the same treatment: both mean
   lines existed that were never looked at. */

/* ─── API shapes (GET /api/logs/search) ────────────────── */
export interface LogMatch {
  workflow_run_id: number;
  workflow_id: number;
  workflow_name: string;
  run_status: string;
  run_created_at: string;
  task_run_id: number;
  task_id: number;
  task_name: string;
  task_status: string;
  attempt: number;
  stream: 'stdout' | 'stderr';
  line_number: number;
  line: string;
  context_before: string[];
  context_after: string[];
}

export interface LogSearchStats {
  files_scanned: number;
  files_missing: number;
  bytes_scanned: number;
  runs_matched: number;
  elapsed_ms: number;
  truncated_files: number;
  /** True only when no bound fired — the one thing that licenses a claim about
   *  history. */
  complete: boolean;
  stopped_by: 'limit' | 'timeout' | 'max_files' | null;
}

export interface LogSearchResponse {
  query: string;
  regex: boolean;
  matches: LogMatch[];
  stats: LogSearchStats;
  /** The oldest match *within the scanned window* — only the first occurrence
   *  in history when stats.complete is true. */
  oldest_match: { workflow_run_id: number; run_created_at: string } | null;
}

type WorkflowRow = { id: number; name: string };
type TaskRow = { id: number; name: string };

/* Enum values from models.py; waiting_approval / awaiting_approval / approved /
   rejected are the v0.5 additions. */
const RUN_STATUSES = ['queued', 'running', 'success', 'failed', 'cancelled', 'waiting_approval'];
const TASK_STATUSES = [
  'queued', 'running', 'success', 'failed', 'skipped', 'cancelled',
  'awaiting_approval', 'approved', 'rejected',
];
const STREAMS = [
  { value: 'both', label: 'Both' },
  { value: 'stderr', label: 'stderr' },
  { value: 'stdout', label: 'stdout' },
] as const;

const LIMITS = [50, 100, 200, 500];
const TIMEOUTS = [2000, 5000, 10_000, 30_000];
const FILE_CAPS = [200, 500, 2000, 8000, 20_000];
const BYTE_CAPS = [1_000_000, 5_000_000, 20_000_000];

interface Filters {
  q: string;
  regex: boolean;
  caseSensitive: boolean;
  workflowId: string;
  taskName: string;
  status: string;
  taskStatus: string;
  stream: 'both' | 'stdout' | 'stderr';
  since: string;   // datetime-local
  until: string;   // datetime-local
  limit: number;
  context: number;
  maxFiles: number;
  maxBytes: number;
  timeoutMs: number;
}

/* Defaults mirror the endpoint's own defaults, so an untouched form asks for
   exactly what the API would have chosen. */
const BLANK: Filters = {
  q: '', regex: false, caseSensitive: false, workflowId: '', taskName: '',
  status: '', taskStatus: '', stream: 'both', since: '', until: '',
  limit: 50, context: 2, maxFiles: 2000, maxBytes: 5_000_000, timeoutMs: 5000,
};

const MIN_QUERY = 2; // the endpoint's own min_length

function toIso(local: string): string | null {
  if (!local) return null;
  const date = new Date(local); // datetime-local is in the viewer's zone
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function buildQuery(f: Filters): string {
  const params = new URLSearchParams({ q: f.q.trim() });
  if (f.regex) params.set('regex', 'true');
  if (f.caseSensitive) params.set('case_sensitive', 'true');
  if (f.workflowId) params.set('workflow_id', f.workflowId);
  if (f.taskName.trim()) params.set('task_name', f.taskName.trim());
  if (f.status) params.set('status', f.status);
  if (f.taskStatus) params.set('task_status', f.taskStatus);
  if (f.stream !== 'both') params.set('stream', f.stream);
  const since = toIso(f.since);
  const until = toIso(f.until);
  if (since) params.set('since', since);
  if (until) params.set('until', until);
  params.set('limit', String(f.limit));
  params.set('context', String(f.context));
  params.set('max_files', String(f.maxFiles));
  params.set('max_bytes_per_file', String(f.maxBytes));
  params.set('timeout_ms', String(f.timeoutMs));
  return params.toString();
}

export function LogSearch() {
  const [filters, setFilters] = useState<Filters>(BLANK);
  const [advanced, setAdvanced] = useState(false);
  const [flows, setFlows] = useState<WorkflowRow[]>([]);
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [result, setResult] = useState<LogSearchResponse | null>(null);
  /** The filters the on-screen result was produced with — the banner quotes
   *  the bounds that actually applied, not whatever the form says now. */
  const [ran, setRan] = useState<Filters | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  const set = <K extends keyof Filters>(key: K, value: Filters[K]) =>
    setFilters(current => ({ ...current, [key]: value }));

  useEffect(() => {
    api<WorkflowRow[]>('/workflows').then(setFlows).catch(() => {});
  }, []);

  /* Task name is matched exactly by the server, so offer the real names of the
     chosen workflow rather than letting people guess. */
  useEffect(() => {
    if (!filters.workflowId) { setTasks([]); return; }
    let stale = false;
    api<TaskRow[]>(`/workflows/${filters.workflowId}/tasks`)
      .then(rows => { if (!stale) setTasks(rows); })
      .catch(() => { if (!stale) setTasks([]); });
    return () => { stale = true; };
  }, [filters.workflowId]);

  useEffect(() => () => inFlight.current?.abort(), []);

  const runSearch = async (event?: FormEvent) => {
    event?.preventDefault();
    const query = filters.q.trim();
    if (query.length < MIN_QUERY) return;
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    const asked = { ...filters, q: query };
    setSearching(true);
    setError(null);
    try {
      const data = await api<LogSearchResponse>(`/logs/search?${buildQuery(asked)}`, { signal: controller.signal });
      setResult(data);
      setRan(asked);
    } catch (problem) {
      if (problem instanceof DOMException && problem.name === 'AbortError') return;
      setResult(null);
      setRan(null);
      setError(problem instanceof Error && problem.message
        ? problem.message
        : 'The search could not be run.');
    } finally {
      if (inFlight.current === controller) {
        inFlight.current = null;
        setSearching(false);
      }
    }
  };

  const groups = useMemo(() => groupByRun(result?.matches ?? []), [result]);
  const highlighter = useMemo(
    () => (ran ? buildHighlighter(ran.q, ran.regex, ran.caseSensitive) : null),
    [ran]);

  const tooShort = filters.q.trim().length < MIN_QUERY;
  const scoped = Boolean(filters.workflowId || filters.taskName || filters.status
    || filters.taskStatus || filters.stream !== 'both' || filters.since || filters.until);

  return (
    <>
      <PageHeader
        eyebrow="OBSERVABILITY"
        title="Log search"
        subtitle="Grep every log your runs have already written — and see exactly how much of it was read."
      />

      <form className="panel logsearch-form" onSubmit={runSearch}>
        <div className="panel-head">
          <div>
            <h2>Search</h2>
            <p>Newest logs first. Nothing is indexed, so each search is a bounded scan.</p>
          </div>
          <button type="button" className="edit-link" onClick={() => setAdvanced(a => !a)}>
            <SlidersHorizontal size={12} /> {advanced ? 'Hide limits' : 'Limits & matching'}
          </button>
        </div>

        <div className="filterbar logsearch-filters">
          <div className="filterbar-search logsearch-query">
            <Search size={14} color="var(--text-3)" />
            <input value={filters.q} autoFocus
                   placeholder={filters.regex ? 'Regular expression…' : 'Text to find in logs…'}
                   aria-label="Search logs"
                   onChange={e => set('q', e.target.value)} />
          </div>
          <select value={filters.workflowId} aria-label="Workflow filter"
                  onChange={e => set('workflowId', e.target.value)}>
            <option value="">All workflows</option>
            {flows.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
          </select>
          <input className="logsearch-taskname" list="logsearch-task-names" value={filters.taskName}
                 placeholder="Task name (exact)" aria-label="Task name filter"
                 onChange={e => set('taskName', e.target.value)} />
          <datalist id="logsearch-task-names">
            {tasks.map(t => <option key={t.id} value={t.name} />)}
          </datalist>
          <select value={filters.status} aria-label="Run status filter"
                  onChange={e => set('status', e.target.value)}>
            <option value="">Any run status</option>
            {RUN_STATUSES.map(s => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
          </select>
          <select value={filters.taskStatus} aria-label="Task status filter"
                  onChange={e => set('taskStatus', e.target.value)}>
            <option value="">Any task status</option>
            {TASK_STATUSES.map(s => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
          </select>
          <div className="segmented slim logsearch-stream-pick" role="group" aria-label="Stream">
            {STREAMS.map(s => (
              <button key={s.value} type="button" className={s.value === filters.stream ? 'active' : ''}
                      aria-pressed={s.value === filters.stream}
                      onClick={() => set('stream', s.value)}>
                {s.label}
              </button>
            ))}
          </div>
          <label className="logsearch-date">
            <span>From</span>
            <input type="datetime-local" value={filters.since}
                   aria-label="Runs created after"
                   onChange={e => set('since', e.target.value)} />
          </label>
          <label className="logsearch-date">
            <span>To</span>
            <input type="datetime-local" value={filters.until}
                   aria-label="Runs created before"
                   onChange={e => set('until', e.target.value)} />
          </label>
          <div className="logsearch-actions">
            {(scoped || filters.q) && (
              <button type="button" className="edit-link"
                      onClick={() => { setFilters(BLANK); setResult(null); setRan(null); setError(null); }}>
                Clear
              </button>
            )}
            <Button type="submit" size="sm" disabled={tooShort || searching}>
              <Search size={13} /> {searching ? 'Searching…' : 'Search'}
            </Button>
          </div>
        </div>

        <p className="logsearch-hint">
          {filters.regex
            ? <>Matching as a <b>regular expression</b> (Python syntax). Patterns that nest quantifiers are rejected by the server.</>
            : <>The query is a <b>literal substring</b> — no wildcards, no regex. Turn on “Regex” below to change that.</>}
          {tooShort && filters.q.length > 0 && <> Enter at least {MIN_QUERY} characters.</>}
          {' '}Date filters apply to when the <b>run</b> was created, not to the timestamps inside the log lines.
        </p>

        {advanced && (
          <div className="logsearch-advanced">
            <label className="logsearch-check">
              <input type="checkbox" checked={filters.regex}
                     onChange={e => set('regex', e.target.checked)} />
              Regex
            </label>
            <label className="logsearch-check">
              <input type="checkbox" checked={filters.caseSensitive}
                     onChange={e => set('caseSensitive', e.target.checked)} />
              Case sensitive
            </label>
            <label className="field compact">
              <span>Max matches</span>
              <select value={filters.limit} onChange={e => set('limit', Number(e.target.value))}>
                {LIMITS.map(n => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <label className="field compact">
              <span>Context lines</span>
              <input type="number" min={0} max={10} value={filters.context}
                     onChange={e => set('context', clamp(Number(e.target.value), 0, 10))} />
            </label>
            <label className="field compact">
              <span>Time budget</span>
              <select value={filters.timeoutMs} onChange={e => set('timeoutMs', Number(e.target.value))}>
                {TIMEOUTS.map(n => <option key={n} value={n}>{n / 1000}s</option>)}
              </select>
            </label>
            <label className="field compact">
              <span>Max log files</span>
              <select value={filters.maxFiles} onChange={e => set('maxFiles', Number(e.target.value))}>
                {FILE_CAPS.map(n => <option key={n} value={n}>{n.toLocaleString()}</option>)}
              </select>
            </label>
            <label className="field compact">
              <span>Per-file cap</span>
              <select value={filters.maxBytes} onChange={e => set('maxBytes', Number(e.target.value))}>
                {BYTE_CAPS.map(n => <option key={n} value={n}>{formatBytes(n)}</option>)}
              </select>
            </label>
            <p className="logsearch-advanced-note">
              Every limit here exists so one search cannot stall the server. Raising them makes the
              scan slower but covers more history; whatever you choose, the result below says how far it got.
            </p>
          </div>
        )}
      </form>

      {error && (
        <div className="logsearch-banner logsearch-banner--error">
          <AlertTriangle size={15} />
          <div>
            <b>The search did not run.</b>
            <p>{error}</p>
          </div>
        </div>
      )}

      {searching && (
        <div className="panel logsearch-progress">
          <div style={{ width: 220 }}><LoadingBar /></div>
          <p>Reading the newest logs in scope…</p>
        </div>
      )}

      {!searching && !error && result && ran && (
        <ScanReport result={result} asked={ran} />
      )}

      {!searching && !error && result && groups.length > 0 && (
        <div className="logsearch-results">
          {groups.map(group => (
            <section key={group.runId} className="panel logsearch-run">
              <div className="panel-head logsearch-run-head">
                <div>
                  <h2>
                    <Link className="run-id" to={`/runs/${group.runId}`}>#{group.runId}</Link>
                    <span className="logsearch-run-flow" title={group.workflowName}>{group.workflowName}</span>
                  </h2>
                  <p title={formatDate(group.createdAt)}>
                    {timeAgo(group.createdAt)} · {group.hits.length} {group.hits.length === 1 ? 'match' : 'matches'}
                  </p>
                </div>
                <div className="logsearch-run-meta">
                  <StatusBadge value={group.runStatus} />
                  <Link className="panel-link" to={`/runs/${group.runId}`}>Open run →</Link>
                </div>
              </div>
              <div className="logsearch-hits">
                {group.hits.map(hit => (
                  <Hit key={`${hit.task_run_id}-${hit.stream}-${hit.line_number}`}
                       hit={hit} highlighter={highlighter} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      {!searching && !error && result && groups.length === 0 && (
        <EmptyState
          icon={<Terminal size={22} />}
          title={result.stats.complete ? 'No matches anywhere in scope' : 'No matches in the part that was scanned'}
          text={result.stats.complete
            ? `Nothing in the ${result.stats.files_scanned.toLocaleString()} log files in scope contains “${result.query}”.`
            : 'The scan stopped before it read everything in scope, so this is not evidence that the text never appeared. Widen the limits above or narrow the filters, then search again.'}
        />
      )}

      {!searching && !error && !result && (
        <EmptyState
          icon={<Search size={22} />}
          title="Search the logs your runs already wrote"
          text={`Type at least ${MIN_QUERY} characters and press Search. The query is a literal substring unless you turn on regex, and each search reads the newest logs first within a fixed budget.`}
        />
      )}
    </>
  );
}

/* ─── Honesty banner ───────────────────────────────────────
   The one component on this page that must never be softened. */
function ScanReport({ result, asked }: { result: LogSearchResponse; asked: Filters }) {
  const { stats, oldest_match: oldest, matches } = result;
  const caveats: string[] = [];
  if (stats.truncated_files > 0) {
    caveats.push(
      `${stats.truncated_files} log ${stats.truncated_files === 1 ? 'file was' : 'files were'} larger than the `
      + `${formatBytes(asked.maxBytes)} per-file cap and ${stats.truncated_files === 1 ? 'was' : 'were'} read only up to it — `
      + 'anything after that point in those files was not looked at.');
  }
  if (stats.files_missing > 0) {
    caveats.push(
      `${stats.files_missing} log ${stats.files_missing === 1 ? 'file' : 'files'} could not be opened `
      + '(rotated, deleted, or moved) and were skipped.');
  }

  const volume = `${stats.files_scanned.toLocaleString()} files · ${formatBytes(stats.bytes_scanned)} · ${stats.elapsed_ms} ms`;

  if (stats.complete) {
    return (
      <div className="logsearch-banner logsearch-banner--complete">
        <CheckCircle2 size={15} />
        <div>
          <b>Complete scan of everything in scope.</b>
          <p>
            {matches.length} {matches.length === 1 ? 'match' : 'matches'} across {stats.runs_matched}{' '}
            {stats.runs_matched === 1 ? 'run' : 'runs'} — {volume}.
            {oldest && (
              <> First appeared in{' '}
                <Link className="run-id" to={`/runs/${oldest.workflow_run_id}`}>#{oldest.workflow_run_id}</Link>{' '}
                on {formatDate(oldest.run_created_at)}{caveats.length > 0 ? ', with the caveats below' : ''}.
              </>
            )}
          </p>
          {caveats.map(text => <p key={text} className="logsearch-caveat">{text}</p>)}
        </div>
      </div>
    );
  }

  return (
    <div className="logsearch-banner logsearch-banner--partial">
      <AlertTriangle size={15} />
      <div>
        <b>Partial results — {stoppedHeadline(stats.stopped_by)}</b>
        <p>{stoppedDetail(stats, asked)}</p>
        <p>
          {matches.length} {matches.length === 1 ? 'match' : 'matches'} across {stats.runs_matched}{' '}
          {stats.runs_matched === 1 ? 'run' : 'runs'} in what was read — {volume}.
          {oldest && (
            <> The oldest of them is in{' '}
              <Link className="run-id" to={`/runs/${oldest.workflow_run_id}`}>#{oldest.workflow_run_id}</Link>{' '}
              ({formatDate(oldest.run_created_at)}), which is <b>not</b> necessarily the first time this appeared —
              older logs were never opened.
            </>
          )}
        </p>
        {caveats.map(text => <p key={text} className="logsearch-caveat">{text}</p>)}
      </div>
    </div>
  );
}

function stoppedHeadline(stoppedBy: LogSearchStats['stopped_by']): string {
  if (stoppedBy === 'limit') return 'the match limit was reached.';
  if (stoppedBy === 'timeout') return 'the scan ran out of time.';
  if (stoppedBy === 'max_files') return 'the file cap was reached.';
  return 'the scan stopped early.';
}

function stoppedDetail(stats: LogSearchStats, asked: Filters): string {
  if (stats.stopped_by === 'limit') {
    return `RunRail stopped after the first ${asked.limit} matches. Logs are read newest-first, so `
      + 'everything older than the last match below went unread. Raise the match limit, or narrow the '
      + 'filters so the matches you want are the newest ones.';
  }
  if (stats.stopped_by === 'timeout') {
    return `The scan used its whole ${asked.timeoutMs / 1000}s budget after reading `
      + `${stats.files_scanned.toLocaleString()} files, and stopped. Older logs were never opened. `
      + 'Raise the time budget or narrow the filters.';
  }
  if (stats.stopped_by === 'max_files') {
    return `Only the newest ${asked.maxFiles.toLocaleString()} task logs in scope were considered — `
      + 'anything older was never opened. Raise the file cap, or filter to one workflow or date range '
      + 'so the cap covers all of it.';
  }
  return 'The scan stopped before reading everything in scope.';
}

/* ─── One match, with its context ──────────────────────── */
function Hit({ hit, highlighter }: { hit: LogMatch; highlighter: RegExp | null }) {
  const firstNumber = hit.line_number - hit.context_before.length;
  return (
    <article className="logsearch-hit">
      <div className="logsearch-hit-head">
        <span className="logsearch-hit-task" title={hit.task_name}>{hit.task_name}</span>
        <span className={clsx('logsearch-stream', `logsearch-stream--${hit.stream}`)}>{hit.stream}</span>
        <span className="logsearch-hit-meta">{hit.task_status.replace(/_/g, ' ')}</span>
        {hit.attempt > 1 && <span className="logsearch-hit-meta">attempt {hit.attempt}</span>}
        <span className="logsearch-hit-meta">line {hit.line_number.toLocaleString()}</span>
        <Link className="logsearch-open"
              to={`/runs/${hit.workflow_run_id}#task-${encodeURIComponent(hit.task_name)}`}
              title={`Open ${hit.task_name} on run #${hit.workflow_run_id} and read the full log`}>
          Open log <ChevronRight size={12} />
        </Link>
      </div>
      <pre className="logsearch-lines">
        {hit.context_before.map((line, i) => (
          <span key={`b${i}`} className="logsearch-line logsearch-line--context">
            <span className="logsearch-gutter">{firstNumber + i}</span>
            <span className="logsearch-text">{line || ' '}</span>
          </span>
        ))}
        <span className="logsearch-line logsearch-line--match">
          <span className="logsearch-gutter">{hit.line_number}</span>
          <span className="logsearch-text">{highlight(hit.line, highlighter)}</span>
        </span>
        {hit.context_after.map((line, i) => (
          <span key={`a${i}`} className="logsearch-line logsearch-line--context">
            <span className="logsearch-gutter">{hit.line_number + 1 + i}</span>
            <span className="logsearch-text">{line || ' '}</span>
          </span>
        ))}
      </pre>
    </article>
  );
}

/* ─── Helpers ──────────────────────────────────────────── */
interface RunGroup {
  runId: number;
  workflowName: string;
  workflowId: number;
  runStatus: string;
  createdAt: string;
  hits: LogMatch[];
}

/** Insertion order is the server's order: newest run first, stderr before
 *  stdout within a task run. */
function groupByRun(matches: LogMatch[]): RunGroup[] {
  const groups = new Map<number, RunGroup>();
  for (const match of matches) {
    let group = groups.get(match.workflow_run_id);
    if (!group) {
      group = {
        runId: match.workflow_run_id, workflowName: match.workflow_name,
        workflowId: match.workflow_id, runStatus: match.run_status,
        createdAt: match.run_created_at, hits: [],
      };
      groups.set(match.workflow_run_id, group);
    }
    group.hits.push(match);
  }
  return [...groups.values()];
}

const escapeLiteral = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Highlighting is best-effort by design: Python's regex dialect is not
 *  JavaScript's, so a pattern the server accepted can be unusable here. When
 *  that happens the line renders unhighlighted rather than marking the wrong
 *  span of text. */
function buildHighlighter(query: string, regex: boolean, caseSensitive: boolean): RegExp | null {
  try {
    return new RegExp(regex ? query : escapeLiteral(query), caseSensitive ? 'g' : 'gi');
  } catch {
    return null;
  }
}

function highlight(line: string, pattern: RegExp | null): ReactNode {
  if (!pattern) return line;
  const parts: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  pattern.lastIndex = 0;
  for (let found = pattern.exec(line); found !== null; found = pattern.exec(line)) {
    if (found.index > cursor) parts.push(line.slice(cursor, found.index));
    if (found[0]) {
      parts.push(<mark key={key++} className="log-match">{found[0]}</mark>);
      cursor = found.index + found[0].length;
    } else {
      // A zero-width match would spin forever; step past it.
      pattern.lastIndex++;
    }
    if (parts.length > 200) break; // pathological pattern on a 2000-char line
  }
  if (cursor === 0) return line;
  parts.push(line.slice(cursor));
  return parts;
}

const clamp = (value: number, low: number, high: number) =>
  Number.isNaN(value) ? low : Math.min(high, Math.max(low, value));
