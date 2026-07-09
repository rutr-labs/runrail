import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal, flushSync } from 'react-dom';
import { Link, NavLink, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  LayoutDashboard, GitBranch, History, FolderOpen,
  Cpu, Package, Play, Plus, Search, ChevronRight,
  Clock, Calendar, Zap, GitMerge, Activity,
  AlertTriangle, CheckCircle2, RefreshCw, Trash2, Pencil,
  ArrowLeft, Terminal, Download, Settings, ChevronsRight,
  Database, FileText, Info, Ban, Menu, X, SlidersHorizontal,
} from 'lucide-react';
import { api, del, post, put } from './api';
import { rrws } from './ws';
import { FilePicker } from './components/FilePicker';
import { DagGraph } from './components/DagGraph';
import { RunHeatmap } from './components/Heatmap';
import { Button, StatusBadge, MetricCard, EmptyState, Modal, PageHeader, TaskTypeBadge, SkeletonCard, HealthChip, LoadingBar } from './components/ui';
import { LogViewer } from './components/LogViewer';
import { useToast } from './components/toast';

/* ─── Types ───────────────────────────────────────────── */
type Run = {
  id: number;
  workflow_id: number;
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  trigger_type: string;
  run_key?: string | null;
  parameters_json?: Record<string, unknown> | null;
  task_runs?: TaskRun[];
};

type TaskRun = {
  id: number;
  workflow_run_id: number;
  task_id: number;
  task_name?: string | null;
  task_type?: string | null;
  status: string;
  attempt: number;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  exit_code?: number | null;
  error_message?: string | null;
  rendered_command?: string | null;
};

type Workflow = {
  id: number;
  name: string;
  description?: string | null;
  schedule_cron?: string | null;
  enabled: boolean;
  max_concurrent_runs: number;
  project_id?: number | null;
  default_environment_id?: number | null;
  notify_webhook_url?: string | null;
  auto_pause_failures?: number | null;
};

type Project = {
  id: number;
  name: string;
  description?: string | null;
  root_path: string;
  default_environment_id?: number | null;
};

type Env = {
  id: number;
  name: string;
  env_type: string;
  executable?: string | null;
  conda_env?: string | null;
  env_vars_json?: Record<string, unknown> | null;
  description?: string | null;
  managed: boolean;
  status: string;
  base_executable?: string | null;
  packages_json: string[];
  active_packages_json: string[];
  python_version?: string | null;
  build_log?: string | null;
  last_error?: string | null;
};

type Task = {
  id: number;
  workflow_id?: number | null;
  project_id?: number | null;
  environment_id?: number | null;
  name: string;
  task_type: string;
  command?: string | null;
  script_path?: string | null;
  notebook_path?: string | null;
  sql_path?: string | null;
  cwd?: string | null;
  depends_on_json: string[];
  parameters_json?: Record<string, unknown> | null;
  retries: number;
  retry_delay_seconds: number;
  timeout_seconds?: number | null;
};

/* ─── Helpers ─────────────────────────────────────────── */
const DAY = 86_400_000;
const LIVE = (status: string) => status === 'running' || status === 'queued';

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
  if (ms < DAY) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / DAY)}d ago`;
}

function formatDuration(seconds?: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

const environmentUsable = (e: Env) => e.status === 'ready' || e.status === 'degraded';

/** Ticks every second while `active`, so live durations count up. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function liveDuration(run: Run, now: number): string {
  if (run.duration_seconds != null) return formatDuration(run.duration_seconds);
  if (run.status === 'running' && run.started_at) {
    return formatDuration((now - new Date(run.started_at).getTime()) / 1000);
  }
  return '—';
}

/* ─── Cron helpers (minute/hour steps + daily/weekly) ─── */
const stepOf = (part: string): number | null => {
  const match = part.match(/^\*\/(\d+)$/);
  return match ? Number(match[1]) || null : null;
};

/** Schedules evaluate in UTC on the server, so occurrences are computed in UTC.
 *  Display always converts to the viewer's local timezone. */
function nextCronOccurrence(expr: string, after = new Date()): Date | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minutePart, hourPart, domPart, monthPart, dowPart] = parts;
  if (domPart !== '*' || monthPart !== '*') return null;

  const next = new Date(after);
  next.setUTCSeconds(0, 0);

  // Every minute / every N minutes: "* * * * *", "*/5 * * * *"
  const minuteStep = minutePart === '*' ? 1 : stepOf(minutePart);
  if (minuteStep != null && hourPart === '*' && dowPart === '*') {
    do { next.setUTCMinutes(next.getUTCMinutes() + 1); }
    while (next.getUTCMinutes() % minuteStep !== 0);
    return next;
  }

  const minute = minutePart === '*' ? 0 : Number(minutePart);
  if (Number.isNaN(minute)) return null;

  // Hourly at a fixed minute: "30 * * * *"
  if (hourPart === '*' && dowPart === '*') {
    next.setUTCMinutes(minute, 0, 0);
    if (next <= after) next.setUTCHours(next.getUTCHours() + 1);
    return next;
  }

  // Every N hours: "M */6 * * *"
  const hourStep = stepOf(hourPart);
  if (hourStep != null && dowPart === '*') {
    next.setUTCMinutes(minute, 0, 0);
    while (next <= after || next.getUTCHours() % hourStep !== 0) {
      next.setUTCHours(next.getUTCHours() + 1);
    }
    return next;
  }

  const hour = hourPart === '*' ? 0 : Number(hourPart);
  if (Number.isNaN(hour)) return null;
  next.setUTCHours(hour, minute, 0, 0);

  if (dowPart === '*') {
    if (next <= after) next.setUTCDate(next.getUTCDate() + 1);
    return next;
  }

  const weekday = Number(dowPart);
  if (Number.isNaN(weekday)) return null;
  let delta = (weekday - next.getUTCDay() + 7) % 7;
  if (delta === 0 && next <= after) delta = 7;
  next.setUTCDate(next.getUTCDate() + delta);
  return next;
}

/** Human label for a cron, in the viewer's local timezone so it agrees with the
 *  timestamps shown next to it (the raw expression stays UTC on the server). */
function cronLabel(expr: string): string {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return expr;
  const [minutePart, hourPart, domPart, monthPart, dowPart] = parts;
  if (domPart !== '*' || monthPart !== '*') return expr;

  if (hourPart === '*' && dowPart === '*') {
    if (minutePart === '*') return 'every minute';
    const minuteStep = stepOf(minutePart);
    if (minuteStep) return `every ${minuteStep} min`;
  }
  const hourStep = stepOf(hourPart);
  if (hourStep && dowPart === '*') {
    return hourStep === 1 ? 'hourly' : `every ${hourStep}h`;
  }

  // Fixed-time schedules: derive the label from a real occurrence so the
  // local-time conversion (including weekday shifts across midnight) is exact.
  const next = nextCronOccurrence(expr);
  if (!next) return expr;
  const time = next.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (hourPart === '*' && dowPart === '*') {
    return `hourly at :${String(next.getMinutes()).padStart(2, '0')}`;
  }
  if (dowPart === '*') return `daily at ${time}`;
  return `every ${next.toLocaleDateString(undefined, { weekday: 'short' })} at ${time}`;
}

const REDUCED_MOTION = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Native View Transition between pages when supported: the run id chip morphs
 *  into the run title. Falls back to plain navigation everywhere else. */
function navigateWithTransition(navTo: (to: string) => void, to: string) {
  const doc = document as Document & { startViewTransition?: (cb: () => void) => { finished: Promise<void> } };
  if (!doc.startViewTransition || REDUCED_MOTION()) { navTo(to); return; }
  document.documentElement.classList.add('vt-active');
  const transition = doc.startViewTransition(() => { flushSync(() => navTo(to)); });
  transition.finished.finally(() => document.documentElement.classList.remove('vt-active'));
}

/** FLIP: children with data-flip-id glide to their new grid slots on re-sort. */
function useFlip(ref: React.RefObject<HTMLElement | null>, deps: unknown[]) {
  const previous = useRef(new Map<string, DOMRect>());
  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    const items = [...host.querySelectorAll<HTMLElement>('[data-flip-id]')];
    if (!REDUCED_MOTION()) {
      for (const el of items) {
        const id = el.dataset.flipId!;
        const prev = previous.current.get(id);
        const next = el.getBoundingClientRect();
        if (prev && (Math.abs(prev.left - next.left) > 1 || Math.abs(prev.top - next.top) > 1)) {
          el.style.transition = 'none';
          el.style.transform = `translate(${prev.left - next.left}px, ${prev.top - next.top}px)`;
          void el.offsetHeight; // reflow so the next transition starts from here
          el.style.transition = 'transform .45s var(--ease-out)';
          el.style.transform = '';
          el.addEventListener('transitionend', () => { el.style.transition = ''; }, { once: true });
        }
      }
    }
    previous.current = new Map(items.map(el => [el.dataset.flipId!, el.getBoundingClientRect()]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** Runs with live statuses keep pages fresh even when the WebSocket is unavailable
 *  (for example when the API and worker run as separate processes). */
function useLiveRefresh(hasLive: boolean, refresh: () => void, intervalMs = 3000) {
  // Keep the latest callback in a ref so a new `refresh` identity every render
  // (common — callers define it inline, and a 1s useNow ticker re-renders while
  // a run is live) does not tear down and recreate the interval before it ever
  // fires. Without this the poll silently never runs and live views go stale.
  const saved = useRef(refresh);
  saved.current = refresh;
  useEffect(() => {
    if (!hasLive) return;
    const timer = window.setInterval(() => saved.current(), intervalMs);
    return () => window.clearInterval(timer);
  }, [hasLive, intervalMs]);
}

/* ─── Shell ───────────────────────────────────────────── */
const NAV: { href: string; icon: typeof LayoutDashboard; label: string; section: string }[] = [
  { href: '/',             icon: LayoutDashboard, label: 'Dashboard',    section: 'Overview' },
  { href: '/runs',         icon: History,         label: 'Runs',         section: 'Overview' },
  { href: '/workflows',    icon: GitBranch,       label: 'Workflows',    section: 'Build' },
  { href: '/projects',     icon: FolderOpen,      label: 'Projects',     section: 'Build' },
  { href: '/environments', icon: Cpu,             label: 'Environments', section: 'Build' },
  { href: '/artifacts',    icon: Package,         label: 'Artifacts',    section: 'Observe' },
  { href: '/settings',     icon: Settings,        label: 'Settings',     section: 'System' },
];

function useApiHealth(): 'online' | 'offline' | 'unknown' {
  const [health, setHealth] = useState<'online' | 'offline' | 'unknown'>('unknown');
  useEffect(() => {
    let cancelled = false;
    const check = () => api<{ status: string }>('/health')
      .then(() => !cancelled && setHealth('online'))
      .catch(() => !cancelled && setHealth('offline'));
    check();
    const timer = window.setInterval(check, 30_000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);
  return health;
}

function Shell() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const location = useLocation();
  const health = useApiHealth();
  useEffect(() => setMobileOpen(false), [location.pathname]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen(v => !v);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const page = NAV.find(n => n.href !== '/' && location.pathname.startsWith(n.href))
    ?? (location.pathname.startsWith('/runs') ? NAV[1] : NAV[0]);

  const sections = ['Overview', 'Build', 'Observe'];
  return (
    <div className="app-shell">
      <button className="mobile-nav-btn" onClick={() => setMobileOpen(v => !v)} aria-label="Toggle navigation">
        {mobileOpen ? <X size={17} /> : <Menu size={17} />}
      </button>
      {mobileOpen && <div className="sidebar-overlay" onClick={() => setMobileOpen(false)} />}
      <aside className={`sidebar${mobileOpen ? ' open' : ''}`}>
        <Link to="/" className="sidebar-brand">
          <span className="sidebar-logo"><span className="sidebar-logo-inner"><span /><span /><span /></span></span>
          <span className="sidebar-wordmark"><b>RunRail</b><span>Control plane</span></span>
        </Link>
        <nav className="sidebar-nav">
          {sections.map(section => (
            <div key={section}>
              <div className="sidebar-section-label">{section}</div>
              {NAV.filter(n => n.section === section).map(({ href, icon: Icon, label }) => (
                <NavLink key={href} to={href} end={href === '/'}
                  className={({ isActive }) => `sidebar-nav-item${isActive ? ' active' : ''}`}>
                  <span className="nav-icon"><Icon size={15} /></span>{label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-footer">
          <NavLink to="/settings" className={({ isActive }) => `sidebar-nav-item${isActive ? ' active' : ''}`}>
            <span className="nav-icon"><Settings size={15} /></span>Settings
          </NavLink>
          <div className="sidebar-status-row">
            <span className={`live-dot${health === 'online' ? '' : ' offline'}`} />
            {health === 'online' ? 'Connected' : health === 'offline' ? 'API unreachable' : 'Connecting…'}
          </div>
        </div>
      </aside>
      <div className="workspace">
        <div className="topbar">
          <div className="topbar-breadcrumb">
            <span className="bc-root">RunRail</span>
            <span className="bc-sep">›</span>
            <span className="bc-page">{page.label}</span>
          </div>
          <div className="topbar-actions">
            <button className="cmd-k-btn" onClick={() => setPaletteOpen(true)} aria-label="Open command palette">
              <Search size={13} /> Search
              <kbd>{navigator.platform?.includes('Mac') ? '⌘K' : 'Ctrl K'}</kbd>
            </button>
            <HealthChip label={health === 'online' ? 'Healthy' : health === 'offline' ? 'Offline' : 'Checking'} status={health} />
            <ThemeToggle />
          </div>
        </div>
        {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}
        <main key={location.pathname}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/environments" element={<Environments />} />
            <Route path="/workflows" element={<Workflows />} />
            <Route path="/workflows/:id" element={<WorkflowDetail />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/runs/:id" element={<RunDetail />} />
            <Route path="/artifacts" element={<Artifacts />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme ?? 'dark');
  const apply = (t: string) => {
    document.documentElement.dataset.theme = t;
    localStorage.setItem('runrail-theme', t);
    setTheme(t);
  };
  return (
    <div className="theme-toggle" role="group" aria-label="Theme">
      <span className={theme === 'light' ? 'active' : ''} onClick={() => apply('light')}>☀</span>
      <span className={theme === 'dark' ? 'active' : ''} onClick={() => apply('dark')}>☾</span>
    </div>
  );
}

/* ─── Command palette (⌘K) ────────────────────────────── */
type Command = {
  id: string;
  label: string;
  hint?: string;
  section: string;
  icon: ReactNode;
  run: () => void;
};

function CommandPalette({ onClose }: { onClose: () => void }) {
  const navTo = useNavigate();
  const { toast } = useToast();
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(0);
  const [flows, setFlows] = useState<Workflow[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<Workflow[]>('/workflows').then(setFlows).catch(() => {});
    api<Run[]>('/runs?limit=6').then(setRuns).catch(() => {});
  }, []);

  const commands = useMemo<Command[]>(() => {
    const go = (path: string) => () => { onClose(); navTo(path); };
    const items: Command[] = NAV.map(({ href, icon: Icon, label }) => ({
      id: `nav-${href}`, label, hint: 'Page', section: 'Navigate',
      icon: <Icon size={14} />, run: go(href),
    }));
    items.push({
      id: 'nav-wallboard', label: 'Wallboard', hint: 'TV mode', section: 'Navigate',
      icon: <Activity size={14} />, run: go('/wallboard'),
    });
    for (const w of flows) {
      items.push({
        id: `trigger-${w.id}`, label: `Run ${w.name}`, hint: 'Trigger now',
        section: 'Workflows', icon: <Play size={14} />,
        run: async () => {
          onClose();
          try {
            const run = await post<Run>(`/workflows/${w.id}/run`, { parameters: {} });
            navTo(`/runs/${run.id}`);
          } catch (error) {
            toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
          }
        },
      });
      items.push({
        id: `open-${w.id}`, label: w.name, hint: 'Open workflow',
        section: 'Workflows', icon: <GitBranch size={14} />, run: go(`/workflows/${w.id}`),
      });
    }
    for (const r of runs) {
      const flow = flows.find(f => f.id === r.workflow_id);
      items.push({
        id: `run-${r.id}`, label: `#${r.id} · ${flow?.name ?? `Workflow ${r.workflow_id}`}`,
        hint: r.status, section: 'Recent runs',
        icon: <History size={14} />, run: go(`/runs/${r.id}`),
      });
    }
    return items;
  }, [flows, runs]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(c => c.label.toLowerCase().includes(q) || c.section.toLowerCase().includes(q));
  }, [commands, query]);

  useEffect(() => setSelected(0), [query]);
  useEffect(() => {
    listRef.current?.querySelector('.cmdk-item.selected')?.scrollIntoView({ block: 'nearest' });
  }, [selected]);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSelected(s => Math.min(s + 1, shown.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSelected(s => Math.max(s - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); shown[selected]?.run(); }
    else if (e.key === 'Escape') { e.preventDefault(); onClose(); }
  };

  return createPortal(
    <div className="cmdk-shade" onMouseDown={e => e.target === e.currentTarget && onClose()}>
      <div className="cmdk" role="dialog" aria-label="Command palette">
        <div className="cmdk-input-row">
          <Search size={16} />
          <input
            autoFocus
            placeholder="Search pages, workflows, runs…"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKey}
            aria-label="Command search"
          />
          <kbd className="cmdk-esc">esc</kbd>
        </div>
        <div className="cmdk-list" ref={listRef}>
          {shown.length === 0 && <div className="cmdk-empty">No matches for “{query}”</div>}
          {shown.map((c, i) => (
            <div key={c.id}>
              {c.section !== shown[i - 1]?.section && <div className="cmdk-section">{c.section}</div>}
              <button
                className={`cmdk-item${i === selected ? ' selected' : ''}`}
                onMouseEnter={() => setSelected(i)}
                onClick={() => c.run()}
              >
                <span className="cmdk-icon">{c.icon}</span>
                <span className="cmdk-label">{c.label}</span>
                {c.hint && <span className="cmdk-hint">{c.hint}</span>}
              </button>
            </div>
          ))}
        </div>
        <div className="cmdk-foot">
          <span><kbd>↑↓</kbd> Navigate</span>
          <span><kbd>↵</kbd> Select</span>
          <span><kbd>esc</kbd> Close</span>
        </div>
      </div>
    </div>,
    document.body
  );
}

