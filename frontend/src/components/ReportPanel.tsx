import { ReactNode, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, BookOpen, Check, Clock, Copy, Download, ExternalLink, FileX,
  Maximize2, Minimize2, NotebookText, PackageOpen, RefreshCw,
} from 'lucide-react';
import clsx from 'clsx';
import { post } from '../api';
import { Button, EmptyState, LoadingBar, StatusBadge } from './ui';
import { useToast } from './toast';

/* ─── Backend shapes ──────────────────────────────────────
   Mirrors reports.run_outputs() / reports.latest_report_meta(); ShareRunModal
   imports RunOutputs from here so the two surfaces cannot drift. */

export type ReportEntry = {
  task_run_id: number;
  task_name: string | null;
  notebook_artifact_id: number;
  notebook_name: string;
  notebook_bytes: number | null;
  report_artifact_id: number | null;
  /** Only set once the render is cached on disk. */
  report_bytes: number | null;
  rendered: boolean;
  /** Server-built and already URL-quoted — never rebuild this from task_name. */
  report_url: string;
};

export type LogEntry = {
  task_run_id: number;
  task_name: string | null;
  stdout_bytes: number;
  stderr_bytes: number;
};

export type RunOutputs = {
  run_id: number;
  workflow_name: string | null;
  status: string;
  reports: ReportEntry[];
  logs: LogEntry[];
  renderer_available: boolean;
  estimated_export_bytes: {
    with_report: number;
    without_report: number;
    logs_none: number;
  };
};

export type LatestReportMeta = {
  workflow_id: number;
  workflow_name: string;
  run_id: number;
  run_finished_at: string;
  trigger_type: string;
  task_name: string | null;
  report_url: string;
  permalink: string;
  notebook_artifact_id: number;
  stale: boolean;
  age_seconds: number;
  failed_since: number;
  newer_successful_run_id: number | null;
  workflow_enabled: boolean;
};

/** The typed failure body every reports.py route renders by hand. */
type Failure = { code: string; detail: string };

/* ─── Local formatting ────────────────────────────────────
   Same behaviour as App.tsx's helpers, which are module-private there. */

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function formatDate(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
    hour: '2-digit', minute: '2-digit',
  });
}