/* ─── Cancel run ──────────────────────────────────────── */
function CancelRunButton({ run, onDone, size = 'sm' }: { run: Run; onDone?: () => void; size?: 'sm' | 'md' }) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  if (!LIVE(run.status)) return null;
  const cancel = async () => {
    setBusy(true);
    try {
      await post(`/runs/${run.id}/cancel`, {});
      toast(run.status === 'queued' ? 'Run cancelled' : 'Cancellation requested — stops before the next task', 'info');
      onDone?.();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not cancel run', 'error');
    } finally { setBusy(false); }
  };
  return (
    <Button variant="ghost" size={size} disabled={busy} onClick={cancel} className="cancel-run-btn">
      <Ban size={12} /> Cancel
    </Button>
  );
}

/* ─── Shared run table ────────────────────────────────── */
function RunTable({ runs, flows, onChanged }: { runs: Run[]; flows: Workflow[]; onChanged?: () => void }) {
  const now = useNow(runs.some(r => r.status === 'running'));
  const navTo = useNavigate();
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Workflow</th>
            <th>Status</th>
            <th>Trigger</th>
            <th>Started</th>
            <th>Duration</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {runs.map(r => (
            <tr key={r.id}>
              <td>
                <Link className="run-id" to={`/runs/${r.id}`}
                      style={{ viewTransitionName: `run-${r.id}` } as React.CSSProperties}
                      onClick={e => { e.preventDefault(); navigateWithTransition(navTo, `/runs/${r.id}`); }}>
                  #{r.id}
                </Link>
              </td>
              <td><Link to={`/workflows/${r.workflow_id}`} style={{ color: 'inherit', textDecoration: 'none' }}><b>{flows.find(w => w.id === r.workflow_id)?.name ?? `Workflow ${r.workflow_id}`}</b></Link></td>
              <td><StatusBadge value={r.status} /></td>
              <td><span className="trigger-badge">{r.trigger_type}</span></td>
              <td style={{ color: 'var(--text-2)', fontSize: 13 }} title={formatDate(r.started_at || r.created_at)}>{timeAgo(r.started_at || r.created_at)}</td>
              <td style={{ color: 'var(--text-2)', fontSize: 13, fontVariantNumeric: 'tabular-nums' }}>{liveDuration(r, now)}</td>
              <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                {LIVE(r.status) && <CancelRunButton run={r} onDone={onChanged} />}
                <Link className="row-arrow" to={`/runs/${r.id}`}>›</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FailedRunRow({ run, flows }: { run: Run; flows: Workflow[] }) {
  const flow = flows.find(w => w.id === run.workflow_id);
  return (
    <Link to={`/runs/${run.id}`} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 20px', borderBottom: '1px solid var(--border)', transition: 'background .12s', textDecoration: 'none', color: 'inherit' }}
      onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-hover)')}
      onMouseLeave={e => (e.currentTarget.style.background = '')}
    >
      <StatusBadge value="failed" />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {flow?.name ?? `Workflow ${run.workflow_id}`}
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{timeAgo(run.created_at)} · run #{run.id}</div>
      </div>
      <ChevronRight size={14} color="var(--text-3)" />
    </Link>
  );
}

function LiveRunRow({ run, flows }: { run: Run; flows: Workflow[] }) {
  const flow = flows.find(w => w.id === run.workflow_id);
  return (
    <Link to={`/runs/${run.id}`} className="active-run-row" style={{ textDecoration: 'none', color: 'inherit' }}>
      <span className={`run-pulse ${run.status}`} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {flow?.name ?? `Workflow ${run.workflow_id}`}
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 1 }}>#{run.id} · {run.trigger_type} · {timeAgo(run.created_at)}</div>
      </div>
      <StatusBadge value={run.status} />
    </Link>
  );
}

function UpcomingList({ flows }: { flows: Workflow[] }) {
  const upcoming = useMemo(() => {
    const now = new Date();
    const items: { flow: Workflow; next: Date; label: string }[] = [];
    for (const flow of flows) {
      if (!flow.enabled || !flow.schedule_cron) continue;
      let cursor = now;
      for (let i = 0; i < 3; i++) {
        const next = nextCronOccurrence(flow.schedule_cron, cursor);
        if (!next || next.getTime() - now.getTime() > 7 * DAY) break;
        items.push({ flow, next, label: cronLabel(flow.schedule_cron) });
        // +1s, not +1min: a full minute would skip the very next occurrence
        // of an every-minute schedule.
        cursor = new Date(next.getTime() + 1000);
      }
    }
    return items.sort((a, b) => a.next.getTime() - b.next.getTime()).slice(0, 8);
  }, [flows]);

  if (!upcoming.length) {
    return <EmptyState icon={<Clock size={22} />} title="No upcoming schedules" text="Enabled workflows with cron schedules will appear here." />;
  }
  return (
    <div style={{ padding: '4px 0 8px' }}>
      {upcoming.map(({ flow, next, label }) => (
        <Link to={`/workflows/${flow.id}`} key={`${flow.id}-${next.toISOString()}`} className="tl-upcoming-row" style={{ textDecoration: 'none', color: 'inherit', display: 'flex', alignItems: 'center', gap: 10 }}>
          <div className="tl-upcoming-dot" />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-1)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{flow.name}</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 1 }}>{label}</div>
          </div>
          <div style={{ textAlign: 'right', flexShrink: 0 }}>
            <div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)', fontFamily: 'SF Mono, monospace' }}>{next.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}</div>
            <div style={{ fontSize: 11, color: 'var(--text-3)' }}>{next.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</div>
          </div>
        </Link>
      ))}
    </div>
  );
}