function timeAgo(value?: string | null): string {
  if (!value) return '—';
  const ms = Date.now() - new Date(value).getTime();
  if (ms < 60_000) return 'just now';
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

/** Coarse age, for a sentence rather than a table cell. */
function humanAge(seconds: number): string {
  if (seconds < 3_600) return `${Math.max(1, Math.floor(seconds / 60))} minutes`;
  if (seconds < 86_400) {
    const hours = Math.floor(seconds / 3_600);
    return hours === 1 ? 'an hour' : `${hours} hours`;
  }
  const days = Math.floor(seconds / 86_400);
  return days === 1 ? 'a day' : `${days} days`;
}

/* ─── Frame constants ─────────────────────────────────────
   The document inside the frame is arbitrary notebook-authored HTML and JS. */

/** Mirrors the `sandbox` directive inside REPORT_CSP (src/runrail/reports.py).
 *  The CSP carries it too — that is what protects a pasted /latest link, which
 *  is a top-level navigation where this attribute does nothing — but the
 *  attribute must be here as well so the frame is sandboxed even if a proxy
 *  strips response headers.
 *
 *  allow-same-origin must NEVER be added: paired with allow-scripts it is
 *  equivalent to no sandbox at all, and a notebook cell's output would then be
 *  able to read RunRail's DOM, storage and cookies. */
const FRAME_SANDBOX = 'allow-scripts allow-popups allow-downloads';

const MIN_FRAME_HEIGHT = 320;
/** A pathological notebook can report a six-figure scrollHeight; past this the
 *  frame keeps its own scrollbar rather than making the page unusable. */
const MAX_FRAME_HEIGHT = 24_000;
/** Shown until the document volunteers its height, which is one paint away. */
const DEFAULT_FRAME_HEIGHT = 640;

const INSTALL_COMMAND = "pip install 'runrail[notebook]'";

/** Runs whose report would be a snapshot of something still moving. */
const IN_PROGRESS = new Set(['queued', 'running', 'waiting_approval']);

function withNonce(url: string, nonce: number): string {
  if (!nonce) return url;
  return `${url}${url.includes('?') ? '&' : '?'}_=${nonce}`;
}

/* ─── Typed fetch ─────────────────────────────────────────
   api() collapses a failure into an Error message, which loses the machine
   `code` these panels branch on, so both requests are made by hand. */

async function fetchTyped<T>(
  url: string, signal?: AbortSignal,
): Promise<{ ok: true; data: T } | ({ ok: false } & Failure)> {
  const response = await fetch(url, { signal, headers: { Accept: 'application/json' } });
  if (response.ok) return { ok: true, data: await response.json() as T };
  return { ok: false, ...await readFailure(response) };
}

async function readFailure(response: Response): Promise<Failure> {
  const fallback: Failure = {
    code: 'request_failed',
    detail: `The server answered ${response.status}.`,
  };
  try {
    const body = await response.json() as { code?: unknown; detail?: unknown };
    return {
      code: typeof body.code === 'string' ? body.code : fallback.code,
      detail: typeof body.detail === 'string' ? body.detail : fallback.detail,
    };
  } catch {
    return fallback; // not the typed {detail, code} body — keep the status line
  }
}

/** Resolve what the report URL will actually do before pointing a frame at it.
 *
 *  The route answers with either the rendered HTML or a JSON failure at a
 *  non-200 status, and an <iframe> would happily display that JSON. Probing
 *  first also warms the on-disk cache: the render is lazy and synchronous, so
 *  this request is the slow one and the frame's own load is a cached file. */
type Phase =
  | { phase: 'rendering' }
  | { phase: 'ready' }
  | ({ phase: 'failed' } & Failure);

function useReportProbe(src: string | null, nonce: number): Phase {
  const [state, setState] = useState<Phase>({ phase: 'rendering' });
  useEffect(() => {
    if (!src) return;
    const controller = new AbortController();
    setState({ phase: 'rendering' });
    void (async () => {
      try {
        const response = await fetch(src, {
          signal: controller.signal, headers: { Accept: 'text/html' },
        });
        if (response.ok) {
          // The body IS the report. The frame below asks for the same URL and
          // gets the file this call just rendered, so drop a few hundred KB of
          // inlined JupyterLab CSS rather than decode it into a JS string.
          try { await response.body?.cancel(); } catch { /* already drained */ }
          setState({ phase: 'ready' });
          return;
        }
        setState({ phase: 'failed', ...await readFailure(response) });
      } catch (error) {
        if (controller.signal.aborted) return;
        setState({
          phase: 'failed', code: 'unreachable',
          detail: error instanceof Error ? error.message : 'The API is unreachable.',
        });
      }
    })();
    return () => controller.abort();
  }, [src, nonce]);
  return state;
}

/* ─── The frame ───────────────────────────────────────── */

function ReportFrame({ src, title, fullscreen }: {
  src: string; title: string; fullscreen: boolean;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(DEFAULT_FRAME_HEIGHT);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => { setLoaded(false); setHeight(DEFAULT_FRAME_HEIGHT); }, [src]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frame = frameRef.current;
      // Identity, not origin. The sandbox has no allow-same-origin, so the
      // document runs at an opaque origin and event.origin is the literal
      // string "null" for every such frame on the page — it identifies
      // nothing. The window handle is what names *this* report.
      if (!frame || !event.source || event.source !== frame.contentWindow) return;
      if (event.origin !== 'null' && event.origin !== window.location.origin) return;
      const data = event.data as Record<string, unknown> | null;
      if (typeof data !== 'object' || data === null || Array.isArray(data)) return;
      const reported = data.runrailReportHeight;
      if (typeof reported !== 'number' || !Number.isFinite(reported) || reported <= 0) return;
      // +4px absorbs sub-pixel rounding that would otherwise leave the frame
      // one pixel short and give the report a scrollbar of its own.
      setHeight(Math.min(Math.max(Math.round(reported) + 4, MIN_FRAME_HEIGHT), MAX_FRAME_HEIGHT));
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  return (
    <iframe
      ref={frameRef}
      className={clsx('report-frame', loaded && 'report-frame--in')}
      title={title}
      src={src}
      sandbox={FRAME_SANDBOX}
      referrerPolicy="no-referrer"
      onLoad={() => setLoaded(true)}
      // Full screen hands scrolling back to the document: the measured height
      // would be taller than the viewport and the page cannot scroll behind it.
      style={{ height: fullscreen ? '100%' : height }}
    />
  );
}

/* ─── Failure states ──────────────────────────────────── */

function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="report-copy-cmd"
      title="Copy to clipboard"
      onClick={() => {
        void navigator.clipboard?.writeText(command).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 2000);
        }).catch(() => { /* clipboard blocked — the text is on screen anyway */ });
      }}
    >
      <code>{command}</code>
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}

function ReportProblem({ failure, notebookArtifactId, onRerender, busy }: {
  failure: Failure;
  notebookArtifactId: number | null;
  onRerender?: () => void;
  busy?: boolean;
}) {
  const rerender = onRerender && (
    <Button variant="ghost" size="sm" onClick={onRerender} disabled={busy}>
      <RefreshCw size={13} className={busy ? 'icon-spin' : undefined} /> Re-render
    </Button>
  );
  const download = notebookArtifactId != null && (
    <a className="btn btn-ghost btn-sm" href={`/api/artifacts/${notebookArtifactId}/download`} download>
      <Download size={13} /> Download .ipynb
    </a>
  );

  const wrap = (hint: ReactNode, buttons: ReactNode) => (
    <div className="report-problem-actions">
      {hint}
      <div className="report-problem-buttons">{buttons}</div>
    </div>
  );

  switch (failure.code) {
    case 'no_notebook':
      return (
        <EmptyState
          icon={<NotebookText size={22} />}
          title="No notebook report"
          text="This run produced no executed notebook. Add a notebook task to the workflow and its rendered output appears here."
        />
      );
    case 'renderer_missing':
      return (
        <EmptyState
          icon={<PackageOpen size={22} />}
          title="Notebook rendering is not installed"
          text="RunRail renders notebooks with nbconvert, which ships as an optional extra. Install it wherever the API runs, restart it, then re-render."
          // Deliberately not failure.detail: the point is the command, and an
          // ImportError traceback teaches the reader nothing they can act on.
          action={wrap(<CopyCommand command={INSTALL_COMMAND} />, <>{rerender}{download}</>)}
        />
      );
    case 'render_failed':
      return (
        <EmptyState
          icon={<AlertTriangle size={22} />}
          title="The notebook could not be rendered"
          text="nbconvert rejected this notebook. It usually means the file is truncated or was written by a much newer Jupyter."
          action={wrap(
            <code className="report-problem-detail">{failure.detail}</code>,
            <>{rerender}{download}</>,
          )}
        />
      );
    case 'source_removed':
      return (
        <EmptyState
          icon={<FileX size={22} />}
          title="The notebook was cleaned up"
          text="Retention removed the .ipynb this report renders from, so it cannot be rebuilt. Runs older than the retention window keep only what was already rendered."
        />
      );
    case 'notebook_too_large':
      return (
        <EmptyState
          icon={<AlertTriangle size={22} />}
          title="Too large to render"
          text={`${failure.detail}. Download the notebook and open it locally, or trim the cell outputs that made it this big.`}
          action={wrap(null, download)}
        />
      );
    case 'no_successful_run':
      return (
        <EmptyState
          icon={<Clock size={22} />}
          title="Nothing to show yet"
          text={`${failure.detail}. This link starts working after the first successful run that writes a notebook.`}
        />
      );
    case 'no_report_in_any_successful_run':
      return (
        <EmptyState
          icon={<NotebookText size={22} />}
          title="No successful run has a notebook"
          text={`${failure.detail}. Check that the notebook task is not being skipped.`}
        />
      );
    case 'no_such_workflow':
      return (
        <EmptyState icon={<FileX size={22} />} title="No such workflow" text={failure.detail} />
      );
    default:
      return (
        <EmptyState
          icon={<AlertTriangle size={22} />}
          title="The report could not be loaded"
          text={failure.detail}
          action={wrap(null, <>{rerender}{download}</>)}
        />
      );
  }
}