function WeeklyChart({ runs }: { runs: Run[] }) {
  const days = Array.from({ length: 7 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - 6 + i);
    d.setHours(0, 0, 0, 0);
    const label = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()];
    const start = d.getTime();
    const dr = runs.filter(r => {
      const t = new Date(r.created_at).getTime();
      return t >= start && t < start + DAY;
    });
    return {
      label, key: start,
      success: dr.filter(r => r.status === 'success').length,
      failed: dr.filter(r => r.status === 'failed').length,
      other: dr.filter(r => r.status !== 'success' && r.status !== 'failed').length,
      total: dr.length,
    };
  });
  const max = Math.max(...days.map(d => d.total), 1);
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-head"><div><h2>7-day activity</h2><p>Runs per day by outcome</p></div></div>
      <div className="weekly-chart-wrap">
        <div className="weekly-chart">
          {days.map(d => (
            <div key={d.key} className="chart-col">
              <div className="chart-bars">
                <div className="chart-bar failed" style={{ height: `${(d.failed / max) * 100}%` }} title={`${d.failed} failed`} />
                <div className="chart-bar other" style={{ height: `${(d.other / max) * 100}%` }} title={`${d.other} other`} />
                <div className="chart-bar success" style={{ height: `${(d.success / max) * 100}%` }} title={`${d.success} succeeded`} />
              </div>
              <span className="chart-label">{d.label}</span>
              {d.total > 0 && <span className="chart-count">{d.total}</span>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─── Dashboard ───────────────────────────────────────── */
function Dashboard() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [flows, setFlows] = useState<Workflow[] | null>(null);
  const navTo = useNavigate();
  const { toast } = useToast();

  const load = () => {
    api<Run[]>('/runs?limit=300').then(setRuns).catch(() => setRuns([]));
    api<Workflow[]>('/workflows').then(setFlows).catch(() => setFlows([]));
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const refresh = () => void api<Run[]>('/runs?limit=300').then(setRuns).catch(() => {});
    const u1 = rrws.on('run_created', refresh);
    const u2 = rrws.on('run_updated', refresh);
    const u3 = rrws.on('task_run_updated', refresh);
    return () => { u1(); u2(); u3(); };
  }, []);
  useLiveRefresh(Boolean(runs?.some(r => LIVE(r.status))), () => void api<Run[]>('/runs?limit=300').then(setRuns).catch(() => {}));

  if (runs === null || flows === null) {
    return <div><div style={{ height: 28, width: 160, marginBottom: 24 }} className="skeleton-line" /><SkeletonCard /><SkeletonCard /></div>;
  }

  const liveRuns = runs.filter(r => LIVE(r.status)).slice(0, 6);
  const recent24h = runs.filter(r => Date.now() - new Date(r.created_at).getTime() < DAY);
  const failed24h = recent24h.filter(r => r.status === 'failed').length;
  const done7d = runs.filter(r => (r.status === 'success' || r.status === 'failed')
    && Date.now() - new Date(r.created_at).getTime() < 7 * DAY);
  const successRate = done7d.length ? Math.round(done7d.filter(r => r.status === 'success').length / done7d.length * 100) : null;
  const withDuration = recent24h.filter(r => r.duration_seconds != null);
  const avgDuration = withDuration.length ? withDuration.reduce((sum, r) => sum + (r.duration_seconds ?? 0), 0) / withDuration.length : null;
  const failures = runs.filter(r => r.status === 'failed').slice(0, 4);
  const empty = flows.length === 0;

  const runNow = async (id: number) => {
    try {
      const run = await post<Run>(`/workflows/${id}/run`, { parameters: {} });
      navTo(`/runs/${run.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
    }
  };

  return (
    <>
      <div className="dashboard-hero">
        <div className="dashboard-hero-content">
          <div className="dashboard-eyebrow">CONTROL PLANE</div>
          <h1 className="dashboard-title">{empty ? 'Welcome to RunRail' : 'Everything on track.'}</h1>
          <p className="dashboard-sub">
            {empty
              ? 'Schedules, retries, logs, and history for the scripts you already have.'
              : `${flows.filter(w => w.enabled).length} active workflow${flows.filter(w => w.enabled).length === 1 ? '' : 's'} · ${recent24h.length} run${recent24h.length === 1 ? '' : 's'} in the last 24 hours`}
          </p>
          <div className="dashboard-cta-row">
            <Button onClick={() => navTo('/workflows')}><GitBranch size={14} /> {empty ? 'Create a workflow' : 'View workflows'}</Button>
            {!empty && <Link className="text-link" to="/runs">Open run history →</Link>}
          </div>
          {!empty && (
            <div className="dash-status-bar">
              <span className={`dash-stat${liveRuns.some(r => r.status === 'running') ? ' stat-running' : ''}`}>
                <Activity size={12} /> {liveRuns.filter(r => r.status === 'running').length} running
              </span>
              <span className="dash-sep">·</span>
              <span className={`dash-stat${liveRuns.some(r => r.status === 'queued') ? ' stat-queued' : ''}`}>
                <Clock size={12} /> {liveRuns.filter(r => r.status === 'queued').length} queued
              </span>
              <span className="dash-sep">·</span>
              <span className="dash-stat"><AlertTriangle size={12} /> {failed24h} failed today</span>
            </div>
          )}
        </div>
        {!empty && (
          <div className="dashboard-hero-aside">
            <div className="dashboard-hero-aside-head">
              <span>Activity · 16 weeks</span>
              <Link className="panel-link" to="/wallboard">Wallboard →</Link>
            </div>
            <RunHeatmap weeks={16} />
          </div>
        )}
      </div>

      {empty ? <QuickStart /> : (
        <>
          <div className="metric-grid dashboard-metric-grid">
            <MetricCard icon={<Activity size={18} />} label="Live now" value={liveRuns.length} tone={liveRuns.length ? 'running' : 'default'} note="Running and queued" />
            <MetricCard icon={<Zap size={18} />} label="Runs · 24h" value={recent24h.length} note={`${recent24h.filter(r => r.status === 'success').length} succeeded`} />
            <MetricCard icon={<CheckCircle2 size={18} />} label="Success rate · 7d" value={successRate != null ? `${successRate}%` : '—'} tone={successRate != null && successRate < 80 ? 'warning' : 'success'} note={`${done7d.length} completed runs`} />
            <MetricCard icon={<AlertTriangle size={18} />} label="Failures · 24h" value={failed24h} tone={failed24h ? 'danger' : 'default'} note={failed24h ? 'Needs attention' : 'All clear'} />
            <MetricCard icon={<Clock size={18} />} label="Avg duration · 24h" value={avgDuration != null ? formatDuration(avgDuration) : '—'} note="Completed runs" />
          </div>

          <div className="dashboard-panels">
            <div>
              <WeeklyChart runs={runs} />
              <div className="panel dashboard-lead-panel">
                <div className="panel-head">
                  <div><h2>Recent runs</h2><p>The latest executions across all workflows</p></div>
                  <Link className="panel-link" to="/runs">See all →</Link>
                </div>
                {runs.length > 0
                  ? <RunTable runs={runs.slice(0, 8)} flows={flows} onChanged={load} />
                  : <EmptyState icon={<History size={22} />} title="No runs yet" text="Trigger a workflow to see it here." />}
              </div>
            </div>
            <div>
              <div className="panel" style={{ marginBottom: 16 }}>
                <div className="panel-head"><div><h2>Live now</h2><p>Running and queued</p></div></div>
                {liveRuns.length > 0
                  ? <div style={{ padding: '6px 0' }}>{liveRuns.map(r => <LiveRunRow key={r.id} run={r} flows={flows} />)}</div>
                  : <EmptyState icon={<Activity size={22} />} title="Nothing live" text="Runs appear here the moment they queue or start." />}
              </div>
              <div className="panel" style={{ marginBottom: 16 }}>
                <div className="panel-head">
                  <div><h2>Workflows</h2><p>Run one right now</p></div>
                </div>
                <div style={{ padding: '6px 0 10px' }}>
                  {flows.slice(0, 6).map(w => (
                    <div key={w.id} className="wf-spark-row">
                      <Link className="wf-spark-name" to={`/workflows/${w.id}`}>{w.name}</Link>
                      <WorkflowSparkline runs={runs.filter(r => r.workflow_id === w.id).slice(0, 12)} />
                      <button className="edit-link" onClick={() => runNow(w.id)} title={`Run ${w.name} now`}><Play size={12} /></button>
                    </div>
                  ))}
                </div>
              </div>
              <div className="panel" style={{ marginBottom: 16 }}>
                <div className="panel-head"><div><h2>Upcoming</h2><p>Next scheduled executions</p></div></div>
                <UpcomingList flows={flows} />
              </div>
              {failures.length > 0 && (
                <div className="panel">
                  <div className="panel-head"><div><h2>Needs attention</h2><p>Most recent failures</p></div></div>
                  {failures.map(r => <FailedRunRow key={r.id} run={r} flows={flows} />)}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  );
}

function WorkflowSparkline({ runs }: { runs: Run[] }) {
  const bars = [...runs].reverse();
  return (
    <div className="wf-spark-bars">
      {bars.length === 0 && <div className="wf-bar empty" style={{ height: 3 }} />}
      {bars.map(r => {
        const cls = r.status === 'success' ? 'success' : r.status === 'failed' ? 'failed' : 'running';
        const h = r.duration_seconds != null ? Math.max(20, Math.min(100, r.duration_seconds * 8)) : 40;
        return <Link key={r.id} to={`/runs/${r.id}`} className={`wf-bar ${cls}`} style={{ height: `${h}%` }} title={`#${r.id} · ${r.status}`} />;
      })}
    </div>
  );
}

function QuickStart() {
  return (
    <div className="welcome-card">
      <div className="welcome-copy">
        <span className="pill">LET'S GET STARTED</span>
        <h2>Bring your first workflow to life.</h2>
        <p>Connect a folder containing your scripts, create a workflow, and RunRail takes care of schedules, logs, retries, and history.</p>
        <div className="btn-row">
          <Link className="btn btn-primary btn-md" to="/projects">
            <FolderOpen size={15} /> Connect a project
          </Link>
          <Link className="text-link" to="/workflows">Skip to workflows →</Link>
        </div>
      </div>
      <div className="steps">
        {[
          ['1', 'Connect your code', 'Choose a local project folder'],
          ['2', 'Build a workflow', 'Add scripts and set up dependencies'],
          ['3', 'Run and observe', 'See logs, outputs, and history'],
        ].map(([n, title, text]) => (
          <div key={n} className="step">
            <span>{n}</span>
            <div><b>{title}</b><small>{text}</small></div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ─── Runs page ───────────────────────────────────────── */
function Runs() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [flows, setFlows] = useState<Workflow[]>([]);
  const [status, setStatus] = useState('');
  const [workflowId, setWorkflowId] = useState('');
  const [trigger, setTrigger] = useState('');
  const [query, setQuery] = useState('');

  const load = () => {
    api<Run[]>('/runs?limit=500').then(setRuns).catch(() => setRuns([]));
    api<Workflow[]>('/workflows').then(setFlows).catch(() => {});
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const refresh = () => void api<Run[]>('/runs?limit=500').then(setRuns).catch(() => {});
    const u1 = rrws.on('run_created', refresh);
    const u2 = rrws.on('run_updated', refresh);
    const u3 = rrws.on('task_run_updated', refresh);
    return () => { u1(); u2(); u3(); };
  }, []);
  useLiveRefresh(Boolean(runs?.some(r => LIVE(r.status))), () => void api<Run[]>('/runs?limit=500').then(setRuns).catch(() => {}));

  const flowName = (id: number) => flows.find(w => w.id === id)?.name ?? `Workflow ${id}`;
  const shown = useMemo(() => {
    if (!runs) return [];
    const q = query.trim().toLowerCase();
    return runs.filter(r =>
      (!status || r.status === status)
      && (!workflowId || String(r.workflow_id) === workflowId)
      && (!trigger || r.trigger_type === trigger)
      && (!q || `#${r.id}`.includes(q) || String(r.id) === q || flowName(r.workflow_id).toLowerCase().includes(q))
    );
  }, [runs, status, workflowId, trigger, query, flows]);

  if (runs === null) {
    return <div><div style={{ height: 28, width: 120, marginBottom: 24 }} className="skeleton-line" /><SkeletonCard /><SkeletonCard /></div>;
  }

  const liveRuns = runs.filter(r => LIVE(r.status)).slice(0, 6);
  const recent24h = runs.filter(r => Date.now() - new Date(r.created_at).getTime() < DAY);
  const failed24h = recent24h.filter(r => r.status === 'failed').length;
  const withDuration = recent24h.filter(r => r.duration_seconds != null);
  const avgDuration = withDuration.length ? withDuration.reduce((sum, r) => sum + (r.duration_seconds ?? 0), 0) / withDuration.length : null;
  const filtersActive = Boolean(status || workflowId || trigger || query);

  return (
    <>
      <PageHeader eyebrow="OBSERVABILITY" title="Runs" subtitle="Live activity, schedule context, and the full execution history." />

      <div className="summary-strip">
        <div><span>Live</span><strong>{liveRuns.length}</strong></div>
        <div><span>Runs · 24h</span><strong>{recent24h.length}</strong></div>
        <div><span>Failures · 24h</span><strong>{failed24h}</strong></div>
        <div><span>Avg duration · 24h</span><strong>{avgDuration != null ? formatDuration(avgDuration) : '—'}</strong></div>
        <div><span>History loaded</span><strong>{runs.length}</strong></div>
      </div>

      <div className="dashboard-panels dashboard-panels-v2">
        <div>
          <div className="panel" style={{ marginBottom: 16 }}>
            <div className="panel-head">
              <div><h2>Run history</h2><p>Filter and inspect every execution in one place</p></div>
              {filtersActive && (
                <button className="edit-link" onClick={() => { setStatus(''); setWorkflowId(''); setTrigger(''); setQuery(''); }}>
                  Clear filters
                </button>
              )}
            </div>
            <div className="filterbar" style={{ marginBottom: 0, border: 'none', padding: '0 20px 18px' }}>
              <div className="filterbar-search">
                <Search size={14} color="var(--text-3)" />
                <input placeholder="Search by workflow or run id…" value={query} onChange={e => setQuery(e.target.value)} />
              </div>
              <select value={workflowId} onChange={e => setWorkflowId(e.target.value)} aria-label="Workflow filter">
                <option value="">All workflows</option>
                {flows.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
              <select value={status} onChange={e => setStatus(e.target.value)} aria-label="Status filter">
                <option value="">All statuses</option>
                {['queued', 'running', 'success', 'failed', 'cancelled'].map(s => <option key={s}>{s}</option>)}
              </select>
              <select value={trigger} onChange={e => setTrigger(e.target.value)} aria-label="Trigger filter">
                <option value="">All triggers</option>
                {['manual', 'schedule', 'cli', 'backfill'].map(t => <option key={t}>{t}</option>)}
              </select>
              <span className="filterbar-count">{shown.length} of {runs.length}</span>
            </div>
            {shown.length > 0
              ? <RunTable runs={shown.slice(0, 100)} flows={flows} onChanged={load} />
              : <EmptyState icon={<History size={22} />} title={filtersActive ? 'No runs match these filters' : 'No runs yet'} text={filtersActive ? 'Try widening your filters.' : 'Run a workflow to see its history here.'} />}
            {shown.length > 100 && (
              <div style={{ padding: '10px 20px', fontSize: 12, color: 'var(--text-3)', borderTop: '1px solid var(--border)' }}>
                Showing the first 100 matches. Narrow the filters to see older runs.
              </div>
            )}
          </div>
          {runs.length > 0 && <WeeklyChart runs={runs} />}
        </div>

        <div>
          <div className="panel" style={{ marginBottom: 16 }}>
            <div className="panel-head"><div><h2>Live now</h2><p>Running and queued runs</p></div></div>
            {liveRuns.length > 0
              ? <div style={{ padding: '6px 0' }}>{liveRuns.map(r => <LiveRunRow key={r.id} run={r} flows={flows} />)}</div>
              : <EmptyState icon={<Activity size={22} />} title="Nothing live right now" text="Runs surface here as soon as they queue or start." />}
          </div>
          <div className="panel" style={{ marginBottom: 16 }}>
            <div className="panel-head"><div><h2>Upcoming</h2><p>Next scheduled executions</p></div></div>
            <UpcomingList flows={flows} />
          </div>
        </div>
      </div>
    </>
  );
}

/* ─── Run detail ──────────────────────────────────────── */
function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run>();
  const [flow, setFlow] = useState<Workflow>();
  const [flowTasks, setFlowTasks] = useState<Task[]>([]);
  const navTo = useNavigate();
  const { toast } = useToast();

  const retry = async () => {
    try {
      const fresh = await post<Run>(`/runs/${id}/retry`, {});
      toast('Run queued with the same parameters');
      navTo(`/runs/${fresh.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not retry run', 'error');
    }
  };

  const refresh = () => { void api<Run>(`/runs/${id}`).then(setRun).catch(() => {}); };
  useEffect(() => { setRun(undefined); refresh(); }, [id]);
  useEffect(() => {
    if (run && !flow) {
      api<Workflow>(`/workflows/${run.workflow_id}`).then(setFlow).catch(() => {});
      api<Task[]>(`/workflows/${run.workflow_id}/tasks`).then(setFlowTasks).catch(() => {});
    }
  }, [run?.workflow_id]);
  useEffect(() => {
    const u1 = rrws.on('run_updated', e => { if (String(e.id) === id) refresh(); });
    const u2 = rrws.on('task_run_updated', e => { if (String(e.run_id) === id) refresh(); });
    return () => { u1(); u2(); };
  }, [id]);
  useLiveRefresh(Boolean(run && LIVE(run.status)), refresh, 2000);
  const now = useNow(Boolean(run && run.status === 'running'));

  if (!run) return (
    <div>
      <div style={{ height: 28, width: 120, marginBottom: 24 }} className="skeleton-line" />
      <SkeletonCard /><SkeletonCard />
    </div>
  );

  const params = Object.entries(run.parameters_json ?? {});
  return (
    <>
      <div className="detail-head">
        <Link to="/runs"><ArrowLeft size={14} /> Runs</Link>
        <div className="detail-head-row">
          <div className="workflow-glyph large"><Play size={22} /></div>
          <div className="detail-head-text">
            <span className="eyebrow">WORKFLOW RUN</span>
            <h1 style={{ viewTransitionName: `run-${run.id}` } as React.CSSProperties}>
              {flow ? `${flow.name} · #${run.id}` : `Run #${run.id}`}
            </h1>
            <p>Created {formatDate(run.created_at)} · {run.trigger_type} trigger</p>
          </div>
          <div className="detail-actions">
            <CancelRunButton run={run} onDone={refresh} size="md" />
            {!LIVE(run.status) && (
              <Button variant={run.status === 'failed' ? 'primary' : 'ghost'} onClick={retry}
                      title="Queue a new run with this run's parameters">
                <RefreshCw size={13} /> {run.status === 'failed' ? 'Retry' : 'Run again'}
              </Button>
            )}
            {flow && <Link className="btn btn-ghost btn-md" to={`/workflows/${flow.id}`}><GitBranch size={13} /> Workflow</Link>}
            <StatusBadge value={run.status} />
          </div>
        </div>
      </div>

      <div className="summary-strip">
        <div><span>Status</span><StatusBadge value={run.status} /></div>
        <div><span>Trigger</span><strong>{run.trigger_type}</strong></div>
        <div><span>Duration</span><strong>{liveDuration(run, now)}</strong></div>
        <div><span>Tasks</span><strong>{run.task_runs?.length || 0}</strong></div>
        {run.started_at && <div><span>Started</span><strong>{formatDate(run.started_at)}</strong></div>}
        {run.finished_at && <div><span>Finished</span><strong>{formatDate(run.finished_at)}</strong></div>}
      </div>

      {params.length > 0 && (
        <div className="panel" style={{ marginBottom: 20 }}>
          <div className="panel-head"><div><h2>Parameters</h2><p>Values injected into task templates for this run</p></div></div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, padding: '0 20px 16px' }}>
            {params.map(([key, value]) => (
              <span key={key} className="path-chip" style={{ margin: 0 }}>{`${key} = ${String(value)}`}</span>
            ))}
          </div>
        </div>
      )}

      {flowTasks.length > 1 && (
        <div className="panel" style={{ marginBottom: 20 }}>
          <div className="panel-head">
            <div><h2>Graph</h2><p>Live task statuses across the dependency graph</p></div>
          </div>
          <DagGraph
            tasks={flowTasks.map(t => ({
              name: t.name, task_type: t.task_type, depends_on: t.depends_on_json || [],
            }))}
            statuses={Object.fromEntries(
              [...(run.task_runs ?? [])]
                .sort((a, b) => a.attempt - b.attempt)
                .map(t => [t.task_name ?? '', t.status])
            )}
            onSelect={name => document.getElementById(`task-${name}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })}
          />
        </div>
      )}

      {run.task_runs && run.task_runs.length > 1 && (
        <TaskTimeline taskRuns={run.task_runs} runStart={run.started_at || run.created_at} now={now} />
      )}

      <div className="panel">
        <div className="panel-head">
          <div><h2>Task runs</h2><p>Commands, attempts, and captured output</p></div>
        </div>
        <div className="run-tasks">
          {run.task_runs && run.task_runs.length > 0
            ? run.task_runs.map((t, i) => <TaskRunCard key={t.id} task={t} index={i + 1} />)
            : run.status === 'queued'
            ? <div className="empty-state">
                <div style={{ width: 200, marginBottom: 4 }}><LoadingBar /></div>
                <h3 className="empty-title">Waiting for the worker</h3>
                <p className="empty-text">Task runs appear here the moment a worker claims this run.</p>
              </div>
            : <EmptyState icon={<Clock size={22} />} title="No task output" text="This run produced no task executions." />}
        </div>
      </div>
    </>
  );
}

function TaskTimeline({ taskRuns, runStart, now }: { taskRuns: TaskRun[]; runStart: string; now: number }) {
  const origin = new Date(runStart).getTime();
  const withTimes = taskRuns.filter(t => t.started_at);
  if (!withTimes.length) return null;
  const startMs = (t: TaskRun) => (t.started_at ? new Date(t.started_at).getTime() : origin);
  const isLive = (t: TaskRun) => Boolean(t.started_at) && !t.finished_at && t.status === 'running';
  // A running task has no duration yet: measure it against the wall clock so its
  // bar grows every tick, and extend the axis to "now" so the whole chart moves.
  const spanMs = (t: TaskRun) =>
    t.duration_seconds != null ? t.duration_seconds * 1000
    : t.finished_at ? new Date(t.finished_at).getTime() - startMs(t)
    : isLive(t) ? Math.max(0, now - startMs(t))
    : 0;
  const totalMs = Math.max(
    ...taskRuns.map(t => t.finished_at
      ? new Date(t.finished_at).getTime() - origin
      : isLive(t) ? now - origin
      : t.started_at ? new Date(t.started_at).getTime() - origin + 1000 : 0
    ), 1000
  );
  // One lane per task; retry attempts render as separate bars on the same lane.
  const lanes = new Map<string, TaskRun[]>();
  for (const t of taskRuns) {
    const key = t.task_name ?? `#${t.task_id}`;
    lanes.set(key, [...(lanes.get(key) ?? []), t]);
  }
  const ticks = [0.25, 0.5, 0.75];
  return (
    <div className="panel" style={{ marginBottom: 20 }}>
      <div className="panel-head">
        <div><h2>Timeline</h2><p>Overlapping bars ran in parallel; multiple bars on a lane are retry attempts</p></div>
      </div>
      <div className="task-timeline">
        {[...lanes.entries()].map(([name, attempts]) => (
          <div key={name} className="tl-row">
            <span className="tl-name" title={name}>{name}</span>
            <div className="tl-track">
              {ticks.map(f => <span key={f} className="tl-tick" style={{ left: `${f * 100}%` }} />)}
              {attempts.map(t => {
                const start = t.started_at ? (startMs(t) - origin) / totalMs * 100 : 0;
                const width = t.started_at ? Math.max(spanMs(t) / totalMs * 100, 1.2) : 2;
                const left = Math.min(Math.max(start, 0), 98);
                const tip = [
                  `${name} · attempt ${t.attempt} · ${t.status}`,
                  t.started_at ? `started +${formatDuration((startMs(t) - origin) / 1000)}` : null,
                  t.duration_seconds != null ? `took ${formatDuration(t.duration_seconds)}`
                    : isLive(t) ? `running ${formatDuration(spanMs(t) / 1000)}` : null,
                  t.exit_code != null ? `exit ${t.exit_code}` : null,
                ].filter(Boolean).join('\n');
                return (
                  <div key={t.id} className={`tl-bar ${t.status}`}
                       style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }} title={tip}>
                    {t.attempt > 1 && <span className="tl-attempt">A{t.attempt}</span>}
                  </div>
                );
              })}
            </div>
            <span className="tl-dur">
              {formatDuration(attempts.reduce((sum, t) => sum + spanMs(t) / 1000, 0) || null)}
            </span>
          </div>
        ))}
        <div className="tl-axis">
          <span className="tl-name" />
          <div className="tl-axis-track">
            <span>0s</span>
            {ticks.map(f => (
              <span key={f} style={{ position: 'absolute', left: `${f * 100}%`, transform: 'translateX(-50%)' }}>
                {formatDuration(totalMs * f / 1000)}
              </span>
            ))}
            <span style={{ marginLeft: 'auto' }}>{formatDuration(totalMs / 1000)}</span>
          </div>
          <span className="tl-dur" />
        </div>
      </div>
    </div>
  );
}

function TaskRunCard({ task, index }: { task: TaskRun; index: number }) {
  const [open, setOpen] = useState(index === 1 || task.status === 'failed' || task.status === 'running');
  const collapsible = task.status !== 'skipped' && task.status !== 'cancelled';
  return (
    <article className="run-task" id={task.task_name ? `task-${task.task_name}` : undefined}>
      <button className="run-task-head" onClick={() => collapsible && setOpen(!open)}>
        <span className="task-order">{index}</span>
        <div>
          <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {task.task_type && <TaskTypeBadge type={task.task_type} />}
            {task.task_name ?? `Task #${task.task_id}`}
          </h3>
          <p>
            Attempt {task.attempt}
            {task.exit_code != null ? ` · exit ${task.exit_code}` : ''}
            {task.duration_seconds != null ? ` · ${formatDuration(task.duration_seconds)}` : ''}
            {task.rendered_command ? ` · ${task.rendered_command}` : ''}
          </p>
        </div>
        <StatusBadge value={task.status} />
        {collapsible && <b>{open ? '⌃' : '⌄'}</b>}
      </button>
      {open && collapsible && (
        <div className="log-area">
          <LogViewer taskRunId={task.id} taskStatus={task.status}
            initialTab={task.status === 'failed' ? 'stderr' : 'stdout'}
            errorMessage={task.error_message ?? undefined} />
        </div>
      )}
      {!collapsible && task.error_message && (
        <div style={{ padding: '0 20px 14px', fontSize: 12.5, color: 'var(--text-3)' }}>{task.error_message}</div>
      )}
    </article>
  );
}

/* ─── Projects ────────────────────────────────────────── */
function Projects() {
  const [items, setItems] = useState<Project[]>([]);
  const [flows, setFlows] = useState<Workflow[] | null>(null);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const { toast } = useToast();
  const load = () => {
    api<Project[]>('/projects').then(setItems).catch(() => {});
    api<Workflow[]>('/workflows').then(setFlows).catch(() => {});
  };
  useEffect(() => { void load(); }, []);

  const removeProject = async (project: Project) => {
    if (!confirm(`Remove project "${project.name}"? Workflows and tasks that reference it will keep running from their stored paths.`)) return;
    try {
      await del(`/projects/${project.id}`);
      toast('Project removed', 'info');
      load();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not remove project', 'error');
    }
  };

  return (
    <>
      <PageHeader
        eyebrow="CODE"
        title="Projects"
        subtitle="Connect the folders where your scripts, notebooks, and SQL live."
        action={<Button onClick={() => setOpen(true)}><Plus size={15} /> New project</Button>}
      />
      {items.length > 0 && flows?.length === 0 && (
        <div className="callout" style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ flex: 1, minWidth: 220 }}>
            <strong style={{ color: 'var(--text-1)' }}>Project connected.</strong>{' '}
            Next, build a workflow that runs scripts from it on a schedule.
          </span>
          <Link className="btn btn-primary btn-sm" to="/workflows">Create a workflow →</Link>
        </div>
      )}
      {items.length > 0 ? (
        <div className="card-grid">
          {items.map(x => (
            <article className="resource-card" key={x.id}>
              <div className="resource-icon folder"><FolderOpen size={17} /></div>
              <div className="resource-main">
                <div className="resource-title">
                  <h3>{x.name}</h3>
                  <div className="row-actions">
                    <button className="edit-link" onClick={() => setEditing(x)}>Edit</button>
                    <button className="delete-link" onClick={() => removeProject(x)}>Remove</button>
                  </div>
                </div>
                <p>{x.description || 'No description yet'}</p>
                <div className="path-chip"><span>⌁</span>{x.root_path}</div>
                <div className="resource-foot"><span>Ready to use</span></div>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<FolderOpen size={24} />}
          title="No projects connected"
          text="Choose a folder on this machine to make its scripts available to workflows."
          action={<Button onClick={() => setOpen(true)}>Connect your first project</Button>}
        />
      )}
      {open && <ProjectModal onClose={() => setOpen(false)} done={() => { setOpen(false); load(); }} />}
      {editing && <EditProjectModal project={editing} onClose={() => setEditing(null)} done={() => { setEditing(null); load(); }} />}
    </>
  );
}

function ProjectModal({ onClose, done }: { onClose: () => void; done: () => void }) {
  const { toast } = useToast();
  const [path, setPath] = useState('');
  const [envs, setEnvs] = useState<Env[]>([]);
  useEffect(() => { api<Env[]>('/environments').then(setEnvs).catch(() => {}); }, []);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    try {
      await post('/projects', { name: f.get('name'), root_path: path, description: f.get('description') || null, default_environment_id: f.get('default_environment_id') ? Number(f.get('default_environment_id')) : null });
      toast('Project connected successfully');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not create project', 'error');
    }
  }
  return (
    <Modal title="Connect a project" subtitle="Point RunRail at an existing local folder." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field"><span>Project name</span><input name="name" placeholder="e.g. Analytics jobs" required autoFocus /></label>
        <FilePicker value={path} onChange={setPath} mode="directories" label="Project folder" placeholder="Choose or enter a folder path" />
        <label className="field"><span>Default environment <em>Recommended for Python projects</em></span><select name="default_environment_id"><option value="">None</option>{envs.map(e => <option key={e.id} value={e.id} disabled={!environmentUsable(e)}>{e.name}{e.status !== 'ready' ? ` (${e.status})` : ''}</option>)}</select></label>
        <label className="field"><span>Description <em>Optional</em></span><textarea name="description" placeholder="What lives in this project?" /></label>
        <div className="callout">RunRail never imports this code. Every task runs safely in a subprocess.</div>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!path}>Connect project</Button>
        </div>
      </form>
    </Modal>
  );
}

function EditProjectModal({ project, onClose, done }: { project: Project; onClose: () => void; done: () => void }) {
  const { toast } = useToast();
  const [path, setPath] = useState(project.root_path);
  const [envs, setEnvs] = useState<Env[]>([]);
  const [environmentId, setEnvironmentId] = useState(String(project.default_environment_id ?? ''));
  useEffect(() => { api<Env[]>('/environments').then(setEnvs).catch(() => {}); }, []);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    try {
      await put(`/projects/${project.id}`, { name: f.get('name'), root_path: path, description: f.get('description') || null, default_environment_id: environmentId ? Number(environmentId) : null });
      toast('Project updated');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not update project', 'error');
    }
  }
  return (
    <Modal title="Edit project" subtitle="Update this project's details." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field"><span>Project name</span><input name="name" defaultValue={project.name} required autoFocus /></label>
        <FilePicker value={path} onChange={setPath} mode="directories" label="Project folder" placeholder="Choose or enter a folder path" />
        <label className="field"><span>Default environment</span><select name="default_environment_id" value={environmentId} onChange={e => setEnvironmentId(e.target.value)}><option value="">None</option>{envs.map(e => <option key={e.id} value={e.id} disabled={!environmentUsable(e)}>{e.name}{e.status !== 'ready' ? ` (${e.status})` : ''}</option>)}</select></label>
        <label className="field"><span>Description <em>Optional</em></span><textarea name="description" defaultValue={project.description || ''} placeholder="What lives in this project?" /></label>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!path}>Save changes</Button>
        </div>
      </form>
    </Modal>
  );
}

/* ─── Environments ────────────────────────────────────── */
function Environments() {
  const [items, setItems] = useState<Env[]>([]);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Env | null>(null);
  const { toast } = useToast();
  const load = () => api<Env[]>('/environments').then(setItems).catch(() => {});
  useEffect(() => { void load(); }, []);
  // Instant updates via WebSocket when an environment build finishes.
  useEffect(() => rrws.on('environment_updated', () => void load()), []);
  // Fallback polling while environments are building (covers the WS-not-yet-connected window).
  useEffect(() => {
    if (!items.some(x => x.status === 'creating' || x.status === 'building')) return;
    const timer = window.setInterval(() => { void load(); }, 1500);
    return () => window.clearInterval(timer);
  }, [items]);

  const removeEnv = async (env: Env) => {
    if (!confirm(`Remove environment "${env.name}"?${env.managed ? ' Its virtual environment will be deleted from disk.' : ''}`)) return;
    try {
      await del(`/environments/${env.id}`);
      toast('Environment removed', 'info');
      load();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not remove environment', 'error');
    }
  };

  return (
    <>
      <PageHeader
        eyebrow="RUNTIMES"
        title="Environments"
        subtitle="Define how RunRail should execute your Python and data jobs."
        action={<Button onClick={() => setOpen(true)}><Plus size={15} /> New environment</Button>}
      />
      {items.length > 0 ? (
        <div className="list-panel">
          {items.map(x => (
            <div className="list-item" key={x.id}>
              <div className="resource-icon terminal"><Terminal size={16} /></div>
              <div className="list-copy">
                <h3>{x.name}</h3>
                {x.status === 'creating' || x.status === 'building'
                  ? <div style={{ marginTop: 6, maxWidth: 260 }}>
                      <LoadingBar size="sm" />
                      <small style={{ display: 'block', marginTop: 5, color: 'var(--text-3)' }}>
                        {x.status === 'building' ? 'Installing packages…' : 'Preparing environment…'}
                      </small>
                    </div>
                  : <p>{x.description || (x.managed ? `${x.active_packages_json.length} active managed libraries` : x.executable) || 'External runtime'}</p>}
                {x.last_error && <small style={{ color: 'var(--danger)' }}>{x.last_error}</small>}
              </div>
              <span className="type-chip">{x.managed ? 'managed' : x.env_type} · {x.status}{x.python_version ? ` · py ${x.python_version}` : ''}</span>
              <div className="row-actions">
                <button className="edit-link" disabled={x.status === 'creating' || x.status === 'building'} onClick={() => setEditing(x)}>Edit</button>
                <button className="delete-link" disabled={x.status === 'creating' || x.status === 'building'} onClick={() => removeEnv(x)}>Remove</button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<Cpu size={24} />}
          title="Add your first runtime"
          text="Environments let each task select a Python executable, Conda environment, and environment variables."
          action={<Button onClick={() => setOpen(true)}>Add environment</Button>}
        />
      )}
      {open && <EnvironmentModal onClose={() => setOpen(false)} done={() => { setOpen(false); load(); }} />}
      {editing && <EditEnvironmentModal env={editing} onClose={() => setEditing(null)} done={() => { setEditing(null); load(); }} />}
    </>
  );
}

function EnvironmentModal({ onClose, done }: { onClose: () => void; done: () => void }) {
  const { toast } = useToast();
  const [executable, setExecutable] = useState('');
  const [kind, setKind] = useState('managed');
  const [creating, setCreating] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    let vars = null;
    try { vars = f.get('vars') ? JSON.parse(String(f.get('vars'))) : null; } catch { toast('Environment variables must be valid JSON', 'error'); return; }
    const packages = String(f.get('packages') || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    setCreating(true);
    try {
      const created = await post<Env>('/environments', {
        name: f.get('name'), env_type: kind === 'managed' ? 'python' : kind,
        create_venv: kind === 'managed', packages: kind === 'managed' ? packages : [],
        executable: kind === 'managed' ? null : executable || null,
        base_executable: kind === 'managed' ? executable || null : null,
        conda_env: kind === 'conda' ? f.get('conda_env') || null : null,
        env_vars_json: vars, description: f.get('description') || null,
      });
      toast(created.managed ? 'Environment build queued' : 'Environment is ready', created.status === 'failed' ? 'error' : 'success');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not create environment', 'error');
    } finally { setCreating(false); }
  }
  return (
    <Modal title="New environment" subtitle="Configure a reusable task runtime." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <div className="field-row">
          <label className="field"><span>Name</span><input name="name" placeholder="Python 3.12" required /></label>
          <label className="field"><span>Type</span>
            <select name="type" value={kind} onChange={e => setKind(e.target.value)}>
              <option value="managed">Managed Python (recommended)</option>
              <option value="python">Existing Python / virtualenv</option>
              <option value="conda">Conda</option>
            </select>
          </label>
        </div>
        {kind === 'managed' ? (
          <>
            <FilePicker value={executable} onChange={setExecutable} label="Base Python executable" placeholder="Optional — uses RunRail's base Python" />
            <label className="field"><span>Libraries <em>One pip requirement per line</em></span><textarea name="packages" defaultValue={'pandas\nsqlalchemy'} placeholder={'pandas==2.3.0\nsqlalchemy>=2,<3'} /><small>Declarations are saved and reapplied whenever this environment is rebuilt.</small></label>
          </>
        ) : (
          <>
            {kind === 'conda' && <label className="field"><span>Conda environment name</span><input name="conda_env" placeholder="analytics" required /></label>}
            <FilePicker value={executable} onChange={setExecutable} label={kind === 'conda' ? 'Conda executable' : 'Python executable'} placeholder={kind === 'conda' ? 'Optional if conda is on PATH' : 'Required, e.g. /project/.venv/bin/python'} />
          </>
        )}
        <label className="field"><span>Environment variables <em>JSON, optional</em></span><textarea name="vars" placeholder={'{"API_MODE": "production"}'} /></label>
        <label className="field"><span>Description <em>Optional</em></span><input name="description" placeholder="What is this environment for?" /></label>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={creating}>{creating ? 'Creating environment…' : 'Create environment'}</Button>
        </div>
      </form>
    </Modal>
  );
}

function EditEnvironmentModal({ env, onClose, done }: { env: Env; onClose: () => void; done: () => void }) {
  const { toast } = useToast();
  const [executable, setExecutable] = useState(env.managed ? env.base_executable || '' : env.executable || '');
  const [kind, setKind] = useState(env.env_type);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    let vars = null;
    try { vars = f.get('vars') ? JSON.parse(String(f.get('vars'))) : null; } catch { toast('Environment variables must be valid JSON', 'error'); return; }
    const type = env.managed ? env.env_type : String(f.get('type'));
    try {
      await put(`/environments/${env.id}`, { name: f.get('name'), ...(env.managed ? {} : { env_type: type }), executable: env.managed ? env.executable : executable || null, conda_env: type === 'conda' ? f.get('conda_env') || null : null, env_vars_json: vars, description: f.get('description') || null });
      if (env.managed) {
        const packages = String(f.get('packages') || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean);
        const rebuilt = await post<Env>(`/environments/${env.id}/rebuild`, { packages, base_executable: executable || null });
        toast(rebuilt.status === 'creating' ? 'Environment rebuild queued' : rebuilt.last_error || 'Environment updated', rebuilt.last_error ? 'error' : 'success');
      } else toast('Environment updated');
      done();
    } catch (error) { toast(error instanceof Error ? error.message : 'Could not update environment', 'error'); }
  }
  return (
    <Modal title="Edit environment" subtitle="Update this runtime configuration." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <div className="field-row">
          <label className="field"><span>Name</span><input name="name" defaultValue={env.name} required autoFocus /></label>
          <label className="field"><span>Type</span>
            <select name="type" value={kind} onChange={e => setKind(e.target.value)} disabled={env.managed}>
              <option value="python">Python</option>
              <option value="conda">Conda</option>
            </select>
          </label>
        </div>
        {kind === 'conda' && <label className="field"><span>Conda environment name</span><input name="conda_env" defaultValue={env.conda_env || ''} required /></label>}
        <FilePicker value={executable} onChange={setExecutable} label={env.managed ? 'Base Python executable' : kind === 'conda' ? 'Conda executable' : 'Python executable'} placeholder={env.managed ? 'Optional — uses the previous base Python' : 'Required executable path'} />
        {env.managed && <label className="field"><span>Libraries <em>One pip requirement per line</em></span><textarea name="packages" defaultValue={(env.packages_json || []).join('\n')} /></label>}
        {env.build_log && <details><summary>Latest build log</summary><pre className="code-input" style={{ maxHeight: 180, overflow: 'auto' }}>{env.build_log}</pre></details>}
        <label className="field"><span>Environment variables <em>JSON, optional</em></span><textarea name="vars" defaultValue={env.env_vars_json ? JSON.stringify(env.env_vars_json, null, 2) : ''} placeholder={'{"API_MODE": "production"}'} /></label>
        <label className="field"><span>Description <em>Optional</em></span><input name="description" defaultValue={env.description || ''} placeholder="What is this environment for?" /></label>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit">{env.managed ? 'Save & rebuild' : 'Save changes'}</Button>
        </div>
      </form>
    </Modal>
  );
}

/* ─── Workflows ───────────────────────────────────────── */
function Workflows() {
  const [items, setItems] = useState<Workflow[]>([]);
  const [open, setOpen] = useState(false);
  const navTo = useNavigate();
  const { toast } = useToast();
  const load = () => api<Workflow[]>('/workflows').then(setItems).catch(() => {});
  useEffect(() => { void load(); }, []);

  const runWorkflow = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    try {
      const run = await post<Run>(`/workflows/${id}/run`, { parameters: {} });
      navTo(`/runs/${run.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
    }
  };

  return (
    <>
      <PageHeader
        eyebrow="AUTOMATION"
        title="Workflows"
        subtitle="Orchestrate scripts and notebooks with schedules and dependencies."
        action={<Button onClick={() => setOpen(true)}><Plus size={15} /> New workflow</Button>}
      />
      {items.length > 0 ? (
        <div className="workflow-grid">
          {items.map(w => (
            <article className="workflow-card" key={w.id} onClick={() => navTo(`/workflows/${w.id}`)}>
              <div className="workflow-top">
                <div className="workflow-glyph"><GitBranch size={18} /></div>
                <StatusBadge value={w.enabled ? 'enabled' : 'disabled'} />
              </div>
              <h3>{w.name}</h3>
              <p>{w.description || 'No description yet'}</p>
              <div className="workflow-meta">
                <span><Clock size={12} />{w.schedule_cron ? cronLabel(w.schedule_cron) : 'Manual runs'}</span>
                <span><ChevronsRight size={12} />Max {w.max_concurrent_runs}</span>
              </div>
              <div className="workflow-actions">
                <button onClick={e => runWorkflow(e, w.id)}>
                  <Play size={12} /> Run now
                </button>
                <span>View workflow →</span>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<GitBranch size={24} />}
          title="Build your first workflow"
          text="Combine existing scripts into reliable, observable, dependency-ordered runs."
          action={<Button onClick={() => setOpen(true)}>Create workflow</Button>}
        />
      )}
      {open && <WorkflowModal onClose={() => setOpen(false)} done={id => navTo(`/workflows/${id}`)} />}
    </>
  );
}

function WorkflowModal({ onClose, done }: { onClose: () => void; done: (id: number) => void }) {
  const { toast } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [envs, setEnvs] = useState<Env[]>([]);
  const [projectId, setProjectId] = useState('');
  const [environmentId, setEnvironmentId] = useState('');
  useEffect(() => {
    // Preselect the only project (and its default runtime) — the common case
    // right after "Connect a project" should not require re-picking everything.
    api<Project[]>('/projects').then(list => {
      setProjects(list);
      if (list.length === 1) {
        setProjectId(String(list[0].id));
        if (list[0].default_environment_id) setEnvironmentId(String(list[0].default_environment_id));
      }
    }).catch(() => {});
    api<Env[]>('/environments').then(setEnvs).catch(() => {});
  }, []);
  const pickProject = (value: string) => {
    setProjectId(value);
    const project = projects.find(p => String(p.id) === value);
    if (project?.default_environment_id) setEnvironmentId(String(project.default_environment_id));
  };
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    try {
      const w = await post<Workflow>('/workflows', {
        name: f.get('name'), description: f.get('description') || null,
        schedule_cron: f.get('cron') || null, enabled: true,
        max_concurrent_runs: Number(f.get('concurrency')),
        project_id: projectId ? Number(projectId) : null,
        default_environment_id: environmentId ? Number(environmentId) : null,
        notify_webhook_url: f.get('webhook') || null,
        auto_pause_failures: f.get('autopause') ? Number(f.get('autopause')) : null,
      });
      toast('Workflow created');
      done(w.id);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not create workflow', 'error');
    }
  }
  return (
    <Modal title="Create a workflow" subtitle="Start with the basics. Add tasks on the next screen." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field"><span>Workflow name</span><input name="name" placeholder="Daily customer refresh" required autoFocus /></label>
        <label className="field"><span>Description <em>Optional</em></span><textarea name="description" placeholder="What does this workflow accomplish?" /></label>
        <div className="field-row">
          <label className="field"><span>Cron schedule <em>Optional</em></span><input name="cron" placeholder="0 6 * * *" /><small>Evaluated in UTC; shown elsewhere in your local time.</small></label>
          <label className="field compact"><span>Max active runs</span><input name="concurrency" type="number" min="1" defaultValue="1" /></label>
        </div>
        <div className="field-row">
          <label className="field"><span>Project <em>Optional</em></span>
            <select name="project_id" value={projectId} onChange={e => pickProject(e.target.value)}>
              <option value="">None</option>
              {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label className="field"><span>Default environment <em>Required for Python</em></span>
            <select name="default_environment_id" value={environmentId} onChange={e => setEnvironmentId(e.target.value)}>
              <option value="">No environment</option>
              {envs.map(e => <option key={e.id} value={e.id} disabled={!environmentUsable(e)}>{e.name}{e.status !== 'ready' ? ` (${e.status})` : ''}</option>)}
            </select>
          </label>
        </div>
        <div className="field-row">
          <label className="field"><span>Failure webhook <em>Optional — Slack/Teams URL</em></span>
            <input name="webhook" placeholder="https://hooks.slack.com/…" />
            <small>Notified on first failure and on recovery.</small>
          </label>
          <label className="field compact"><span>Auto-pause after <em>Optional</em></span>
            <input name="autopause" type="number" min="1" placeholder="e.g. 5" />
            <small>Consecutive failures before pausing.</small>
          </label>
        </div>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit">Create &amp; add tasks</Button>
        </div>
      </form>
    </Modal>
  );
}

function EditWorkflowModal({ w, onClose, done }: { w: Workflow; onClose: () => void; done: () => void }) {
  const { toast } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [envs, setEnvs] = useState<Env[]>([]);
  const [projectId, setProjectId] = useState(String(w.project_id ?? ''));
  const [environmentId, setEnvironmentId] = useState(String(w.default_environment_id ?? ''));
  useEffect(() => {
    api<Project[]>('/projects').then(setProjects).catch(() => {});
    api<Env[]>('/environments').then(setEnvs).catch(() => {});
  }, []);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    try {
      await put(`/workflows/${w.id}`, {
        name: f.get('name'), description: f.get('description') || null,
        schedule_cron: f.get('cron') || null, enabled: f.get('enabled') === 'on',
        max_concurrent_runs: Number(f.get('concurrency')),
        project_id: projectId ? Number(projectId) : null,
        default_environment_id: environmentId ? Number(environmentId) : null,
        notify_webhook_url: f.get('webhook') || null,
        auto_pause_failures: f.get('autopause') ? Number(f.get('autopause')) : null,
      });
      toast('Workflow saved');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not save workflow', 'error');
    }
  }
  return (
    <Modal title="Edit workflow" subtitle="Update this workflow's settings." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field"><span>Workflow name</span><input name="name" defaultValue={w.name} required autoFocus /></label>
        <label className="field"><span>Description <em>Optional</em></span><textarea name="description" defaultValue={w.description || ''} placeholder="What does this workflow accomplish?" /></label>
        <div className="field-row">
          <label className="field"><span>Cron schedule <em>Optional</em></span><input name="cron" defaultValue={w.schedule_cron || ''} placeholder="0 6 * * *" /><small>Evaluated in UTC; shown elsewhere in your local time.</small></label>
          <label className="field compact"><span>Max active runs</span><input name="concurrency" type="number" min="1" defaultValue={w.max_concurrent_runs} /></label>
        </div>
        <div className="field-row">
          <label className="field"><span>Project <em>Optional</em></span>
            <select name="project_id" value={projectId} onChange={e => setProjectId(e.target.value)}>
              <option value="">None</option>
              {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label className="field"><span>Default environment <em>Optional</em></span>
            <select name="default_environment_id" value={environmentId} onChange={e => setEnvironmentId(e.target.value)}>
              <option value="">No environment</option>
              {envs.map(e => <option key={e.id} value={e.id} disabled={!environmentUsable(e)}>{e.name}{e.status !== 'ready' ? ` (${e.status})` : ''}</option>)}
            </select>
          </label>
        </div>
        <div className="field-row">
          <label className="field"><span>Failure webhook <em>Optional — Slack/Teams URL</em></span>
            <input name="webhook" defaultValue={w.notify_webhook_url || ''} placeholder="https://hooks.slack.com/…" />
            <small>Notified on first failure and on recovery.</small>
          </label>
          <label className="field compact"><span>Auto-pause after <em>Optional</em></span>
            <input name="autopause" type="number" min="1" defaultValue={w.auto_pause_failures ?? ''} placeholder="e.g. 5" />
            <small>Consecutive failures before pausing.</small>
          </label>
        </div>
        <label className="field toggle-field">
          <span>Enabled</span>
          <label className="toggle"><input type="checkbox" name="enabled" defaultChecked={w.enabled} /><span /></label>
        </label>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit">Save changes</Button>
        </div>
      </form>
    </Modal>
  );
}

/* ─── Workflow Detail ─────────────────────────────────── */
function WorkflowDetail() {
  const { id } = useParams<{ id: string }>();
  const [w, setW] = useState<Workflow>();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [envs, setEnvs] = useState<Env[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [backfillOpen, setBackfillOpen] = useState(false);
  const [runParamsOpen, setRunParamsOpen] = useState(false);
  const [editingTask, setEditingTask] = useState<Task | null>(null);
  const navTo = useNavigate();
  const { toast } = useToast();

  const load = () => {
    api<Workflow>(`/workflows/${id}`).then(setW).catch(() => {});
    api<Task[]>(`/workflows/${id}/tasks`).then(setTasks).catch(() => {});
    api<Run[]>(`/runs?workflow_id=${id}`).then(setRuns).catch(() => {});
    api<Project[]>('/projects').then(setProjects).catch(() => {});
    api<Env[]>('/environments').then(setEnvs).catch(() => {});
  };

  const deleteWorkflow = async () => {
    if (!confirm('Delete this workflow? All tasks and run history will be permanently removed.')) return;
    await del(`/workflows/${id!}`);
    toast('Workflow deleted', 'info');
    navTo('/workflows');
  };

  const removeTask = async (t: Task) => {
    if (!confirm(`Remove task "${t.name}"? Other tasks that depend on it will be updated.`)) return;
    try {
      await del(`/tasks/${t.id}`);
      toast('Task removed', 'info');
      load();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not remove task', 'error');
    }
  };

  const runNow = async () => {
    try {
      const run = await post<Run>(`/workflows/${id}/run`, { parameters: {} });
      navTo(`/runs/${run.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
    }
  };

  useEffect(() => { load(); }, [id]);
  useEffect(() => {
    const refreshRuns = () => void api<Run[]>(`/runs?workflow_id=${id}`).then(setRuns).catch(() => {});
    const u1 = rrws.on('run_created', e => { if (String(e.workflow_id) === id) refreshRuns(); });
    const u2 = rrws.on('run_updated', refreshRuns);
    return () => { u1(); u2(); };
  }, [id]);
  useLiveRefresh(runs.some(r => LIVE(r.status)), () => void api<Run[]>(`/runs?workflow_id=${id}`).then(setRuns).catch(() => {}));

  if (!w) return (
    <div>
      <div style={{ height: 28, width: 120, marginBottom: 24 }} className="skeleton-line" />
      <SkeletonCard /><SkeletonCard />
    </div>
  );

  const orderedTasks = topologicallyOrdered(tasks);
  return (
    <>
      <div className="detail-head">
        <Link to="/workflows"><ArrowLeft size={14} /> Workflows</Link>
        <div className="detail-head-row">
          <div className="workflow-glyph large"><GitBranch size={22} /></div>
          <div className="detail-head-text">
            <span className="eyebrow">WORKFLOW</span>
            <h1>{w.name}</h1>
            <p>{w.description || 'No description yet'}</p>
          </div>
          <div className="detail-actions">
            <Button variant="ghost" onClick={() => setEditOpen(true)}><Pencil size={13} /> Edit</Button>
            <Button variant="ghost" onClick={() => setAddOpen(true)}><Plus size={13} /> Add task</Button>
            <Button onClick={runNow}><Play size={13} /> Run now</Button>
            <Button variant="ghost" onClick={() => setRunParamsOpen(true)} title="Run with one-off parameters"><SlidersHorizontal size={13} /> Run with…</Button>
            <Button variant="ghost" onClick={() => setBackfillOpen(true)}><Calendar size={13} /> Backfill</Button>
            <Button variant="danger" onClick={deleteWorkflow}><Trash2 size={13} /></Button>
          </div>
        </div>
      </div>

      <div className="summary-strip">
        <div><span>Status</span><StatusBadge value={w.enabled ? 'enabled' : 'disabled'} /></div>
        <div><span>Schedule</span><strong>{w.schedule_cron ? cronLabel(w.schedule_cron) : 'Manual only'}</strong></div>
        <div><span>Tasks</span><strong>{tasks.length}</strong></div>
        <div><span>Max active runs</span><strong>{w.max_concurrent_runs}</strong></div>
        <div><span>Total runs</span><strong>{runs.length}</strong></div>
        {w.project_id && <div><span>Project</span><strong>{projects.find(p => p.id === w.project_id)?.name ?? '—'}</strong></div>}
        {w.default_environment_id && <div><span>Default env</span><strong>{envs.find(e => e.id === w.default_environment_id)?.name ?? '—'}</strong></div>}
      </div>

      {runs.length > 0 && <RunMiniHistory runs={runs} />}

      {runs.length > 0 && (
        <div className="panel" style={{ marginBottom: 20 }}>
          <div className="panel-head"><div><h2>Activity</h2><p>Runs per day, this workflow only</p></div></div>
          <RunHeatmap workflowId={Number(id)} />
        </div>
      )}

      {orderedTasks.length > 1 && (
        <div className="panel" style={{ marginBottom: 20 }}>
          <div className="panel-head">
            <div><h2>Graph</h2><p>Columns run left to right; tasks in the same column execute in parallel.</p></div>
          </div>
          <DagGraph tasks={orderedTasks.map(t => ({
            name: t.name, task_type: t.task_type, depends_on: t.depends_on_json || [],
          }))} />
        </div>
      )}

      <div className="panel" style={{ marginBottom: 20 }}>
        <div className="panel-head">
          <div>
            <h2>Tasks</h2>
            <p>Each task starts once everything it depends on has succeeded.</p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => setAddOpen(true)}><Plus size={13} /> Add task</Button>
        </div>
        {orderedTasks.length > 0 ? (
          <div className="task-flow">
            {orderedTasks.map((t, i) => (
              <div key={t.id}>
                {i > 0 && <div className="task-flow-connector" />}
                <div className="task-card">
                  <div className="task-card-top">
                    <TaskTypeBadge type={t.task_type} />
                    <span className="task-card-name">{t.name}</span>
                    <div className="task-card-actions">
                      <button className="edit-link" onClick={() => setEditingTask(t)}>Edit</button>
                      <button className="delete-link" onClick={() => removeTask(t)}>Remove</button>
                    </div>
                  </div>
                  {(t.command || t.script_path || t.notebook_path || t.sql_path) && (
                    <div className="task-card-cmd">
                      {t.command || t.script_path || t.notebook_path || t.sql_path}
                    </div>
                  )}
                  <div className="task-card-meta">
                    {t.depends_on_json.length > 0 && (
                      <span><GitMerge size={11} />Depends on: {t.depends_on_json.join(', ')}</span>
                    )}
                    {t.parameters_json && Object.keys(t.parameters_json).length > 0 && (
                      <span><SlidersHorizontal size={11} />{Object.entries(t.parameters_json).map(([k, v]) => `${k}=${String(v)}`).join(', ')}</span>
                    )}
                    {t.retries > 0 && <span><RefreshCw size={11} />{t.retries} {t.retries === 1 ? 'retry' : 'retries'}</span>}
                    {t.timeout_seconds && <span><Clock size={11} />Timeout: {t.timeout_seconds}s</span>}
                    {t.environment_id && <span><Cpu size={11} />{envs.find(e => e.id === t.environment_id)?.name ?? 'Custom env'}</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={<Zap size={22} />}
            title="No tasks yet"
            text="Add your first task to start building this workflow."
            action={<Button onClick={() => setAddOpen(true)}><Plus size={15} /> Add first task</Button>}
          />
        )}
      </div>

      {runs.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <div><h2>Recent runs</h2><p>{runs.length} total executions</p></div>
            <Link className="panel-link" to="/runs">See all →</Link>
          </div>
          <RunTable runs={runs.slice(0, 8)} flows={[w]} onChanged={load} />
        </div>
      )}

      {addOpen && <TaskModal workflowId={id!} tasks={tasks} project={projects.find(p => p.id === w.project_id)} defaultEnvironment={envs.find(e => e.id === (w.default_environment_id || projects.find(p => p.id === w.project_id)?.default_environment_id))} onClose={() => setAddOpen(false)} done={() => { setAddOpen(false); load(); }} />}
      {editOpen && <EditWorkflowModal w={w} onClose={() => setEditOpen(false)} done={() => { setEditOpen(false); load(); }} />}
      {runParamsOpen && <RunParamsModal workflowId={id!} onClose={() => setRunParamsOpen(false)} />}
      {backfillOpen && <BackfillModal workflowId={id!} onClose={() => setBackfillOpen(false)} />}
      {editingTask && <EditTaskModal task={editingTask} tasks={tasks} project={projects.find(p => p.id === (editingTask.project_id || w.project_id))} onClose={() => setEditingTask(null)} done={() => { setEditingTask(null); load(); }} />}
    </>
  );
}

/** Order tasks the same way the worker will: dependencies first, then id. */
function topologicallyOrdered(tasks: Task[]): Task[] {
  const byName = new Map(tasks.map(t => [t.name, t]));
  const ordered: Task[] = [];
  const placed = new Set<string>();
  let remaining = tasks.slice().sort((a, b) => a.id - b.id);
  while (remaining.length) {
    const ready = remaining.filter(t => (t.depends_on_json || []).every(d => placed.has(d) || !byName.has(d)));
    if (!ready.length) return [...ordered, ...remaining]; // cycle safety: show the rest as-is
    for (const t of ready) { ordered.push(t); placed.add(t.name); }
    remaining = remaining.filter(t => !placed.has(t.name));
  }
  return ordered;
}

function RunMiniHistory({ runs }: { runs: Run[] }) {
  const recent = [...runs].slice(0, 40).reverse();
  const stats = { success: runs.filter(r => r.status === 'success').length, failed: runs.filter(r => r.status === 'failed').length };
  const durs = runs.filter(r => r.duration_seconds != null);
  const avg = durs.length ? durs.reduce((a, r) => a + r.duration_seconds!, 0) / durs.length : 0;
  return (
    <div className="panel" style={{ marginBottom: 20 }}>
      <div className="panel-head">
        <div>
          <h2>Run history at a glance</h2>
          <p>{runs.length} total · {stats.success} succeeded · {stats.failed} failed{avg ? ` · avg ${formatDuration(avg)}` : ''}</p>
        </div>
      </div>
      <div className="mini-dots">
        {recent.map(r => (
          <Link key={r.id} to={`/runs/${r.id}`} className={`mini-dot ${r.status}`} title={`#${r.id} · ${r.status} · ${formatDate(r.created_at)}`} />
        ))}
      </div>
    </div>
  );
}

function RunParamsModal({ workflowId, onClose }: { workflowId: string; onClose: () => void }) {
  const { toast } = useToast();
  const navTo = useNavigate();
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const raw = String(new FormData(e.currentTarget).get('parameters') || '').trim();
    let parameters: Record<string, unknown> = {};
    if (raw) {
      try { parameters = JSON.parse(raw); } catch { toast('Parameters must be valid JSON', 'error'); return; }
    }
    setBusy(true);
    try {
      const run = await post<Run>(`/workflows/${workflowId}/run`, { parameters });
      navTo(`/runs/${run.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
      setBusy(false);
    }
  }
  return (
    <Modal title="Run with parameters" subtitle="Start a run with one-off values for this execution." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field">
          <span>Parameters <em>JSON</em></span>
          <textarea className="code-input" name="parameters" placeholder={'{"region": "ca", "ds": "2026-07-01"}'} autoFocus />
        </label>
        <div className="callout">
          Every task template can reference these values, e.g. <code>{'{{ region }}'}</code>.
          They override the built-ins (<code>ds</code>, <code>ts</code>) and merge under any per-task parameters.
        </div>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy}>{busy ? 'Starting…' : 'Run workflow'}</Button>
        </div>
      </form>
    </Modal>
  );
}

function BackfillModal({ workflowId, onClose }: { workflowId: string; onClose: () => void }) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const today = new Date().toISOString().slice(0, 10);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const from = String(f.get('from')), to = String(f.get('to'));
    let parameters: Record<string, unknown> = {};
    const raw = String(f.get('parameters') || '').trim();
    if (raw) {
      try { parameters = JSON.parse(raw); } catch { toast('Parameters must be valid JSON', 'error'); return; }
    }
    setBusy(true);
    try {
      const runs = await post<Run[]>(`/workflows/${workflowId}/backfill`, { from, to, parameters });
      toast(runs.length ? `Queued ${runs.length} backfill run${runs.length === 1 ? '' : 's'}` : 'All dates in that range already have backfill runs', 'info');
      onClose();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not queue backfill', 'error');
    } finally { setBusy(false); }
  }
  return (
    <Modal title="Backfill" subtitle="Queue one run per date. Each run receives its date as {{ ds }}." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <div className="field-row">
          <label className="field"><span>From</span><input name="from" type="date" defaultValue={today} required autoFocus /></label>
          <label className="field"><span>To</span><input name="to" type="date" defaultValue={today} required /></label>
        </div>
        <label className="field"><span>Parameters <em>JSON, optional — added to every run</em></span>
          <textarea className="code-input" name="parameters" placeholder={'{"region": "ca"}'} />
        </label>
        <div className="callout">Dates are inclusive. Days that already have a backfill run are skipped automatically.</div>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy}>{busy ? 'Queueing…' : 'Queue backfill'}</Button>
        </div>
      </form>
    </Modal>
  );
}

function TaskModal({ workflowId, tasks, project, defaultEnvironment, onClose, done }: { workflowId: string; tasks: Task[]; project?: Project; defaultEnvironment?: Env; onClose: () => void; done: () => void }) {
  const { toast } = useToast();
  const [type, setType] = useState('shell');
  const [path, setPath] = useState('');
  const [cwd, setCwd] = useState('');
  const [envs, setEnvs] = useState<Env[]>([]);
  const fileKey = type === 'python' ? 'script_path' : type === 'notebook' ? 'notebook_path' : 'sql_path';
  useEffect(() => { api<Env[]>('/environments').then(setEnvs).catch(() => {}); }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    let parameters = null;
    try { parameters = f.get('parameters') ? JSON.parse(String(f.get('parameters'))) : null; }
    catch { toast('Parameters must be valid JSON', 'error'); return; }
    try {
      await post(`/workflows/${workflowId}/tasks`, {
        name: f.get('name'), task_type: type,
        command: type === 'shell' ? f.get('command') || null : null,
        [fileKey]: type !== 'shell' ? path || null : null,
        cwd: cwd || null, depends_on_json: f.getAll('dependencies'),
        parameters_json: parameters,
        retries: Number(f.get('retries')), retry_delay_seconds: Number(f.get('delay')),
        timeout_seconds: f.get('timeout') ? Number(f.get('timeout')) : null,
        environment_id: f.get('environment_id') ? Number(f.get('environment_id')) : null,
      });
      toast('Task added');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not add task', 'error');
    }
  }
  const usableEnvs = envs.filter(environmentUsable);
  const needsEnvironment = (type === 'python' || type === 'notebook') && !defaultEnvironment && usableEnvs.length === 0;
  return (
    <Modal title="Add a task" subtitle="Run existing code without changing how it is written." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        {project ? (
          <div className="callout"><strong>{project.name}</strong><br /><span>Files open from {project.root_path}</span>{defaultEnvironment && <><br /><span>Runtime: {defaultEnvironment.name}</span></>}</div>
        ) : (
          <div className="callout">No project is attached to this workflow. File browsing starts at the server browse root.</div>
        )}
        <label className="field"><span>Task name</span><input name="name" placeholder="extract-customers" required autoFocus /></label>
        <div className="segmented">
          {([['shell', '›_', 'Shell'], ['python', 'Py', 'Python'], ['notebook', 'Nb', 'Notebook'], ['sql', 'SQL', 'SQL']] as const).map(([v, icon, label]) => (
            <button key={v} type="button" className={type === v ? 'active' : ''} onClick={() => { setType(v); setPath(''); }}>
              <i>{icon}</i>{label}
            </button>
          ))}
        </div>
        {type === 'shell'
          ? <label className="field"><span>Command</span><textarea className="code-input" name="command" placeholder="python scripts/daily.py --date {{ ds }}" required /></label>
          : <FilePicker value={path} onChange={setPath} browseFrom={project?.root_path} label={`${type[0].toUpperCase() + type.slice(1)} file`} placeholder={`Choose a ${type === 'python' ? '.py' : type === 'notebook' ? '.ipynb' : '.sql'} file`} />
        }
        <FilePicker value={cwd} onChange={setCwd} browseFrom={project?.root_path} mode="directories" label="Working directory" placeholder={project ? `Optional — defaults to ${project.name}` : 'Optional — defaults to process directory'} />
        {needsEnvironment ? (
          <div className="callout" style={{ borderColor: 'var(--warning-border)', color: 'var(--warning)' }}>
            {type === 'python' ? 'Python' : 'Notebook'} tasks need an execution environment, and none exists yet.{' '}
            <Link to="/environments" style={{ fontWeight: 600, textDecoration: 'underline' }}>Create one first →</Link>
          </div>
        ) : (
          <label className="field"><span>Environment override <em>Optional — inherits workflow default</em></span>
            <select name="environment_id" required={(type === 'python' || type === 'notebook') && !defaultEnvironment}>
              <option value="">Inherit from workflow</option>
              {envs.map(e => <option key={e.id} value={e.id} disabled={!environmentUsable(e)}>{e.name}{e.status !== 'ready' ? ` (${e.status})` : ''}</option>)}
            </select>
          </label>
        )}
        <label className="field"><span>Parameters <em>JSON, optional — used as {'{{ key }}'} in templates</em></span>
          <textarea className="code-input" name="parameters" placeholder={'{"region": "ca"}'} />
        </label>
        {tasks.length > 0 && (
          <fieldset style={{ border: '1px solid var(--border)', borderRadius: 'var(--r-md)', padding: '12px 14px' }}>
            <legend style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)', padding: '0 6px' }}>Run after <em style={{ fontWeight: 400, color: 'var(--text-3)' }}>Optional</em></legend>
            <div className="checks">
              {tasks.map(t => (
                <label key={t.id}><input type="checkbox" name="dependencies" value={t.name} />{t.name}</label>
              ))}
            </div>
          </fieldset>
        )}
        <div className="field-row">
          <label className="field"><span>Retries</span><input name="retries" type="number" min="0" defaultValue="0" /></label>
          <label className="field"><span>Retry delay (sec)</span><input name="delay" type="number" min="0" defaultValue="0" /></label>
        </div>
        <label className="field"><span>Timeout (sec) <em>Optional</em></span><input name="timeout" type="number" min="1" /></label>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={needsEnvironment}>Add task</Button>
        </div>
      </form>
    </Modal>
  );
}

function EditTaskModal({ task, tasks, project, onClose, done }: { task: Task; tasks: Task[]; project?: Project; onClose: () => void; done: () => void }) {
  const { toast } = useToast();
  const [type, setType] = useState(task.task_type);
  const [path, setPath] = useState(task.script_path || task.notebook_path || task.sql_path || '');
  const [cwd, setCwd] = useState(task.cwd || '');
  const [envs, setEnvs] = useState<Env[]>([]);
  const [environmentId, setEnvironmentId] = useState(String(task.environment_id ?? ''));
  const fileKey = type === 'python' ? 'script_path' : type === 'notebook' ? 'notebook_path' : 'sql_path';
  useEffect(() => { api<Env[]>('/environments').then(setEnvs).catch(() => {}); }, []);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    let parameters = null;
    try { parameters = f.get('parameters') ? JSON.parse(String(f.get('parameters'))) : null; }
    catch { toast('Parameters must be valid JSON', 'error'); return; }
    try {
      await put(`/tasks/${task.id}`, {
        name: f.get('name'), task_type: type,
        command: type === 'shell' ? f.get('command') || null : null,
        [fileKey]: type !== 'shell' ? path || null : null,
        cwd: cwd || null, depends_on_json: f.getAll('dependencies'),
        parameters_json: parameters,
        retries: Number(f.get('retries')), retry_delay_seconds: Number(f.get('delay')),
        timeout_seconds: f.get('timeout') ? Number(f.get('timeout')) : null,
        project_id: task.project_id ?? null,
        environment_id: environmentId ? Number(environmentId) : null,
      });
      toast('Task updated');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not update task', 'error');
    }
  }
  return (
    <Modal title="Edit task" subtitle="Update this task's configuration." onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field"><span>Task name</span><input name="name" defaultValue={task.name} required autoFocus /></label>
        <div className="segmented">
          {([['shell', '›_', 'Shell'], ['python', 'Py', 'Python'], ['notebook', 'Nb', 'Notebook'], ['sql', 'SQL', 'SQL']] as const).map(([v, icon, label]) => (
            <button key={v} type="button" className={type === v ? 'active' : ''} onClick={() => { setType(v); setPath(''); }}>
              <i>{icon}</i>{label}
            </button>
          ))}
        </div>
        {type === 'shell'
          ? <label className="field"><span>Command</span><textarea className="code-input" name="command" defaultValue={task.command || ''} placeholder="python scripts/daily.py" required /></label>
          : <FilePicker value={path} onChange={setPath} browseFrom={project?.root_path} label={`${type[0].toUpperCase() + type.slice(1)} file`} placeholder={`Choose a ${type === 'python' ? '.py' : type === 'notebook' ? '.ipynb' : '.sql'} file`} />
        }
        <FilePicker value={cwd} onChange={setCwd} browseFrom={project?.root_path} mode="directories" label="Working directory" placeholder="Optional — defaults to project root" />
        <label className="field"><span>Environment override <em>Optional — inherits workflow default</em></span>
          <select name="environment_id" value={environmentId} onChange={e => setEnvironmentId(e.target.value)}>
            <option value="">Inherit from workflow</option>
            {envs.map(e => <option key={e.id} value={e.id} disabled={!environmentUsable(e)}>{e.name}{e.status !== 'ready' ? ` (${e.status})` : ''}</option>)}
          </select>
        </label>
        <label className="field"><span>Parameters <em>JSON, optional — used as {'{{ key }}'} in templates</em></span>
          <textarea className="code-input" name="parameters" defaultValue={task.parameters_json ? JSON.stringify(task.parameters_json, null, 2) : ''} placeholder={'{"region": "ca"}'} />
        </label>
        {tasks.filter(t => t.id !== task.id).length > 0 && (
          <fieldset style={{ border: '1px solid var(--border)', borderRadius: 'var(--r-md)', padding: '12px 14px' }}>
            <legend style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-2)', padding: '0 6px' }}>Run after <em style={{ fontWeight: 400, color: 'var(--text-3)' }}>Optional</em></legend>
            <div className="checks">
              {tasks.filter(t => t.id !== task.id).map(t => (
                <label key={t.id}><input type="checkbox" name="dependencies" value={t.name} defaultChecked={task.depends_on_json.includes(t.name)} />{t.name}</label>
              ))}
            </div>
          </fieldset>
        )}
        <div className="field-row">
          <label className="field"><span>Retries</span><input name="retries" type="number" min="0" defaultValue={task.retries} /></label>
          <label className="field"><span>Retry delay (sec)</span><input name="delay" type="number" min="0" defaultValue={task.retry_delay_seconds} /></label>
        </div>
        <label className="field"><span>Timeout (sec) <em>Optional</em></span><input name="timeout" type="number" min="1" defaultValue={task.timeout_seconds ?? ''} /></label>
        <div className="modal-actions">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit">Save changes</Button>
        </div>
      </form>
    </Modal>
  );
}

/* ─── Artifacts ───────────────────────────────────────── */
type ArtifactItem = {
  id: number;
  name: string;
  artifact_type: string;
  size_bytes?: number | null;
  workflow_run_id?: number | null;
  created_at: string;
};

function Artifacts() {
  const [items, setItems] = useState<ArtifactItem[]>([]);
  useEffect(() => { api<ArtifactItem[]>('/artifacts').then(setItems).catch(() => {}); }, []);

  return (
    <>
      <PageHeader eyebrow="OUTPUTS" title="Artifacts" subtitle="Browse notebooks, reports, and files produced by your workflows." />
      {items.length > 0 ? (
        <div className="list-panel">
          {items.map(x => (
            <div className="list-item" key={x.id}>
              <div className="resource-icon artifact"><Package size={16} /></div>
              <div className="list-copy">
                <h3>{x.name}</h3>
                <p>
                  {x.artifact_type} · {x.size_bytes ? formatBytes(x.size_bytes) : 'Size unavailable'} · {timeAgo(x.created_at)}
                  {x.workflow_run_id && <> · <Link to={`/runs/${x.workflow_run_id}`} style={{ color: 'var(--accent)' }}>run #{x.workflow_run_id}</Link></>}
                </p>
              </div>
              <a className="btn btn-ghost btn-sm" href={`/api/artifacts/${x.id}/download`}>
                <Download size={13} /> Download
              </a>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<Package size={24} />}
          title="No artifacts yet"
          text="Executed notebooks and task outputs will appear here when workflows produce them."
        />
      )}
    </>
  );
}

/* ─── Settings Page ──────────────────────────────────── */
function SettingsPage() {
  const [theme, setTheme] = useState<string>(
    () => document.documentElement.dataset.theme ?? 'dark'
  );

  const switchTheme = (t: string) => {
    document.documentElement.dataset.theme = t;
    localStorage.setItem('runrail-theme', t);
    setTheme(t);
  };

  return (
    <>
      <PageHeader eyebrow="SYSTEM" title="Settings" subtitle="Configure RunRail and view system information." />

      <div className="settings-grid">
        <div className="about-card">
          <div className="about-logo">R</div>
          <div className="about-text">
            <h2>RunRail</h2>
            <p>A self-hosted workflow control plane for Python scripts, Jupyter notebooks, SQL tasks, shell commands, schedules, backfills, logs, artifacts, and workflow runs.</p>
            <div className="about-links">
              <a className="btn btn-ghost btn-sm" href="https://github.com" target="_blank" rel="noreferrer">
                <Info size={13} /> GitHub
              </a>
            </div>
          </div>
        </div>

        <div className="settings-section">
          <h3><Settings size={14} /> Appearance</h3>
          <div className="settings-row">
            <span className="settings-label">Theme</span>
            <div style={{ display: 'flex', gap: 6 }}>
              {(['dark', 'light'] as const).map(t => (
                <button
                  key={t}
                  className={`btn btn-sm ${theme === t ? 'btn-primary' : 'btn-ghost'}`}
                  onClick={() => switchTheme(t)}
                >
                  {t === 'dark' ? '☾ Dark' : '☀ Light'}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="settings-section">
          <h3><Database size={14} /> System</h3>
          <div className="settings-row">
            <span className="settings-label">API endpoint</span>
            <span className="settings-value">{window.location.origin}/api</span>
          </div>
          <div className="settings-row">
            <span className="settings-label">Mode</span>
            <span className="settings-value">Self-hosted</span>
          </div>
        </div>

        <div className="settings-section">
          <h3><FileText size={14} /> Navigation</h3>
          {NAV.filter(n => n.href !== '/settings').map(({ href, icon: Icon, label }) => (
            <div className="settings-row" key={href}>
              <span className="settings-label" style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                <Icon size={13} />{label}
              </span>
              <Link className="btn btn-ghost btn-sm" to={href}>Open →</Link>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

/* ─── Wallboard (TV mode) ─────────────────────────────── */
function formatEta(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** Median duration of the workflow's last five successful runs, in seconds. */
function medianDuration(runs: Run[], workflowId: number): number | null {
  const durations = runs
    .filter(r => r.workflow_id === workflowId && r.status === 'success' && r.duration_seconds != null)
    .slice(0, 5)
    .map(r => r.duration_seconds!)
    .sort((a, b) => a - b);
  return durations.length ? durations[Math.floor(durations.length / 2)] : null;
}

const WB_RANK: Record<string, number> = {
  failed: 0, overdue: 1, running: 2, queued: 3, success: 4, cancelled: 5, never: 6,
};

function Wallboard() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [flows, setFlows] = useState<Workflow[]>([]);
  const [clock, setClock] = useState(() => new Date());
  const [blooms, setBlooms] = useState<Record<number, string>>({});
  const prevStatuses = useRef(new Map<number, string>());
  const gridRef = useRef<HTMLDivElement>(null);

  const load = () => {
    api<Run[]>('/runs?limit=300').then(setRuns).catch(() => {});
    api<Workflow[]>('/workflows').then(setFlows).catch(() => {});
  };
  useEffect(() => {
    load();
    const poll = window.setInterval(load, 5000);
    const tick = window.setInterval(() => setClock(new Date()), 1000);
    const u1 = rrws.on('run_updated', load);
    const u2 = rrws.on('run_created', load);
    return () => { window.clearInterval(poll); window.clearInterval(tick); u1(); u2(); };
  }, []);
  const now = useNow(runs.some(r => r.status === 'running'));

  const live = runs.filter(r => LIVE(r.status));
  const failures = runs.filter(r => r.status === 'failed'
    && Date.now() - new Date(r.created_at).getTime() < DAY).slice(0, 4);

  // Per-workflow triage state: latest outcome, failure streak, 7-day rate,
  // next occurrence, and silent-scheduler (overdue) detection.
  const tiles = flows.map(w => {
    const mine = runs.filter(r => r.workflow_id === w.id);
    const completed = mine.filter(r => r.status === 'success' || r.status === 'failed');
    const last = completed[0];
    let streak = 0;
    while (streak < completed.length && completed[streak].status === 'failed') streak++;
    const streakStart = streak ? completed[streak - 1].created_at : null;
    const week = completed.filter(r => Date.now() - new Date(r.created_at).getTime() < 7 * DAY);
    const rate7d = week.length ? Math.round(week.filter(r => r.status === 'success').length / week.length * 100) : null;
    const running = live.some(r => r.workflow_id === w.id && r.status === 'running');
    const next = w.enabled && w.schedule_cron ? nextCronOccurrence(w.schedule_cron, clock) : null;
    // Overdue: the schedule should have fired after the latest run, gave it a
    // 2-minute grace, and no newer run ever appeared — a silently dead scheduler.
    let overdue = false;
    if (w.enabled && w.schedule_cron && mine.length) {
      const expected = nextCronOccurrence(w.schedule_cron, new Date(mine[0].created_at));
      overdue = !!expected && expected.getTime() < clock.getTime() - 120_000
        && !mine.some(r => new Date(r.created_at) >= expected);
    }
    const status = running ? 'running'
      : last?.status === 'failed' ? 'failed'
      : overdue ? 'overdue'
      : (last?.status ?? (mine.length ? mine[0].status : 'never'));
    return { flow: w, mine, last, streak, streakStart, rate7d, next, overdue, status };
  }).sort((a, b) =>
    (WB_RANK[a.status] ?? 9) - (WB_RANK[b.status] ?? 9)
    || b.streak - a.streak
    || a.flow.name.localeCompare(b.flow.name));

  // One-shot bloom when a tile's status transitions.
  const signature = tiles.map(t => `${t.flow.id}:${t.status}`).join('|');
  useEffect(() => {
    const fresh: Record<number, string> = {};
    for (const tile of tiles) {
      const prev = prevStatuses.current.get(tile.flow.id);
      if (prev && prev !== tile.status) fresh[tile.flow.id] = tile.status;
      prevStatuses.current.set(tile.flow.id, tile.status);
    }
    if (Object.keys(fresh).length) setBlooms(b => ({ ...b, ...fresh }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);
  useFlip(gridRef, [signature]);

  const failing = tiles.filter(t => t.status === 'failed').length;
  const overdueCount = tiles.filter(t => t.status === 'overdue').length;
  const runningCount = live.filter(r => r.status === 'running').length;
  const mood = failing || overdueCount ? 'attention' : runningCount ? 'active' : 'calm';
  const verdict = failing ? `${failing} failing`
    : overdueCount ? `${overdueCount} overdue`
    : runningCount ? `${runningCount} running`
    : 'All systems nominal';
  const nextUp = tiles.filter(t => t.next).sort((a, b) => a.next!.getTime() - b.next!.getTime())[0];

  return (
    <div className={`wallboard wallboard--${mood}`}>
      <div className="wallboard-aurora" aria-hidden />
      <header className="wallboard-head">
        <span className="wallboard-brand">
          <span className="sidebar-logo"><span className="sidebar-logo-inner"><span /><span /><span /></span></span>
          RunRail
        </span>
        <span className={`wallboard-health wallboard-health--${mood}`}>
          <span className="wallboard-health-dot" />{verdict}
        </span>
        {nextUp && (
          <span className="wallboard-next" title={nextUp.next!.toLocaleString()}>
            <Clock size={13} /> Next: {nextUp.flow.name} in {formatEta(nextUp.next!.getTime() - clock.getTime())}
          </span>
        )}
        <span className="wallboard-clock">
          {clock.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
        </span>
        <Link to="/" className="wallboard-exit" title="Back to the app"><X size={16} /></Link>
      </header>

      {live.length > 0 && (
        <div className="wallboard-strip">
          {live.map(r => {
            const expected = r.status === 'running' ? medianDuration(runs, r.workflow_id) : null;
            const elapsed = r.started_at ? (now - new Date(r.started_at).getTime()) / 1000 : 0;
            const pct = expected ? Math.min(100, elapsed / expected * 100) : null;
            const over = expected != null && elapsed > expected * 1.15;
            return (
              <div key={r.id} className={`wallboard-live-card ${r.status}`}>
                <span className={`run-pulse ${r.status}`} />
                <div className="wallboard-live-body">
                  <b>{flows.find(w => w.id === r.workflow_id)?.name ?? `Workflow ${r.workflow_id}`}</b>
                  <small>#{r.id} · {r.status} · {liveDuration(r, now)}</small>
                  {pct != null && (
                    <div className={`wb-progress${over ? ' wb-progress--over' : ''}`}>
                      <div className="wb-progress-fill" style={{ width: `${pct}%` }} />
                    </div>
                  )}
                </div>
                {expected != null && r.status === 'running' && (
                  <span className={`wb-eta${over ? ' wb-eta--over' : ''}`}>
                    {over ? 'running long' : `~${formatEta(Math.max(0, (expected - elapsed) * 1000))} left`}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="wallboard-grid" ref={gridRef}>
        {tiles.map(({ flow: w, last, streak, streakStart, rate7d, next, status }) => (
          <div key={w.id} data-flip-id={String(w.id)}
               className={`wallboard-tile wb-${status}${blooms[w.id] ? ` wb-bloom wb-bloom-${blooms[w.id]}` : ''}`}
               onAnimationEnd={e => {
                 if (e.animationName === 'tile-bloom') setBlooms(({ [w.id]: _, ...rest }) => rest);
               }}>
            <div className="wallboard-tile-name">{w.name}</div>
            <div className="wallboard-tile-status" key={status}>{status === 'never' ? 'no runs' : status}</div>
            {status === 'failed' && streak > 0 && (
              <div className="wallboard-tile-streak">
                {streak} consecutive failure{streak === 1 ? '' : 's'}
                {streakStart ? ` · red for ${formatEta(clock.getTime() - new Date(streakStart).getTime())}` : ''}
              </div>
            )}
            <div className="wallboard-tile-meta">
              {last ? `${timeAgo(last.finished_at || last.created_at)}` : '—'}
              {w.schedule_cron ? ` · ${cronLabel(w.schedule_cron)}` : ' · manual'}
              {next ? ` · next in ${formatEta(next.getTime() - clock.getTime())}` : ''}
              {status !== 'failed' && rate7d != null ? ` · ${rate7d}% · 7d` : ''}
              {!w.enabled ? ' · paused' : ''}
            </div>
            <WorkflowSparkline runs={runs.filter(r => r.workflow_id === w.id).slice(0, 16)} />
          </div>
        ))}
        {flows.length === 0 && <div className="wallboard-empty">No workflows yet.</div>}
      </div>

      {failures.length > 0 && (
        <div className="wallboard-failures">
          <span className="wallboard-failures-label"><AlertTriangle size={14} /> Failures · 24h</span>
          {failures.map(r => (
            <span key={r.id} className="wallboard-failure">
              {flows.find(w => w.id === r.workflow_id)?.name ?? r.workflow_id} #{r.id} · {timeAgo(r.created_at)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/* ─── App Entry ───────────────────────────────────────── */
export default function App() {
  const location = useLocation();
  if (location.pathname === '/wallboard') return <Wallboard />;
  return <Shell />;
}