/* ─── Shell ───────────────────────────────────────────────
   One implementation of the frame, its controls and its states, shared by the
   per-run panel and the /latest page so the two cannot diverge. */

function ReportShell({
  title, subtitle, src, notebookArtifactId, tabs, banner, problem, canRerender = true,
}: {
  title: string;
  subtitle?: ReactNode;
  /** null when the caller already knows there is nothing to point at. */
  src: string | null;
  notebookArtifactId: number | null;
  tabs?: ReactNode;
  banner?: ReactNode;
  /** A failure resolved before the report URL was reached (metadata lookups). */
  problem?: Failure | null;
  canRerender?: boolean;
}) {
  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const { toast } = useToast();
  const url = src ? withNonce(src, nonce) : null;
  const state = useReportProbe(problem ? null : url, nonce);

  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.defaultPrevented) return;
      // Claim the key so a Modal we might be inside does not also close.
      event.preventDefault();
      setFullscreen(false);
    };
    document.addEventListener('keydown', onKey);
    const restore = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = restore;
    };
  }, [fullscreen]);

  const rerender = async () => {
    if (notebookArtifactId == null) return;
    setBusy(true);
    try {
      await post(`/artifacts/${notebookArtifactId}/render`, {});
      toast('Report re-rendered');
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not re-render the report', 'error');
    } finally {
      setBusy(false);
      // Bump either way: on failure the probe re-runs and the panel shows the
      // typed reason instead of a stale success.
      setNonce(value => value + 1);
    }
  };

  const failure: Failure | null =
    problem ?? (state.phase === 'failed' ? { code: state.code, detail: state.detail } : null);

  return (
    <section className={clsx('panel report-panel', fullscreen && 'report-panel--fullscreen')}>
      <div className="panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        <div className="report-actions">
          {url && state.phase === 'ready' && (
            <a className="btn btn-ghost btn-sm" href={url} target="_blank" rel="noreferrer"
               title="Open the rendered report in its own tab">
              <ExternalLink size={13} /> Open
            </a>
          )}
          {notebookArtifactId != null && (
            <a className="btn btn-ghost btn-sm" download
               href={`/api/artifacts/${notebookArtifactId}/download`}
               title="Download the executed notebook this report was rendered from">
              <Download size={13} /> .ipynb
            </a>
          )}
          {canRerender && notebookArtifactId != null && (
            <Button variant="ghost" size="sm" onClick={() => void rerender()} disabled={busy}
                    title="Discard the cached render and build it again">
              <RefreshCw size={13} className={busy ? 'icon-spin' : undefined} /> Re-render
            </Button>
          )}
          {state.phase === 'ready' && (
            <Button variant="ghost" size="sm" onClick={() => setFullscreen(!fullscreen)}
                    title={fullscreen ? 'Exit full screen (Esc)' : 'Expand to full screen'}>
              {fullscreen ? <><Minimize2 size={13} /> Exit</> : <><Maximize2 size={13} /> Full screen</>}
            </Button>
          )}
        </div>
      </div>

      {banner}
      {tabs}

      <div className="report-body">
        {failure ? (
          <ReportProblem
            failure={failure}
            notebookArtifactId={notebookArtifactId}
            onRerender={canRerender && notebookArtifactId != null ? () => void rerender() : undefined}
            busy={busy}
          />
        ) : state.phase === 'rendering' || !url ? (
          <div className="report-rendering">
            <LoadingBar />
            <p>
              Rendering the notebook. The first view converts it and caches the
              result — later views are instant.
            </p>
          </div>
        ) : (
          <ReportFrame src={url} title={title} fullscreen={fullscreen} />
        )}
      </div>
    </section>
  );
}

/* ─── Run detail panel ────────────────────────────────── */

export interface ReportPanelProps {
  runId: number | string;
  /** Run status from the page, so an in-flight run explains itself. */
  runStatus?: string;
  /** Open this notebook task first, when the run has several. */
  initialTask?: string | null;
  /** Render nothing at all when the run produced no notebook (default true) —
   *  an empty report panel on every shell-only run is noise. */
  hideWhenEmpty?: boolean;
}

export function ReportPanel({
  runId, runStatus, initialTask, hideWhenEmpty = true,
}: ReportPanelProps) {
  const [outputs, setOutputs] = useState<RunOutputs | null>(null);
  const [problem, setProblem] = useState<Failure | null>(null);
  const [activeTask, setActiveTask] = useState<string | null>(initialTask ?? null);

  useEffect(() => {
    const controller = new AbortController();
    setOutputs(null);
    setProblem(null);
    void fetchTyped<RunOutputs>(`/api/runs/${runId}/outputs`, controller.signal)
      .then(result => {
        if (result.ok) setOutputs(result.data);
        else setProblem({ code: result.code, detail: result.detail });
      })
      .catch(() => { if (!controller.signal.aborted) setProblem({ code: 'unreachable', detail: 'Could not reach the API.' }); });
    return () => controller.abort();
  }, [runId]);

  useEffect(() => { setActiveTask(initialTask ?? null); }, [runId, initialTask]);

  if (problem) {
    return <ReportShell title="Notebook report" src={null} notebookArtifactId={null} problem={problem} />;
  }

  if (!outputs) {
    return (
      <section className="panel report-panel">
        <div className="panel-head"><div><h2>Notebook report</h2><p>Looking for notebook output…</p></div></div>
        <div className="report-body"><div className="report-rendering"><LoadingBar /></div></div>
      </section>
    );
  }

  const live = IN_PROGRESS.has(runStatus ?? outputs.status);

  if (outputs.reports.length === 0) {
    if (hideWhenEmpty && !live) return null;
    return (
      <section className="panel report-panel">
        <div className="panel-head"><div><h2>Notebook report</h2></div></div>
        <div className="report-body">
          {live ? (
            <div className="report-rendering">
              <LoadingBar />
              <p>This run is still going. A notebook report appears here as soon as a notebook task finishes.</p>
            </div>
          ) : (
            <ReportProblem
              failure={{ code: 'no_notebook', detail: 'This run produced no notebook output' }}
              notebookArtifactId={null}
            />
          )}
        </div>
      </section>
    );
  }

  const active = outputs.reports.find(entry => entry.task_name === activeTask) ?? outputs.reports[0];
  const tabs = outputs.reports.length > 1 ? (
    <div className="report-tabstrip">
      <div className="segmented">
        {outputs.reports.map(entry => (
          <button
            key={entry.task_run_id}
            type="button"
            className={clsx(entry === active && 'active')}
            onClick={() => setActiveTask(entry.task_name)}
            title={entry.notebook_name}
          >
            <BookOpen size={12} />
            {entry.task_name ?? `task ${entry.task_run_id}`}
          </button>
        ))}
      </div>
    </div>
  ) : undefined;

  const size = active.rendered && active.report_bytes != null
    ? `${formatBytes(active.report_bytes)} rendered`
    : 'renders on first view';

  // With one notebook, use the bare permalink instead of the ?task= form: the
  // route already defaults to the lowest-Task.id notebook, and an unnamed task
  // would otherwise send `?task=` — an empty string, which matches no task.
  const src = outputs.reports.length === 1 ? `/api/runs/${runId}/report` : active.report_url;

  return (
    <ReportShell
      title="Notebook report"
      subtitle={`${active.notebook_name} · ${size}`}
      src={src}
      notebookArtifactId={active.notebook_artifact_id}
      tabs={tabs}
    />
  );
}

/* ─── /latest page ────────────────────────────────────────
   The pinnable surface. Everything extra here exists because the reader
   arrived from a wiki link months later and cannot see what they are missing:
   which run this is, when it finished, and whether it is still the truth. */

export interface LatestReportPanelProps {
  /** Workflow id or exact name — resolve_workflow() accepts either. */
  workflow: string | number;
  /** Called once metadata resolves, so the host page can title itself. */
  onMeta?: (meta: LatestReportMeta) => void;
}

export function LatestReportPanel({ workflow, onMeta }: LatestReportPanelProps) {
  const [meta, setMeta] = useState<LatestReportMeta | null>(null);
  const [problem, setProblem] = useState<Failure | null>(null);
  const metaRef = useRef(onMeta);
  metaRef.current = onMeta;

  useEffect(() => {
    const controller = new AbortController();
    setMeta(null);
    setProblem(null);
    const reference = encodeURIComponent(String(workflow));
    void fetchTyped<LatestReportMeta>(`/api/workflows/${reference}/latest-report`, controller.signal)
      .then(result => {
        if (result.ok) { setMeta(result.data); metaRef.current?.(result.data); }
        else setProblem({ code: result.code, detail: result.detail });
      })
      .catch(() => { if (!controller.signal.aborted) setProblem({ code: 'unreachable', detail: 'Could not reach the API.' }); });
    return () => controller.abort();
  }, [workflow]);

  if (problem) {
    return <ReportShell title="Latest report" src={null} notebookArtifactId={null} problem={problem} />;
  }
  if (!meta) {
    return (
      <section className="panel report-panel">
        <div className="panel-head"><div><h2>Latest report</h2><p>Resolving the newest successful run…</p></div></div>
        <div className="report-body"><div className="report-rendering"><LoadingBar /></div></div>
      </section>
    );
  }

  const reasons: string[] = [];
  if (meta.failed_since > 0) {
    reasons.push(meta.failed_since === 1
      ? 'A run has failed since this one, so the workflow may be broken.'
      : `${meta.failed_since} runs have failed since this one, so the workflow may be broken.`);
  }
  if (meta.age_seconds > 86_400) {
    reasons.push(`Nothing newer has succeeded with a notebook in ${humanAge(meta.age_seconds)}.`);
  }
  if (meta.newer_successful_run_id != null) {
    reasons.push(`Run #${meta.newer_successful_run_id} succeeded more recently but produced no notebook.`);
  }
  if (!meta.workflow_enabled) {
    reasons.push('This workflow is paused, so it will not refresh on its own.');
  }

  const banner = (
    <>
      <div className="report-freshness">
        <div><span>Run</span><Link className="run-id" to={`/runs/${meta.run_id}`}>#{meta.run_id}</Link></div>
        <div>
          <span>Finished</span>
          <strong title={formatDate(meta.run_finished_at)}>{timeAgo(meta.run_finished_at)}</strong>
        </div>
        <div><span>Status</span><StatusBadge value="success" /></div>
        <div><span>Task</span><strong title={meta.task_name ?? ''}>{meta.task_name ?? '—'}</strong></div>
        <div><span>Trigger</span><strong>{meta.trigger_type}</strong></div>
      </div>
      {meta.stale && (
        <div className="report-stale" role="status">
          <AlertTriangle size={16} />
          <div>
            <strong>These numbers are {humanAge(meta.age_seconds)} old.</strong>
            <p>{reasons.join(' ')}</p>
          </div>
          {meta.newer_successful_run_id != null && (
            <Link className="btn btn-secondary btn-sm" to={`/runs/${meta.newer_successful_run_id}`}>
              Newer run
            </Link>
          )}
        </div>
      )}
    </>
  );

  return (
    <ReportShell
      title={`${meta.workflow_name} · latest report`}
      subtitle={<>Permalink for this exact run: <code>{meta.permalink}</code></>}
      src={meta.report_url}
      notebookArtifactId={meta.notebook_artifact_id}
      banner={banner}
    />
  );
}
