import { CSSProperties, FormEvent, Fragment, ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal, flushSync } from 'react-dom';
import { Link, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import clsx from 'clsx';
import {
  GitBranch, History, FolderOpen, Cpu, Package, Play, Plus, Search, ChevronRight, Server, Bell,
  Clock, Calendar, Zap, GitMerge, Activity, AlertTriangle, RefreshCw, Trash2, Pencil, ArrowLeft,
  Terminal, Download, Settings, ChevronsRight, Ban, X, SlidersHorizontal, ShieldAlert, Share2,
  BookOpen, Lock, Sun, Moon, ArrowRight, MonitorPlay, Radio, Ellipsis, ChevronDown,
} from 'lucide-react';
import { api, del, post, put } from './api';
import { rrws } from './ws';
import { formatBytes, formatDate, formatDuration, localDateKey, timeAgo, viewerZone } from './format';
import { FilePicker } from './components/FilePicker';
import { RunHeatmap } from './components/Heatmap';
import { cronLabel, nextCronOccurrence } from './cron';
import { ScheduleBuilder } from './components/ScheduleBuilder';
import { Button, CancelButton, StatusBadge, CountUp, EmptyState, Modal, PageHeader, TaskTypeBadge, SkeletonCard, LoadingBar, CometCanvas } from './components/ui';
import { Board } from './components/Board';
import { untilLabel } from './pulseRail';
import { countdown, laneState, upcomingFires } from './board';
import { signalLabel, verdictLine, type Failing } from './pulseModel';
import { PulseProvider, usePulse } from './Pulse';
import { PeekProvider, usePeek } from './components/RunPeek';
import { LogViewer } from './components/LogViewer';
import { useToast } from './components/toast';
import { ApprovalGate, ApprovalInbox, useOpenApprovals } from './components/ApprovalGate';
import { ResumeButton } from './components/ResumeDialog';
import { SnoozeBadge, SnoozeControl } from './components/SnoozeControl';
import { WatchdogFields, watchdogValues } from './components/WatchdogFields';
import { NotificationBell } from './components/NotificationCenter';
import { ScheduleGapsPanel, useScheduleGaps, heatmapGapFeed } from './components/ScheduleGaps';
import { ApprovalField, approvalValues } from './components/ApprovalField';
import { RunRailMark } from './components/Brand';
import { useNow, useVisibleInterval } from './hooks';
import { SettingsPage } from './SettingsPage';
import {
  ServiceDetail, ServicesPage, ServicesPanel, WallboardServices, serviceCounts, useServices,
} from './Services';
import { attemptLabel, attemptTag, groupAttempts } from './attempts';
import { RunFormEditor, RunFormFields, runFormValue } from './components/RunForm';
import { RunChanges } from './components/RunChanges';
import { chainSummary, type ChainInfo } from './components/ChainStrip';
import { Pipeline, type PipeEnd } from './components/Pipeline';
import { collect, initialValues, needsAsking, type FormValues } from './runForms';
import { AttemptGroup } from './attempts';
import { LockField, LockBadge, lockValues } from './components/LockField';
import { LatestReportPanel, ReportPanel } from './components/ReportPanel';
import { LatestReportMeta } from './components/ReportPanel';
import { ShareRunModal } from './components/ShareRunModal';
import { LogSearch } from './components/LogSearch';
import { RunNotes, RunNotesIndicator, useRunNotesSummary } from './components/RunNotes';
import { TrendSpark, useTaskDurations } from './components/TrendSpark';
import { TaskDurationSeries } from './components/TrendSpark';

import { Run, TaskRun, Workflow, Project, Env, Task, DailyStat, Summary } from './types';


/* ─── Helpers ─────────────────────────────────────────── */
/** How a workflow starts, in a few words: its schedule, the workflows it runs
 *  after, or by hand. `names` maps workflow ids to names. */
function startsLabel(w: Workflow, names: Map<number, string>, manual = 'Manual only'): string {
  if (w.schedule_cron) return cronLabel(w.schedule_cron, w.schedule_timezone);
  if (w.after?.length) return `After ${w.after.map(id => names.get(id) ?? `#${id}`).join(', ')}`;
  return manual;
}

/** A trigger type as people say it. "upstream" is the chained start. */
function triggerLabel(trigger: string): string {
  return trigger === 'upstream' ? 'after' : trigger;
}

/** The "after" list a workflow form publishes through ScheduleBuilder. */
function afterValue(f: FormData): number[] {
  try {
    const value = JSON.parse(String(f.get('after') ?? '[]'));
    return Array.isArray(value) ? value.map(Number) : [];
  } catch {
    return [];
  }
}

const DAY = 86_400_000;
/** Unfinished, and therefore still cancellable and still worth polling.
 *  `waiting_approval` belongs here: routes_runs.cancel_run explicitly allows
 *  cancelling a parked run — without it a gate nobody decides would hold a
 *  concurrency slot forever with no way out of the UI. */
const LIVE = (status: string) =>
  status === 'running' || status === 'queued' || status === 'waiting_approval';

/** Split a stored second-count into the largest whole unit for editing. */
function splitTimeout(seconds?: number | null): { value: string; unit: string } {
  if (!seconds) return { value: '', unit: '1' };
  for (const unit of [86400, 3600, 60]) {
    if (seconds % unit === 0) return { value: String(seconds / unit), unit: String(unit) };
  }
  return { value: String(seconds), unit: '1' };
}

/** Render a number input + unit selector that submit as `timeout` and `timeout_unit`. */
function TimeoutField({ seconds }: { seconds?: number | null }) {
  const initial = splitTimeout(seconds);
  return (
    <label className="field"><span>Timeout <em>Optional — kills the task if it runs past this</em></span>
      <div className="path-input">
        <input name="timeout" type="number" min="1" placeholder="e.g. 30"
               defaultValue={initial.value} style={{ flex: 1 }} />
        <select name="timeout_unit" defaultValue={initial.unit} style={{ maxWidth: 120 }}>
          <option value="1">seconds</option>
          <option value="60">minutes</option>
          <option value="3600">hours</option>
          <option value="86400">days</option>
        </select>
      </div>
    </label>
  );
}

const timeoutSeconds = (f: FormData): number | null =>
  f.get('timeout') ? Number(f.get('timeout')) * Number(f.get('timeout_unit') || 1) : null;

const environmentUsable = (e: Env) => e.status === 'ready' || e.status === 'degraded';

/** "waiting on publish · 3m 12s" for a run parked on a gate. */
function waitingLine(gate: { task_name: string | null; created_at: string } | undefined, now: number): string {
  if (!gate) return 'waiting approval';
  const since = Math.max(0, (now - new Date(gate.created_at).getTime()) / 1000);
  return `waiting on ${gate.task_name ?? 'approval'} · ${formatDuration(since)}`;
}

function liveDuration(run: Run, now: number): string {
  if (run.duration_seconds != null) return formatDuration(run.duration_seconds);
  if (run.status === 'running' && run.started_at) {
    return formatDuration((now - new Date(run.started_at).getTime()) / 1000);
  }
  return '—';
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
  useVisibleInterval(hasLive, refresh, intervalMs);
}

/* ─── Shell ───────────────────────────────────────────── */
/* Four places a person goes every day, and the rest behind "More": projects
   and environments are set up once, logs and artifacts are looked up when
   something sends you there. The order is the order things exist in: what is
   happening, the workflows and services that make it happen, then the record
   of what they did. */
const NAV: { href: string; icon: typeof Radio; label: string; primary?: boolean }[] = [
  { href: '/',             icon: Radio,      label: 'Now',          primary: true },
  { href: '/workflows',    icon: GitBranch,  label: 'Workflows',    primary: true },
  { href: '/services',     icon: Server,     label: 'Services',     primary: true },
  { href: '/runs',         icon: History,    label: 'Runs',         primary: true },
  { href: '/projects',     icon: FolderOpen, label: 'Projects' },
  { href: '/environments', icon: Cpu,        label: 'Environments' },
  { href: '/logs',         icon: Search,     label: 'Log search' },
  { href: '/artifacts',    icon: Package,    label: 'Artifacts' },
  { href: '/settings',     icon: Settings,   label: 'Settings' },
];
const PRIMARY = NAV.filter(n => n.primary);
const SECONDARY = NAV.filter(n => !n.primary);

/** Anything on the page can ask for the command palette. */
const openPalette = () => window.dispatchEvent(new Event('runrail:palette'));

function useApiHealth(): 'online' | 'offline' | 'unknown' {
  const [health, setHealth] = useState<'online' | 'offline' | 'unknown'>('unknown');
  const check = () => void api<{ status: string }>('/health')
    .then(() => setHealth('online'))
    .catch(() => setHealth('offline'));
  useEffect(() => { check(); }, []);
  useVisibleInterval(true, check, 30_000);
  return health;
}

function Shell() {
  return (
    <PulseProvider>
      <PeekProvider>
        <ShellFrame />
      </PeekProvider>
    </PulseProvider>
  );
}

/** `g` then one of these goes straight to a place. */
const GO: Record<string, string> = { n: '/', w: '/workflows', r: '/runs', s: '/services', l: '/logs', a: '/artifacts' };

function ShellFrame() {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const location = useLocation();
  const navTo = useNavigate();
  const health = useApiHealth();
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    let chord = 0;   // when `g` was pressed, for the second key of a go-to
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setPaletteOpen(v => !v);
        return;
      }
      // Bare keys are shortcuts only when nobody is typing and nothing is open.
      const el = e.target as HTMLElement | null;
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      if (el?.closest('input, textarea, select, [contenteditable="true"], .modal-shade, .cmdk-shade')) return;
      if (document.querySelector('.modal-shade, .cmdk-shade')) return;
      const key = e.key.toLowerCase();
      if (key === '/') { e.preventDefault(); setPaletteOpen(true); return; }
      if (key === 'g') { chord = Date.now(); return; }
      if (GO[key] && Date.now() - chord < 1200) { e.preventDefault(); navTo(GO[key]); }
      chord = 0;
    };
    const onAsk = () => setPaletteOpen(true);
    window.addEventListener('keydown', onKey);
    window.addEventListener('runrail:palette', onAsk);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('runrail:palette', onAsk);
    };
  }, []);
  // The pill behind the current page is placed from measurements, so it tracks
  // any label width and glides between them.
  useLayoutEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const measure = () => {
      const active = nav.querySelector<HTMLElement>('.dock-link.active');
      // Measured against the nav itself: the "More" button sits inside its
      // own positioned wrapper, so its offsetLeft is 0 and the pill used to
      // land on the first item whenever a page behind More was open.
      const x = active ? active.getBoundingClientRect().left - nav.getBoundingClientRect().left - nav.clientLeft : 0;
      nav.style.setProperty('--ind-x', `${x}px`);
      nav.style.setProperty('--ind-w', `${active?.offsetWidth ?? 0}px`);
      nav.classList.toggle('has-active', Boolean(active));
    };
    measure();
    void document.fonts?.ready.then(measure);
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [location.pathname]);

  const isActive = (href: string) =>
    href === '/' ? location.pathname === '/' : location.pathname.startsWith(href);

  return (
    <div className="shell">
      <div className="atmosphere" aria-hidden="true"><i /><i /><i /></div>

      <header className="dock">
        <Link to="/" className="dock-brand" aria-label="RunRail, home">
          <RunRailMark size={22} title="" />
          <b>RunRail</b>
        </Link>

        <nav className="dock-nav" ref={navRef} aria-label="Main">
          <span className="dock-ind" aria-hidden="true" />
          {PRIMARY.map(({ href, label }) => (
            <Link key={href} to={href} className={clsx('dock-link', isActive(href) && 'active')}
                  aria-current={isActive(href) ? 'page' : undefined}>{label}</Link>
          ))}
          <MoreMenu variant="dock" />
        </nav>

        <div className="dock-tools">
          <Signal health={health} />
          <button type="button" className="dock-search" onClick={() => setPaletteOpen(true)} aria-label="Open command palette">
            <Search size={14} /> <span>Search</span>
            <kbd>{navigator.platform?.includes('Mac') ? '⌘K' : 'Ctrl K'}</kbd>
          </button>
          <NotificationBell />
          <ThemeToggle />
          <Link to="/settings" className={clsx('dock-btn', isActive('/settings') && 'active')} aria-label="Settings" title="Settings">
            <Settings size={15} />
          </Link>
        </div>
      </header>

      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}

      <main key={location.pathname}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/environments" element={<Environments />} />
          <Route path="/services" element={<ServicesPage />} />
          <Route path="/services/:id" element={<ServiceDetail />} />
          <Route path="/workflows" element={<Workflows />} />
          <Route path="/workflows/:id" element={<WorkflowDetail />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          {/* Per-run report permalink: run ids are never reused, so this URL
              survives a workflow rename. */}
          <Route path="/runs/:id/report" element={<RunReportPage />} />
          {/* Pinnable per-workflow link; :workflow is an id or an exact name. */}
          <Route path="/reports/:workflow/latest" element={<LatestReport />} />
          <Route path="/logs" element={<LogSearch />} />
          <Route path="/artifacts" element={<Artifacts />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>

      {/* On a phone the same four places sit under the thumb. */}
      <nav className="tabbar" aria-label="Main">
        {PRIMARY.map(({ href, icon: Icon, label }) => (
          <Link key={href} to={href} className={clsx('tab', isActive(href) && 'active')}
                aria-current={isActive(href) ? 'page' : undefined}>
            <Icon size={18} /><span>{label}</span>
          </Link>
        ))}
        <MoreMenu variant="tab" />
      </nav>
    </div>
  );
}

/** The verdict, in the dock: the one thing visible from every page. */
function Signal({ health }: { health: 'online' | 'offline' | 'unknown' }) {
  const { mood, serviceCount, waitingRuns, failing } = usePulse();
  if (health === 'offline') {
    return <span className="signal is-offline" title="The RunRail API is not answering"><i />Offline</span>;
  }
  const state = { down: serviceCount.down, waiting: waitingRuns.length, failing: failing.length };
  const to = mood === 'down' ? '/services'
    : mood === 'waiting' ? (waitingRuns.length === 1 ? `/runs/${waitingRuns[0]}` : '/')
    : mood === 'failing' ? (failing.length === 1 ? `/runs/${failing[0].run.id}` : '/runs?status=failed')
    : '/';
  return (
    <Link to={to} className={`signal is-${mood}`} title={verdictLine(state)}>
      <i />{signalLabel(state)}
    </Link>
  );
}

function MoreMenu({ variant }: { variant: 'dock' | 'tab' }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); setOpen(false); } };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  // Settings has its own button in the dock; on a phone it lives here.
  const items = variant === 'dock' ? SECONDARY.filter(n => n.href !== '/settings') : SECONDARY;
  const current = items.find(n => location.pathname.startsWith(n.href));
  return (
    <div className={clsx('more', `more--${variant}`)} ref={ref}>
      <button type="button" className={clsx(variant === 'dock' ? 'dock-link' : 'tab', current && 'active')}
              aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(v => !v)}>
        {variant === 'tab'
          ? <><Ellipsis size={18} /><span>More</span></>
          : <>{current?.label ?? 'More'} <ChevronDown size={13} /></>}
      </button>
      {open && (
        <div className="more-menu" role="menu">
          {items.map(({ href, icon: Icon, label }) => (
            <Link key={href} to={href} role="menuitem"
                  className={clsx('more-item', location.pathname.startsWith(href) && 'active')}>
              <Icon size={15} />{label}
            </Link>
          ))}
          <span className="more-rule" />
          <Link to="/wallboard" role="menuitem" className="more-item"><MonitorPlay size={15} />Wallboard<small>TV mode</small></Link>
        </div>
      )}
    </div>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme ?? 'dark');
  const next = theme === 'light' ? 'dark' : 'light';
  const apply = () => {
    document.documentElement.dataset.theme = next;
    localStorage.setItem('runrail-theme', next);
    setTheme(next);
  };
  return (
    <button type="button" className="dock-btn theme-toggle" onClick={apply}
            aria-label={`Switch to the ${next} theme`} title={`Switch to the ${next} theme`}>
      {theme === 'light' ? <Moon size={15} /> : <Sun size={15} />}
    </button>
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
  // Entrance stagger plays once per open; clearing the query must not replay it.
  const [fresh, setFresh] = useState(true);
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
            onChange={e => { setQuery(e.target.value); if (e.target.value) setFresh(false); }}
            onKeyDown={onKey}
            aria-label="Command search"
          />
          <kbd className="cmdk-esc">esc</kbd>
        </div>
        <div className={`cmdk-list${fresh ? ' cmdk-fresh' : ''}`} ref={listRef}>
          {shown.length === 0 && <div className="cmdk-empty">No matches for “{query}”</div>}
          {shown.map((c, i) => (
            <div key={c.id}>
              {c.section !== shown[i - 1]?.section && <div className="cmdk-section">{c.section}</div>}
              <button
                className={`cmdk-item${i === selected ? ' selected' : ''}`}
                title={c.label}
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
          <span className="cmdk-foot-tip"><kbd>/</kbd> opens this · <kbd>g</kbd> then <kbd>n</kbd> <kbd>w</kbd> <kbd>r</kbd> <kbd>s</kbd> jumps to a place</span>
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
      // Only a *running* run has a worker attached; the other two settle inside
      // the request, and the open gate is cancelled with them.
      // A running task is killed by the worker's next poll, within about a second.
      toast(run.status === 'running'
        ? 'Cancelling — the running task is being stopped'
        : run.status === 'waiting_approval'
        ? 'Run cancelled — the open approval was withdrawn'
        : 'Run cancelled', 'info');
      onDone?.();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not cancel run', 'error');
    } finally { setBusy(false); }
  };
  return (
    <Button variant="ghost" size={size} disabled={busy} onClick={cancel} className="cancel-run-btn" title="Cancel this run">
      <Ban size={12} /> <span className="cancel-run-label">Cancel</span>
    </Button>
  );
}

/* ─── Shared run list ─────────────────────────────────────
   Runs as a ledger rather than a table: the outcome first, then which
   workflow, then when and how long. The bar beside each duration is drawn
   against the longest run in the list, so the slow one stands out without
   anyone comparing numbers. `byDay` rules the list off by calendar day. */
function RunTable({ runs, flows, onChanged, byDay = false }: {
  runs: Run[]; flows: Workflow[]; onChanged?: () => void; byDay?: boolean;
}) {
  const now = useNow(runs.some(r => r.status === 'running'));
  const navTo = useNavigate();
  const peek = usePeek();
  // One query for the whole list; a broken summary must not break the list.
  const { entryFor } = useRunNotesSummary();
  const seconds = (r: Run) => r.duration_seconds
    ?? (r.status === 'running' && r.started_at ? (now - new Date(r.started_at).getTime()) / 1000 : 0);
  const longest = Math.max(1, ...runs.map(seconds));
  const today = localDateKey(new Date());
  let lastDay = '';
  return (
    <div className={clsx('run-list', byDay && 'run-list--days')}>
      {runs.map((r, index) => {
        const flowName = flows.find(w => w.id === r.workflow_id)?.name ?? `Workflow ${r.workflow_id}`;
        const at = r.started_at || r.created_at;
        const day = localDateKey(new Date(at));
        const newDay = byDay && day !== lastDay;
        lastDay = day;
        // The row opens the run beside the list; its id and its arrow go to
        // the run's own page.
        const open = (e: React.MouseEvent) => {
          if ((e.target as HTMLElement).closest('a, button')) return;
          peek.open(r.id);
        };
        return (
          <Fragment key={r.id}>
            {newDay && (
              <div className="run-day">
                {day === today ? 'Today' : new Date(at).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
              </div>
            )}
            <div className={`run-row is-${r.status}`} onClick={open} style={{ '--i': Math.min(index, 16) } as CSSProperties}>
              <StatusBadge value={r.status} />
              <div className="run-main">
                <Link to={`/workflows/${r.workflow_id}`} className="run-flow-name" title={flowName}>{flowName}</Link>
                <span className="run-meta">
                  <Link className="run-id" to={`/runs/${r.id}`}
                        style={{ viewTransitionName: `run-${r.id}` } as React.CSSProperties}
                        onClick={e => { e.preventDefault(); navigateWithTransition(navTo, `/runs/${r.id}`); }}>
                    #{r.id}
                  </Link>
                  <span className="trigger-badge">{triggerLabel(r.trigger_type)}</span>
                  <RunNotesIndicator entry={entryFor(r.id)} />
                </span>
              </div>
              <span className="run-when" title={formatDate(at)}>
                {byDay ? new Date(at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : timeAgo(at)}
              </span>
              <span className="run-dur">
                {seconds(r) > 0 && <i aria-hidden="true"><b style={{ width: `${Math.max(4, (seconds(r) / longest) * 100)}%` }} /></i>}
                {liveDuration(r, now)}
              </span>
              <span className="run-act">
                {LIVE(r.status) && <CancelRunButton run={r} onDone={onChanged} />}
                <Link className="row-arrow" to={`/runs/${r.id}`} aria-label={`Open run ${r.id}`}><ChevronRight size={15} /></Link>
              </span>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}

/* ─── Next up ──────────────────────────────────────────────
   What the schedule does next, as a departure board: the nearest fire counts
   down by the second, the ones after it by the minute. */
function Departures({ flows }: { flows: Workflow[] }) {
  const now = useNow(true, 1000);
  const minute = Math.floor(now / 60_000);
  const upcoming = useMemo(() => {
    const owed = upcomingFires(flows, minute * 60_000, 7 * DAY, 4);
    const items: { flow: Workflow; at: number }[] = [];
    for (const flow of flows) for (const at of owed.get(flow.id) ?? []) items.push({ flow, at });
    return items.sort((a, b) => a.at - b.at);
  }, [flows, minute]);
  const due = upcoming.filter(item => item.at > now).slice(0, 5);
  const clock = (at: number) => new Date(at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const today = localDateKey(new Date(now));
  const [lead, ...rest] = due;
  return (
    <aside className="departures" aria-label="Next scheduled runs">
      <header><h2>Next up</h2><span>{viewerZone()}</span></header>
      {lead ? (
        <>
          <Link className="departure-lead" to={`/workflows/${lead.flow.id}`}>
            <span className="departure-count">{countdown(lead.at - now)}</span>
            <b title={lead.flow.name}>{lead.flow.name}</b>
            <span className="departure-meta">
              {clock(lead.at)} · {cronLabel(lead.flow.schedule_cron!, lead.flow.schedule_timezone)}
            </span>
          </Link>
          <ol>
            {rest.map(({ flow, at }) => (
              <li key={`${flow.id}-${at}`}>
                <Link to={`/workflows/${flow.id}`}>
                  <time>{clock(at)}</time>
                  <b title={flow.name}>{flow.name}</b>
                  <span>
                    {localDateKey(new Date(at)) !== today && `${new Date(at).toLocaleDateString(undefined, { weekday: 'short' })} · `}
                    {untilLabel(at - now)}
                  </span>
                </Link>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <p className="departures-empty">Nothing is scheduled. Give a workflow a schedule and its next run counts down here.</p>
      )}
    </aside>
  );
}

const CHART_DAYS = 14;

/** Round a chart ceiling up to 1, 2 or 5 × a power of ten, so the gridline
 *  labels are numbers a person would have picked. */
function niceCeil(value: number): number {
  if (value <= 5) return 5;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 5, 10]) if (value <= step * magnitude) return step * magnitude;
  return 10 * magnitude;
}

function ActivityChart() {
  // Aggregated server-side so the counts stay correct no matter how many runs
  // a day holds (a client-side bucket of a capped /runs fetch under-counts).
  const [stats, setStats] = useState<Record<string, DailyStat>>({});
  const [focus, setFocus] = useState<number | null>(null);
  useEffect(() => {
    api<DailyStat[]>(`/stats/daily?days=${CHART_DAYS}&tz=${encodeURIComponent(viewerZone())}`)
      .then(rows => setStats(Object.fromEntries(rows.map(r => [r.date, r])))).catch(() => {});
  }, []);
  // Local days, matching the buckets the server just counted. setDate rather
  // than setUTCDate, so "today" is the operator's today.
  const days = Array.from({ length: CHART_DAYS }, (_, i) => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - (CHART_DAYS - 1) + i);
    const key = localDateKey(d);
    const s = stats[key] ?? { success: 0, failed: 0, other: 0 };
    return { ...s, day: d, key, total: s.success + s.failed + s.other };
  });
  const ceiling = niceCeil(Math.max(...days.map(d => d.total), 1));
  const total = days.reduce((sum, d) => sum + d.total, 0);
  const finished = days.reduce((sum, d) => sum + d.success + d.failed, 0);
  const succeeded = days.reduce((sum, d) => sum + d.success, 0);
  const shown = focus != null ? days[focus] : null;
  return (
    <div className="panel activity-chart" style={{ marginBottom: 16 }}>
      <div className="panel-head">
        <div><h2>Activity</h2><p>Runs per day by outcome, last {CHART_DAYS} days</p></div>
        {/* One readout for the whole chart: the period's totals at rest, the
            day under the pointer while it is over a column. */}
        <p className="ac-readout" aria-live="off">
          {shown ? (
            <>
              <b>{shown.total.toLocaleString()}</b>
              <span>{shown.day.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}</span>
              {shown.failed > 0 && <em>{shown.failed} failed</em>}
            </>
          ) : (
            <>
              <b>{total.toLocaleString()}</b>
              <span>runs</span>
              {finished > 0 && <span>{Math.round((succeeded / finished) * 100)}% succeeded</span>}
            </>
          )}
        </p>
      </div>
      <div className="ac-plot" onMouseLeave={() => setFocus(null)}>
        <div className="ac-grid" aria-hidden="true">
          <span><i>{ceiling.toLocaleString()}</i></span>
          <span><i>{(ceiling / 2).toLocaleString()}</i></span>
          <span />
        </div>
        <div className="ac-cols">
          {days.map((d, i) => {
            const label = `${d.day.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })}: `
              + (d.total ? `${d.total} runs, ${d.failed} failed` : 'no runs');
            const body = (
              <>
                <span className="ac-track">
                  <span className="ac-stack" style={{ height: `${(d.total / ceiling) * 100}%`, animationDelay: `${i * 22}ms` }}>
                    {d.failed > 0 && <i className="failed" style={{ flexGrow: d.failed }} />}
                    {d.other > 0 && <i className="other" style={{ flexGrow: d.other }} />}
                    {d.success > 0 && <i className="success" style={{ flexGrow: d.success }} />}
                  </span>
                </span>
                <span className="ac-label">{d.day.toLocaleDateString(undefined, { weekday: 'narrow' })}</span>
              </>
            );
            const cls = clsx('ac-col', i === CHART_DAYS - 1 && 'is-today', focus === i && 'is-focus');
            // Only the days that lead somewhere are links — as on the heatmap.
            return d.total > 0
              ? <Link key={d.key} to={`/runs?day=${d.key}`} className={cls} aria-label={label}
                      onMouseEnter={() => setFocus(i)} onFocus={() => setFocus(i)} onBlur={() => setFocus(null)}>{body}</Link>
              : <div key={d.key} className={cls} aria-label={label} onMouseEnter={() => setFocus(i)}>{body}</div>;
          })}
        </div>
      </div>
    </div>
  );
}

/* ─── Dashboard instruments ────────────────────────────────
   The headline numbers as one strip rather than five cards: a row of gauges
   reads as a single instrument, and none of them has to fill a box. */

/** "1m 49s" with the digits at full size and the units stepped down beside
 *  them, so a duration scans as a number like its neighbours. */
function UnitValue({ text }: { text: string }) {
  if (!/\d/.test(text)) return <>{text}</>;
  return (
    <>
      {text.split(/(\d+(?:\.\d+)?|<)/).filter(part => part.trim()).map((part, i) =>
        /^[\d.<]+$/.test(part) ? <span key={i}>{part}</span> : <small key={i}>{part.trim()}</small>)}
    </>
  );
}

function Instrument({ label, value, note, tone = 'default', to, children }: {
  label: string; value: ReactNode; note?: ReactNode;
  tone?: 'default' | 'success' | 'danger' | 'running' | 'warning';
  to?: string; children?: ReactNode;
}) {
  const body = (
    <>
      <span className="instrument-label"><i aria-hidden="true" />{label}</span>
      <strong className="instrument-value">{typeof value === 'number' ? <CountUp value={value} /> : value}</strong>
      {children}
      {note && <span className="instrument-note">{note}</span>}
    </>
  );
  const cls = clsx('instrument', `instrument-${tone}`);
  return to ? <Link className={cls} to={to}>{body}</Link> : <div className={cls}>{body}</div>;
}

const METER_DOTS = 24;

/** A ratio as a rail of dots: the frozen-comet idiom, used as a gauge. */
function DotMeter({ ratio }: { ratio: number }) {
  const lit = Math.round(Math.min(1, Math.max(0, ratio)) * METER_DOTS);
  return (
    <span className="dot-meter" aria-hidden="true">
      {Array.from({ length: METER_DOTS }, (_, i) => <i key={i} className={i < lit ? 'on' : undefined} />)}
    </span>
  );
}

/** The hero's date line. Its own ticker, so a second passing re-renders one
 *  line and not the page. */
function NowClock() {
  const now = new Date(useNow(true, 1000));
  // The viewer's own clock format, with the seconds stepped back: they are
  // there to show the page is alive, not to be read.
  const parts = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' }).formatToParts(now);
  return (
    <p className="now-date">
      <span>{now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}</span>
      <time>
        {parts.map((part, i) =>
          part.type === 'second' || (part.type === 'literal' && parts[i + 1]?.type === 'second')
            ? <small key={i}>{part.value}</small> : part.value)}
      </time>
    </p>
  );
}

/** Workflows whose latest verdict is a failure, each with the two things a
 *  person does about it: read the run, or run it again. */
function FailingPanel({ failing, onRetried }: { failing: Failing<Workflow, Run>[]; onRetried: () => void }) {
  const navTo = useNavigate();
  const { toast } = useToast();
  const [busy, setBusy] = useState<number | null>(null);
  const retry = async (run: Run) => {
    setBusy(run.id);
    try {
      const next = await post<Run>(`/runs/${run.id}/retry`, {});
      onRetried();
      navTo(`/runs/${next.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not retry the run', 'error');
    } finally { setBusy(null); }
  };
  return (
    <section className="panel failing-panel">
      <div className="panel-head">
        <div><h2>Failing</h2><p>Workflows whose latest run failed</p></div>
      </div>
      {failing.slice(0, 6).map(({ flow, run }) => (
        <div key={flow.id} className="failing-row">
          <i className="lane-signal" aria-hidden="true" />
          <div>
            <Link to={`/workflows/${flow.id}`} title={flow.name}>{flow.name}</Link>
            <span>Failed {timeAgo(run.finished_at || run.started_at || run.created_at)} · <Link to={`/runs/${run.id}`}>run #{run.id}</Link></span>
          </div>
          <Button variant="ghost" size="sm" disabled={busy === run.id} onClick={() => retry(run)}>
            <RefreshCw size={12} /> Retry
          </Button>
        </div>
      ))}
      {failing.length > 6 && <Link className="panel-more" to="/runs?status=failed">{failing.length - 6} more <ArrowRight size={13} /></Link>}
    </section>
  );
}

/* ─── Now ─────────────────────────────────────────────────
   The home screen. It answers, in this order: is everything all right, what
   should I do about it, what is moving, and what happens next. */
function Dashboard() {
  const { runs, flows, summary, services, serviceCount, waitingRuns, failing, mood, complete, refresh } = usePulse();
  const navTo = useNavigate();
  const { toast } = useToast();
  const failingIds = useMemo(() => new Set(failing.map(f => f.flow.id)), [failing]);
  const peek = usePeek();
  // Where the timetable puts "now". The headline sits to its left and what
  // comes next to its right, so the whole first screen reads along one line.
  const [nowX, setNowX] = useState(0);

  if (runs === null || flows === null) {
    return (
      <div className="now-loading">
        <div className="skeleton-line" style={{ height: 14, width: 220 }} />
        <div className="skeleton-line" style={{ height: 58, width: 'min(560px, 80%)' }} />
        <div className="skeleton-line" style={{ height: 320, width: '100%' }} />
      </div>
    );
  }

  const empty = flows.length === 0;
  // `waiting` are runs parked on an approval gate: live, but blocked on a
  // person rather than making progress, so the strip says so.
  const parked = summary?.waiting ?? runs.filter(r => r.status === 'waiting_approval').length;
  const runningCount = summary?.running ?? runs.filter(r => r.status === 'running').length;
  const queuedCount = summary?.queued ?? runs.filter(r => r.status === 'queued').length;
  const liveCount = summary?.live ?? (runningCount + queuedCount + parked);
  const runs24h = summary?.runs_24h ?? 0;
  const succeeded24h = summary?.succeeded_24h ?? 0;
  const failed24h = summary?.failed_24h ?? 0;
  const done7d = summary?.done_7d ?? 0;
  const successRate = summary?.success_rate_7d ?? null;
  const avgDuration = summary?.avg_duration_24h ?? null;
  const activeFlows = flows.filter(w => w.enabled).length;

  const runNow = async (id: number) => {
    try {
      const run = await post<Run>(`/workflows/${id}/run`, { parameters: {} });
      refresh();
      // Started from the timetable: watch it beside the timetable, which is
      // about to draw it, instead of being taken away to its page.
      peek.open(run.id);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
    }
  };

  // Only the states that have something in them: "0 running · 0 queued · 2
  // waiting" spends two thirds of the line on nothing.
  const liveNote = [
    runningCount && `${runningCount} running`,
    queuedCount && `${queuedCount} queued`,
    parked && `${parked} waiting on you`,
  ].filter(Boolean).join(' · ') || 'Nothing running or queued';

  // One verdict, one colour, one next step — all read from the same order of
  // urgency, so the headline never points one way and the button another.
  const state = { down: serviceCount.down, waiting: waitingRuns.length, failing: failing.length };
  const reviewApprovals = () => {
    if (waitingRuns.length === 1) { navTo(`/runs/${waitingRuns[0]}`); return; }
    document.querySelector('.approval-inbox')?.scrollIntoView({ behavior: REDUCED_MOTION() ? 'auto' : 'smooth', block: 'center' });
  };
  const next = mood === 'down' ? { label: 'Open services', icon: <Server size={15} />, go: () => navTo('/services') }
    : mood === 'waiting' ? { label: waitingRuns.length === 1 ? 'Review the approval' : `Review ${waitingRuns.length} approvals`, icon: <ShieldAlert size={15} />, go: reviewApprovals }
    : mood === 'failing' ? (failing.length === 1
        ? { label: 'Open the failed run', icon: <AlertTriangle size={15} />, go: () => navTo(`/runs/${failing[0].run.id}`) }
        : { label: 'Review failures', icon: <AlertTriangle size={15} />, go: () => navTo('/runs?status=failed') })
    : { label: 'Run a workflow', icon: <Play size={15} />, go: openPalette };

  return (
    <div className="now" style={nowX ? { '--now-x': `${nowX}px` } as CSSProperties : undefined}>
      <div className={clsx('now-stage', !empty && 'has-beam')}>
      <section className="now-hero">
        <div className="now-lead">
          <NowClock />
          <h1 className="now-verdict">{empty ? 'Welcome to RunRail.' : verdictLine(state)}</h1>
          <p className="now-sub">
            {empty ? 'Schedules, retries, logs, and history for the scripts you already have.' : (
              <>
                {mood === 'failing' && (
                  <span className="now-detail">
                    {failing.slice(0, 2).map(({ flow }, i) => (
                      <Fragment key={flow.id}>{i > 0 && ', '}<Link to={`/workflows/${flow.id}`}>{flow.name}</Link></Fragment>
                    ))}
                    {failing.length > 2 && ` and ${failing.length - 2} more`}
                    {failing.length === 1 && ` last failed ${timeAgo(failing[0].run.finished_at || failing[0].run.created_at)}`}
                    .
                  </span>
                )}
                <span>
                  {activeFlows} workflow{activeFlows === 1 ? '' : 's'} on schedule or on call
                  <i aria-hidden="true">·</i>
                  {runs24h.toLocaleString()} run{runs24h === 1 ? '' : 's'} in the last 24 hours
                  {serviceCount.total > 0 && (
                    <>
                      <i aria-hidden="true">·</i>
                      <Link to="/services" className={serviceCount.down ? 'is-down' : undefined}>
                        {serviceCount.down
                          ? `${serviceCount.down} service${serviceCount.down === 1 ? '' : 's'} down`
                          : `${serviceCount.up} of ${serviceCount.total} service${serviceCount.total === 1 ? '' : 's'} up`}
                      </Link>
                    </>
                  )}
                </span>
              </>
            )}
          </p>
          {/* On a first run the welcome card below carries the next step. */}
          {!empty && (
            <div className="now-actions">
              <Button onClick={next.go}>{next.icon} {next.label}</Button>
              {mood !== 'calm' && <Button variant="ghost" onClick={openPalette}><Play size={14} /> Run a workflow</Button>}
              <Link className="text-link" to="/runs">Run history <ArrowRight size={13} /></Link>
              <Link className="text-link" to="/wallboard"><MonitorPlay size={13} /> Wallboard</Link>
            </div>
          )}
        </div>
        {!empty && <Departures flows={flows} />}
      </section>
      {!empty && <Board runs={runs} flows={flows} complete={complete} failing={failingIds} onRun={runNow} onNowX={setNowX} onOpenRun={peek.open} />}
      {/* The present, drawn once for the whole scene. */}
      {!empty && <span className="now-beam" aria-hidden="true" />}
      </div>

      {empty ? (
        <>
          {/* Someone who only runs services still sees them first. */}
          <ServicesPanel items={services.items} onChanged={services.load} />
          <QuickStart />
        </>
      ) : (
        <>

          <div className="instruments">
            <Instrument label="In flight" value={liveCount}
                        tone={parked ? 'warning' : liveCount ? 'running' : 'default'}
                        note={liveNote}>
              {runningCount > 0 && <LoadingBar size="sm" className="instrument-live" />}
            </Instrument>
            <Instrument label="Runs · 24h" value={runs24h} note={`${succeeded24h.toLocaleString()} succeeded`} />
            <Instrument label="Success · 7d"
                        value={successRate != null ? <>{successRate}<small>%</small></> : '—'}
                        tone={successRate == null ? 'default' : successRate < 80 ? 'warning' : 'success'}
                        note={`${done7d.toLocaleString()} completed runs`}>
              {successRate != null && <DotMeter ratio={successRate / 100} />}
            </Instrument>
            <Instrument label="Failures · 24h" value={failed24h}
                        tone={failed24h ? 'danger' : 'default'}
                        to={failed24h ? '/runs?status=failed' : undefined}
                        note={failed24h ? 'Open the failed runs' : 'None'} />
            <Instrument label="Typical run"
                        value={avgDuration != null ? <UnitValue text={formatDuration(avgDuration)} /> : '—'}
                        note="Average over 24 hours" />
          </div>

          <div className="now-grid">
            <div className="now-main">
              <section className="panel">
                <div className="panel-head">
                  <div><h2>Latest runs</h2><p>Across every workflow, newest first</p></div>
                  <Link className="panel-link" to="/runs">All runs <ArrowRight size={13} /></Link>
                </div>
                {runs.length > 0
                  ? <RunTable runs={runs.slice(0, 9)} flows={flows} onChanged={refresh} />
                  : <EmptyState icon={<History size={22} />} title="No runs yet" text="Trigger a workflow to see it here." />}
              </section>
              {/* The fortnight and the season, side by side. */}
              <div className="now-history">
                <ActivityChart />
                <section className="panel year-panel">
                  <div className="panel-head">
                    <div><h2>The long view</h2><p>Every day's runs, by outcome</p></div>
                  </div>
                  <div className="year-panel-body"><RunHeatmap selectable /></div>
                </section>
              </div>
            </div>
            <div className="now-side">
              {/* Self-fetching and self-hiding — first in the column so a
                  paused run is the first thing the column says. */}
              <ApprovalInbox />
              {failing.length > 0 && <FailingPanel failing={failing} onRetried={refresh} />}
              {/* Services are what should always be up, and the panel leads
                  with any that are not. */}
              <ServicesPanel items={services.items} onChanged={services.load} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ─── Webhook test ─────────────────────────────────────────
   Sends one message to the SAVED webhook and says what came back. It tests the
   stored URL on purpose: the field above may hold an unsaved edit, and a test
   that passed on text never submitted would be worse than none. */
function WebhookTest({ workflow }: { workflow: Workflow }) {
  const [state, setState] = useState<{ busy: boolean; result?: { ok: boolean; error: string | null; url_host: string | null } | string }>({ busy: false });
  const configured = Boolean(workflow.notify_webhook_url);
  const send = async () => {
    setState({ busy: true });
    try {
      const result = await post<{ ok: boolean; error: string | null; url_host: string | null }>(`/workflows/${workflow.id}/notify-test`, {});
      setState({ busy: false, result });
    } catch (error) {
      setState({ busy: false, result: error instanceof Error ? error.message : 'Could not send' });
    }
  };
  const r = state.result;
  return (
    <div className="webhook-test">
      <button type="button" className="btn btn-ghost btn-sm" disabled={state.busy} onClick={send}
              title={configured ? 'Send a test message to the saved webhook' : 'Uses the default webhook from RUNRAIL_NOTIFY_WEBHOOK_URL, if any'}>
        <Bell size={12} /> {state.busy ? 'Sending…' : 'Send a test message'}
      </button>
      {r && (typeof r === 'string'
        ? <small className="webhook-test-result is-bad">{r}</small>
        : r.ok
          ? <small className="webhook-test-result is-ok">Delivered to {r.url_host}.</small>
          : <small className="webhook-test-result is-bad">{r.url_host} answered: {r.error}</small>)}
      {!r && workflow.webhook_error && (
        <small className="webhook-test-result is-bad">Last delivery failed: {workflow.webhook_error}</small>
      )}
    </div>
  );
}

/* ─── Workflows quick list ────────────────────────────────
   The dashboard's launch pad. It used to show the first six workflows
   alphabetically and nothing else — with forty-odd workflows, only the ones
   whose names start with "b" were ever reachable from here. Now: the most
   recently active first, a filter once there are more than a handful, and a
   scrolling list whose fade says there is more below. */

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
const RUN_TABS: { key: string; label: string; match: (status: string) => boolean }[] = [
  { key: '',          label: 'All',       match: () => true },
  { key: 'live',      label: 'Live',      match: LIVE },
  { key: 'failed',    label: 'Failed',    match: s => s === 'failed' },
  { key: 'success',   label: 'Succeeded', match: s => s === 'success' },
  { key: 'cancelled', label: 'Cancelled', match: s => s === 'cancelled' },
];

function Runs() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [flows, setFlows] = useState<Workflow[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [status, setStatus] = useState('');
  const [workflowId, setWorkflowId] = useState('');
  const [trigger, setTrigger] = useState('');
  const [query, setQuery] = useState('');
  // Arrived from a heatmap cell. The day has to be a server-side filter, not
  // another client-side one: the page holds the newest 500 runs, and any day
  // older than those would filter down to nothing on a busy install.
  const [params, setParams] = useSearchParams();
  const day = params.get('day') ?? '';

  const runsPath = useMemo(() => {
    const query = new URLSearchParams({ limit: '500' });
    if (day) query.set('day', day);
    return `/runs?${query}`;
  }, [day]);

  const loadStats = () => {
    void api<Run[]>(runsPath).then(setRuns).catch(() => {});
    void api<Summary>('/stats/summary').then(setSummary).catch(() => {});
  };
  const load = () => {
    api<Run[]>(runsPath).then(setRuns).catch(() => setRuns([]));
    api<Workflow[]>('/workflows').then(setFlows).catch(() => {});
    api<Summary>('/stats/summary').then(setSummary).catch(() => {});
  };
  useEffect(() => { load(); }, [runsPath]);
  // A workflow named in the link preselects the existing dropdown.
  useEffect(() => {
    const linked = params.get('workflow');
    if (linked) setWorkflowId(linked);
    // And a status, for "Review failures" on the home screen.
    const linkedStatus = params.get('status');
    if (linkedStatus) setStatus(linkedStatus);
  }, [params]);
  useEffect(() => {
    const u1 = rrws.on('run_created', loadStats);
    const u2 = rrws.on('run_updated', loadStats);
    const u3 = rrws.on('task_run_updated', loadStats);
    return () => { u1(); u2(); u3(); };
  }, []);
  useLiveRefresh(Boolean(runs?.some(r => LIVE(r.status))), loadStats);

  const flowName = (id: number) => flows.find(w => w.id === id)?.name ?? `Workflow ${id}`;
  // A status the tabs do not name (a link to ?status=queued) still filters.
  const tab = RUN_TABS.find(t => t.key === status);
  const matches = tab ? tab.match : (s: string) => s === status;
  // Everything but the status, so each tab can say how many it would show.
  const scoped = useMemo(() => {
    if (!runs) return [];
    const q = query.trim().toLowerCase();
    return runs.filter(r =>
      (!workflowId || String(r.workflow_id) === workflowId)
      && (!trigger || r.trigger_type === trigger)
      && (!q || `#${r.id}`.includes(q) || String(r.id) === q || flowName(r.workflow_id).toLowerCase().includes(q))
    );
  }, [runs, workflowId, trigger, query, flows]);
  const shown = useMemo(() => scoped.filter(r => matches(r.status)), [scoped, status]);

  if (runs === null) {
    return <div><div style={{ height: 40, width: 160, marginBottom: 24 }} className="skeleton-line" /><SkeletonCard /><SkeletonCard /></div>;
  }

  const avgDuration = summary?.avg_duration_24h ?? null;
  const filtersActive = Boolean(status || workflowId || trigger || query || day);
  const facts = [
    summary && `${summary.live} live`,
    summary && `${summary.runs_24h.toLocaleString()} in the last 24 hours`,
    summary && (summary.failed_24h ? `${summary.failed_24h} failed` : 'none failed'),
    avgDuration != null && `typically ${formatDuration(avgDuration)}`,
  ].filter(Boolean).join(' · ');

  return (
    <>
      <PageHeader title="Runs" subtitle={facts || 'Every execution, newest first.'} />

      <div className="toolbar">
        <div className="tabs" role="tablist" aria-label="Status">
          {RUN_TABS.map(t => {
            const n = scoped.filter(r => t.match(r.status)).length;
            return (
              <button key={t.key} type="button" role="tab" aria-selected={status === t.key}
                      className={clsx('tab-pill', status === t.key && 'active', t.key && n > 0 && `is-${t.key}`)}
                      onClick={() => setStatus(t.key)}>
                {t.label}<small>{n}</small>
              </button>
            );
          })}
        </div>
        <div className="toolbar-fields">
          <div className="filterbar-search">
            <Search size={14} />
            <input placeholder="Workflow or run id" value={query} onChange={e => setQuery(e.target.value)} aria-label="Search runs" />
          </div>
          <select value={workflowId} onChange={e => setWorkflowId(e.target.value)} aria-label="Workflow filter">
            <option value="">All workflows</option>
            {flows.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
          </select>
          <select value={trigger} onChange={e => setTrigger(e.target.value)} aria-label="Trigger filter">
            <option value="">All triggers</option>
            {['manual', 'schedule', 'upstream', 'cli', 'backfill'].map(t => <option key={t} value={t}>{triggerLabel(t)}</option>)}
          </select>
        </div>
      </div>

      {(day || filtersActive) && (
        <div className="toolbar-note">
          {day && (
            <span className="pill">
              {new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, {
                weekday: 'long', month: 'long', day: 'numeric', timeZone: 'UTC' })}
              {' · '}
              <button className="edit-link"
                      onClick={() => setParams(previous => {
                        const next = new URLSearchParams(previous);
                        next.delete('day');
                        return next;
                      }, { replace: true })}>
                show all days
              </button>
            </span>
          )}
          <span className="filterbar-count">{shown.length} of {runs.length} loaded</span>
          <button className="edit-link" onClick={() => {
            setStatus(''); setWorkflowId(''); setTrigger(''); setQuery('');
            setParams({}, { replace: true });
          }}>
            Clear filters
          </button>
        </div>
      )}

      <section className="panel ledger">
        {shown.length > 0
          ? <RunTable runs={shown.slice(0, 100)} flows={flows} onChanged={load} byDay />
          : <EmptyState icon={<History size={22} />} title={filtersActive ? 'No runs match these filters' : 'No runs yet'} text={filtersActive ? 'Try widening your filters.' : 'Run a workflow to see its history here.'} />}
        {shown.length > 100 && (
          <p className="ledger-foot">Showing the first 100 matches. Narrow the filters to see older runs.</p>
        )}
      </section>

      {runs.length > 0 && <ActivityChart />}
    </>
  );
}

/** On a phone the console's task list is one scrolling row; this keeps the
 *  chosen task in view. It moves only the row, never the page, and only when
 *  the choice changes — it is a stable ref callback, so React calls it when
 *  the element it is attached to changes, not on every render. */
function centreInRow(el: HTMLElement | null) {
  const row = el?.parentElement;
  if (!el || !row || row.scrollWidth <= row.clientWidth) return;
  row.scrollLeft = el.offsetLeft - (row.clientWidth - el.offsetWidth) / 2;
}

/** This run among the workflow's other runs: a strip of them, oldest on the
 *  left, with this one marked. It answers "is this new?" before anyone asks —
 *  a failure after twenty successes reads differently from the fifth in a row
 *  — and steps to the run before or after without a trip to the list. */
function RunNeighbours({ run }: { run: Run }) {
  const [siblings, setSiblings] = useState<Run[]>([]);
  useEffect(() => {
    let stale = false;
    api<Run[]>(`/runs?workflow_id=${run.workflow_id}&limit=80`)
      .then(list => { if (!stale) setSiblings(list); }).catch(() => {});
    return () => { stale = true; };
  }, [run.workflow_id, run.id, run.status]);
  const at = siblings.findIndex(r => r.id === run.id);
  if (at < 0 || siblings.length < 2) return null;
  // Newest first on the wire. Show up to 8 newer and 19 older, drawn oldest first.
  const strip = siblings.slice(Math.max(0, at - 8), at + 20).reverse();
  const newer = siblings[at - 1];
  const older = siblings[at + 1];
  const say = (r: Run) => `#${r.id} · ${r.status.replace(/_/g, ' ')} · ${timeAgo(r.started_at || r.created_at)}`;
  return (
    <div className="run-neighbours">
      {older
        ? <Link className="run-step" to={`/runs/${older.id}`} title={`Older run — ${say(older)}`} aria-label={`Older run ${older.id}`}><ChevronRight size={14} style={{ transform: 'rotate(180deg)' }} /></Link>
        : <span className="run-step is-off" aria-hidden="true"><ChevronRight size={14} style={{ transform: 'rotate(180deg)' }} /></span>}
      <span className="flow-history">
        {strip.map(r => (
          <Link key={r.id} to={`/runs/${r.id}`} className={clsx('tick', `is-${r.status}`, r.id === run.id && 'is-here')}
                title={r.id === run.id ? `This run — ${say(r)}` : say(r)} aria-label={`Run ${r.id}, ${r.status.replace(/_/g, ' ')}`}
                aria-current={r.id === run.id ? 'true' : undefined} />
        ))}
      </span>
      {newer
        ? <Link className="run-step" to={`/runs/${newer.id}`} title={`Newer run — ${say(newer)}`} aria-label={`Newer run ${newer.id}`}><ChevronRight size={14} /></Link>
        : <span className="run-step is-off" aria-hidden="true"><ChevronRight size={14} /></span>}
      <small>{at === 0 ? 'the latest run' : `${at} newer`} · {siblings.length - at - 1}{siblings.length >= 80 ? '+' : ''} older</small>
    </div>
  );
}

/** The link above a detail page's title. Arrived from inside the app, it goes
 *  back to exactly where you were — the timetable, a filtered list, another
 *  run — rather than to the top of a list you may never have been on. Opened
 *  cold (a pasted link, a reload), it goes to the list it names. */
function BackLink({ to, label }: { to: string; label: string }) {
  const navTo = useNavigate();
  const here = useLocation();
  const cameFromApp = here.key !== 'default';
  return (
    <Link className="back-link" to={to}
          onClick={e => {
            if (!cameFromApp || e.metaKey || e.ctrlKey || e.shiftKey) return;
            e.preventDefault();
            navTo(-1);
          }}>
      <ArrowLeft size={14} /> {cameFromApp ? 'Back' : label}
    </Link>
  );
}

/** A run's verdict as a sentence: what happened, how long it took, how long
 *  ago. The run page leads with it. */
function runSentence(run: Run, now: number, task?: string): string {
  const took = liveDuration(run, now);
  const ended = timeAgo(run.finished_at || run.started_at || run.created_at, now);
  switch (run.status) {
    case 'success': return `Succeeded in ${took} · ${ended}`;
    case 'failed': return task ? `Failed at ${task} after ${took} · ${ended}` : `Failed after ${took} · ${ended}`;
    case 'running': return task ? `Running ${task} · ${took} so far` : `Running for ${took}`;
    case 'queued': return `Queued ${timeAgo(run.created_at, now)}`;
    case 'waiting_approval': return task ? `Waiting for your approval at ${task}` : 'Waiting for your approval';
    case 'cancelled': return `Cancelled ${ended}`;
    default: return run.status.replace(/_/g, ' ');
  }
}

/* ─── Why a queued run is waiting ─────────────────────────
   A run sitting at `queued` with nothing visibly happening is the one place
   this app can leave someone guessing, and the reason is never in the run's
   own row — it is in some OTHER run holding a slot or a named resource.

   worker/queue.claim_next_run refuses a queued run for exactly three reasons,
   and they are all derivable from lists the API already serves: its workflow
   is at max_concurrent_runs; a run holds its lock_resource in a mode that
   cannot share; or, for a shared run, an exclusive run is already queued for
   that resource and the starvation barrier holds newcomers behind it. Anything
   else and the run is simply next in line.

   Read-only, computed client-side, and only while the run is actually queued —
   which is normally seconds. The verdict names the blocking run and links
   straight to it, because "wait for something" is not an answer. */

type QueueHolder = { run: Run; flow: Workflow };

type QueueVerdict =
  | { kind: 'concurrency'; holders: Run[] }
  | { kind: 'lock-held'; resource: string; holders: QueueHolder[] }
  | { kind: 'lock-barrier'; resource: string; holders: QueueHolder[] }
  | { kind: 'ahead'; count: number }
  | { kind: 'clear' };

/** How deep each status list is read. Generous for a single-user tool, and a
 *  short read is never wrong in a dangerous direction: an unseen holder only
 *  costs the calmer "waiting for the worker" copy, never a false accusation. */
const RUN_LIST_CAP = 200;

function useQueueVerdict(run: Run | undefined, flow: Workflow | undefined): QueueVerdict | null {
  const queued = Boolean(run && run.status === 'queued');
  const [snapshot, setSnapshot] = useState<{ active: Run[]; queue: Run[] } | null>(null);
  const [flows, setFlows] = useState<Workflow[] | null>(null);

  // Lock configuration is not a per-tick fact; fetching it once keeps the poll
  // below to the three lists that actually move.
  useEffect(() => {
    if (!queued || flows) return;
    let alive = true;
    api<Workflow[]>('/workflows')
      .then(list => { if (alive) setFlows(list); })
      .catch(() => {});
    return () => { alive = false; };
  }, [queued, flows]);

  const stillQueued = useRef(queued);
  stillQueued.current = queued;
  const read = () => {
    Promise.all([
      api<Run[]>(`/runs?status=running&limit=${RUN_LIST_CAP}`),
      api<Run[]>(`/runs?status=waiting_approval&limit=${RUN_LIST_CAP}`),
      api<Run[]>(`/runs?status=queued&limit=${RUN_LIST_CAP}`),
    ])
      .then(([running, parked, queue]) => {
        // A response landing after the run started must not repopulate the
        // snapshot the effect below just cleared.
        if (stillQueued.current) setSnapshot({ active: [...running, ...parked], queue });
      })
      // A failure leaves the last snapshot in place; the generic waiting
      // copy is the fallback, never a wrong reason.
      .catch(() => {});
  };

  useEffect(() => {
    if (!queued) { setSnapshot(null); return; }
    read();
  }, [queued, run?.id]);
  // Slower than the run's own 2s poll: this answer changes when some OTHER
  // run finishes, not when this one does.
  useVisibleInterval(queued, read, 5000);

  return useMemo<QueueVerdict | null>(() => {
    if (!run || !flow || !snapshot || !flows) return null;
    const flowOf = new Map(flows.map(f => [f.id, f]));
    const pair = (list: Run[]) => list
      .filter(r => r.id !== run.id)
      .map(r => ({ run: r, flow: flowOf.get(r.workflow_id) }))
      .filter((h): h is QueueHolder => Boolean(h.flow));

    // 1. Its own workflow's budget, checked first because it is the limit the
    //    operator set on this very page.
    const mine = snapshot.active.filter(r => r.workflow_id === run.workflow_id);
    if (mine.length >= flow.max_concurrent_runs) return { kind: 'concurrency', holders: mine };

    // 2. The named resource. NULL locks nothing and is locked by nothing —
    //    the same rule SQL's NULL comparison gives the worker.
    const resource = flow.lock_resource;
    if (resource) {
      const exclusiveHere = flow.lock_mode === 'exclusive';
      const holders = pair(snapshot.active).filter(
        h => h.flow.lock_resource === resource
          && (exclusiveHere || h.flow.lock_mode === 'exclusive'));
      if (holders.length) return { kind: 'lock-held', resource, holders };
      if (!exclusiveHere) {
        const barrier = pair(snapshot.queue).filter(
          h => h.flow.lock_resource === resource && h.flow.lock_mode === 'exclusive');
        if (barrier.length) return { kind: 'lock-barrier', resource, holders: barrier };
      }
    }

    // 3. Not blocked, just not first. Workers claim the oldest queued run.
    const mark = new Date(run.created_at).getTime();
    const older = snapshot.queue.filter(
      r => r.id !== run.id && new Date(r.created_at).getTime() < mark).length;
    return older > 0 ? { kind: 'ahead', count: older } : { kind: 'clear' };
  }, [snapshot, flows, run, flow]);
}

function QueueHolderChips({ holders }: { holders: { run: Run; label: string; tail?: string }[] }) {
  const shown = holders.slice(0, 6);
  return (
    <div className="queue-why-holders">
      {shown.map(({ run, label, tail }) => (
        <Link key={run.id} to={`/runs/${run.id}`} className="queue-why-holder"
              title={`Started ${formatDate(run.started_at || run.created_at)}`}>
          <b>{label}</b><em>#{run.id}{tail ? ` · ${tail}` : ''}</em>
        </Link>
      ))}
      {holders.length > shown.length && (
        <span className="queue-why-holder">+{holders.length - shown.length} more</span>
      )}
    </div>
  );
}

function QueueWhy({ run, flow }: { run: Run; flow?: Workflow }) {
  const verdict = useQueueVerdict(run, flow);
  const kind = verdict?.kind;
  const blocked = kind === 'concurrency' || kind === 'lock-held' || kind === 'lock-barrier';
  const Glyph = kind === 'lock-held' || kind === 'lock-barrier' ? Lock
    : kind === 'concurrency' ? ChevronsRight
    : Clock;

  let headline: ReactNode = 'Waiting for the worker';
  let body: ReactNode = 'Task runs appear here the moment a worker claims this run.';
  let chips: { run: Run; label: string; tail?: string }[] | null = null;

  if (verdict?.kind === 'concurrency') {
    const limit = flow?.max_concurrent_runs ?? 1;
    headline = 'Its own workflow is already at capacity';
    body = (
      <>
        {flow?.name ?? 'This workflow'} allows {limit} active {limit === 1 ? 'run' : 'runs'} at a
        time, and {verdict.holders.length === 1 ? 'one is' : `${verdict.holders.length} are`} going
        now. This run starts the moment a slot frees up — nothing needs doing.
      </>
    );
    chips = verdict.holders.map(r => ({
      run: r, label: flow?.name ?? 'This workflow',
      tail: r.status === 'waiting_approval' ? 'awaiting approval' : 'running',
    }));
  } else if (verdict?.kind === 'lock-held') {
    const alone = flow?.lock_mode === 'exclusive';
    headline = <>Waiting for <code>{verdict.resource}</code></>;
    body = alone ? (
      <>
        This workflow takes <code>{verdict.resource}</code> alone, so it waits for every run
        currently holding it — including ones that would happily share with each other.
      </>
    ) : (
      <>
        {verdict.holders.length === 1 ? 'A run is holding' : `${verdict.holders.length} runs are holding`}{' '}
        <code>{verdict.resource}</code> alone. This run shares the resource, so it starts once
        that finishes.
      </>
    );
    chips = verdict.holders.map(h => ({
      run: h.run, label: h.flow.name,
      tail: h.flow.lock_mode === 'exclusive' ? 'runs alone' : 'shares',
    }));
  } else if (verdict?.kind === 'lock-barrier') {
    headline = 'Held back so a run-alone job gets its turn';
    body = (
      <>
        {verdict.holders.length === 1 ? 'A run' : `${verdict.holders.length} runs`} queued for{' '}
        <code>{verdict.resource}</code> {verdict.holders.length === 1 ? 'takes' : 'take'} it alone,
        and no new shared run may start ahead of {verdict.holders.length === 1 ? 'it' : 'them'}.
        That barrier is what stops a steady drip of small runs from starving the heavy one.
      </>
    );
    chips = verdict.holders.map(h => ({ run: h.run, label: h.flow.name, tail: 'queued, runs alone' }));
  } else if (verdict?.kind === 'ahead') {
    headline = `${verdict.count} older ${verdict.count === 1 ? 'run is' : 'runs are'} queued ahead of it`;
    body = 'Workers claim the oldest queued run first. Nothing is blocking this one — it is simply next in line.';
  } else if (verdict?.kind === 'clear') {
    body = 'Nothing is holding this run back: no lock, no concurrency limit, nothing older in the queue. A worker claims it on the next tick.';
  }

  return (
    <div className={`queue-why${blocked ? ' queue-why--blocked' : ''}`}>
      <span className="queue-why-glyph" aria-hidden="true"><Glyph size={17} /></span>
      <div className="queue-why-text">
        <span className="queue-why-eyebrow">Waiting to start</span>
        <h3>{headline}</h3>
        <p>{body}</p>
        {chips && chips.length > 0 && <QueueHolderChips holders={chips} />}
        {!blocked && <div className="queue-why-bar"><LoadingBar /></div>}
      </div>
    </div>
  );
}

/* ─── Run detail ──────────────────────────────────────── */
function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run>();
  const [flow, setFlow] = useState<Workflow>();
  const [flowTasks, setFlowTasks] = useState<Task[]>([]);
  const [shareOpen, setShareOpen] = useState(false);
  // Graph and Timeline answer the same question — the shape of the run — so
  // they share one panel instead of stacking two.
  const [shape, setShape] = useState<'graph' | 'timeline'>('graph');
  // The task the console shows when someone has chosen one; otherwise it
  // follows the run (see `focus`).
  const [pickedTask, setPickedTask] = useState<string | null>(null);
  const navTo = useNavigate();
  const location = useLocation();
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
  useEffect(() => { setRun(undefined); setPickedTask(null); refresh(); }, [id]);
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
  // One fetch for the page; every task card reads its own series out of it.
  const trends = useTaskDurations(run?.workflow_id);
  useLiveRefresh(Boolean(run && LIVE(run.status)), trends.reload, 15_000);

  // Log search deep-links to /runs/{id}#task-{name}; without this the link
  // lands on the run but never reaches the task.
  const hash = location.hash;
  useEffect(() => {
    if (!run || !hash.startsWith('#task-')) return;
    const anchor = document.getElementById(`task-${decodeURIComponent(hash.slice(6))}`);
    anchor?.scrollIntoView({ behavior: REDUCED_MOTION() ? 'auto' : 'smooth', block: 'center' });
  }, [Boolean(run), hash]);

  if (!run) return (
    <div>
      <div style={{ height: 28, width: 120, marginBottom: 24 }} className="skeleton-line" />
      <SkeletonCard /><SkeletonCard />
    </div>
  );

  const params = Object.entries(run.parameters_json ?? {});
  const hasGraph = flowTasks.length > 1;
  const hasTimeline = Boolean(run.task_runs?.some(t => t.started_at));
  const view: 'graph' | 'timeline' =
    hasTimeline && (shape === 'timeline' || !hasGraph) ? 'timeline' : 'graph';
  const hasNotebook = flowTasks.some(t => t.task_type === 'notebook');
  const openTaskAnchor = hash.startsWith('#task-') ? decodeURIComponent(hash.slice(6)) : null;
  // One row per task: every task the workflow has, in the order it runs them,
  // with this run's attempts where there are any. A task the run never
  // reached is still a row, so the list is the whole pipeline.
  const groups = groupAttempts(run.task_runs ?? []);
  const lastOf = (g: AttemptGroup<TaskRun>) => g.attempts[g.attempts.length - 1] ?? g.gates[g.gates.length - 1];
  const names = [...new Set([...topologicallyOrdered(flowTasks).map(task => task.name), ...groups.map(g => g.name)])];
  const rows = names.map(name => {
    const group = groups.find(g => g.name === name);
    return { name, group, last: group ? lastOf(group) : undefined, attempts: group?.attempts.length ?? 0 };
  });
  const longestTask = Math.max(1, ...rows.map(r => r.last?.duration_seconds ?? 0));
  // The task worth reading first: the one that failed, else the one in
  // flight or waiting on a person, else the last one that ran.
  const withStatus = (status: string) => rows.find(r => r.last?.status === status);
  const focus = withStatus('failed') ?? withStatus('running') ?? withStatus('awaiting_approval')
    ?? [...rows].reverse().find(r => r.attempts > 0) ?? rows[0];
  const shown = rows.find(r => r.name === (pickedTask ?? openTaskAnchor)) ?? focus;
  const shownName = shown?.name ?? null;
  return (
    <>
      <header className={`hero-head is-${run.status}`}>
        <BackLink to="/runs" label="Runs" />
        <p className="hero-eyebrow">
          Run #{run.id}<i>·</i>
          {/* A chained run names the run whose success started it. */}
          {run.triggered_by
            ? <>after <Link to={`/runs/${run.triggered_by.run_id}`}>{run.triggered_by.workflow_name ?? 'a workflow'} #{run.triggered_by.run_id}</Link></>
            : triggerLabel(run.trigger_type)}
          <i>·</i>{formatDate(run.created_at)}
        </p>
        <h1 style={{ viewTransitionName: `run-${run.id}` } as React.CSSProperties}>
          {flow ? <Link to={`/workflows/${flow.id}`}>{flow.name}</Link> : `Run #${run.id}`}
        </h1>
        {/* The run's own verdict, in a sentence: what happened, how long it
            took, how long ago. */}
        <p className="hero-status"><i aria-hidden="true" />{runSentence(run, now, focus?.last ? focus.name : undefined)}</p>
        <RunNeighbours run={run} />
        <div className="detail-actions">
          <CancelRunButton run={run} onDone={refresh} size="md" />
          {/* Renders nothing unless the run is failed or cancelled. Resume is
              the primary action there, so Retry steps down to ghost. */}
          <ResumeButton run={run} onResumed={refresh} />
          {!LIVE(run.status) && (
            <Button variant="ghost" onClick={retry}
                    title="Queue a NEW run (new run id) with this run's parameters">
              <RefreshCw size={13} /> {run.status === 'failed' ? 'Retry' : 'Run again'}
            </Button>
          )}
          {!LIVE(run.status) && (
            <Button variant="ghost" onClick={() => setShareOpen(true)}
                    title="Download this run as one self-contained HTML file">
              <Share2 size={13} /> Share
            </Button>
          )}
          {hasNotebook && !LIVE(run.status) && (
            <Link className="btn btn-ghost btn-md" to={`/runs/${run.id}/report`}
                  title="This run's notebook report on its own page — a stable link to paste">
              <BookOpen size={13} /> Report
            </Link>
          )}
        </div>
      </header>

      {/* Above everything: a run stopped on a person is the page's headline.
          Renders nothing when no gate is open, so it needs no status guard. */}
      <ApprovalGate run={run} tasks={flowTasks} onDecided={refresh} />

      {/* The run itself: its pipeline (or its timeline) on top, and under it
          one console. The drawing and the task list both choose what the
          console shows, and it opens on the task that matters — the one that
          failed, else the one running — so "what happened?" is answered
          without scrolling through every task's output to find it. */}
      <section className="panel run-panel">
        <div className="panel-head">
          <div>
            <h2>{view === 'graph' ? 'Pipeline' : 'Timeline'}</h2>
            <p>{view === 'graph'
              ? 'Lit by this run. Select a task to read its output.'
              : 'Overlapping bars ran in parallel; several bars on a line are retries.'}</p>
          </div>
          {hasGraph && hasTimeline && (
            <div className="segmented slim run-shape-pick" role="group" aria-label="Run shape view">
              {(['graph', 'timeline'] as const).map(mode => (
                <button key={mode} type="button" className={view === mode ? 'active' : ''}
                        aria-pressed={view === mode} onClick={() => setShape(mode)}>
                  {mode === 'graph' ? 'pipeline' : mode}
                </button>
              ))}
            </div>
          )}
        </div>
        {view === 'graph' && flowTasks.length > 0 ? (
          <Pipeline
            tasks={topologicallyOrdered(flowTasks).map(t => ({
              name: t.name, task_type: t.task_type, depends_on: t.depends_on_json || [],
              detail: t.command || t.script_path || t.notebook_path || t.sql_path,
              gate: Boolean(t.requires_approval),
            }))}
            statuses={Object.fromEntries(rows.filter(r => r.last).map(r => [r.name, r.last!.status]))}
            durations={Object.fromEntries(rows.filter(r => r.last).map(r => [r.name, r.last!.duration_seconds]))}
            selected={shownName}
            onSelect={setPickedTask}
          />
        ) : hasTimeline ? (
          <TaskTimeline taskRuns={run.task_runs ?? []} runStart={run.started_at || run.created_at} now={now} />
        ) : null}

        <div className="console">
          {rows.length > 0 && (
            <nav className="console-tasks" aria-label="Tasks in this run">
              {rows.map(row => {
                const seconds = row.last?.duration_seconds
                  ?? (row.last?.status === 'running' && row.last.started_at ? (now - new Date(row.last.started_at).getTime()) / 1000 : null);
                return (
                  <button key={row.name} type="button" aria-pressed={row.name === shownName}
                          ref={row.name === shownName ? centreInRow : undefined}
                          className={clsx('console-task', `is-${row.last?.status ?? 'idle'}`, row.name === shownName && 'is-selected')}
                          onClick={() => setPickedTask(row.name)}>
                    <i className="pipe-lamp" aria-hidden="true" />
                    <span className="console-task-name" title={row.name}>{row.name}</span>
                    {row.attempts > 1 && <small title={`${row.attempts} attempts`}>{row.attempts}×</small>}
                    <span className="console-task-dur">
                      {seconds != null ? formatDuration(seconds) : row.last ? row.last.status.replace(/_/g, ' ') : 'not reached'}
                    </span>
                    {/* Each task's share of the longest one: where the time went. */}
                    <span className="console-task-bar" aria-hidden="true">
                      <b style={{ width: `${seconds != null ? Math.max(3, (seconds / longestTask) * 100) : 0}%` }} />
                    </span>
                  </button>
                );
              })}
            </nav>
          )}
          <div className="console-out run-tasks">
            {shown?.group ? (
              <TaskRunCard key={shown.group.key} group={shown.group} index={rows.indexOf(shown) + 1} console
                           trend={trends.byTaskId.get((shown.group.attempts[0] ?? shown.group.gates[0]).task_id)} />
            ) : run.status === 'queued' ? (
              // Nothing has started, so the useful thing to say is why not.
              <QueueWhy run={run} flow={flow} />
            ) : shown ? (
              <p className="console-empty">
                <b>{shown.name}</b> has not started in this run
                {run.status === 'failed' || run.status === 'cancelled' ? ' — the run ended before reaching it.' : ' yet.'}
              </p>
            ) : (
              <EmptyState icon={<Clock size={22} />} title="No task output" text="This run produced no task executions." />
            )}
          </div>
        </div>
      </section>

      <div className="detail-cols">
      <div className="detail-main">
      {/* "What is different from last time?" — the first question about a
          failed run, so it opens there; any finished run can ask it. */}
      {!LIVE(run.status) && (
        <RunChanges key={run.id} runId={run.id} workflowId={run.workflow_id} failed={run.status === 'failed'} />
      )}

      {/* The conclusion, above the raw stdout it was drawn from. Renders
          nothing when the run produced no notebook. Keyed on status so a run
          that finishes while you watch picks up the notebook it just wrote —
          the panel deliberately does not re-probe on the 2s live tick. */}
      <ReportPanel key={run.status} runId={run.id} runStatus={run.status}
                   liveTaskRuns={(run.task_runs ?? []).filter(t => t.task_type === 'notebook')} />

      <RunNotes runId={run.id} initialNotes={run.notes} onChanged={refresh} />
      </div>

      {/* The facts, beside the story rather than above it. */}
      <aside className="detail-aside">
        <section className="panel facts">
          <h2>Details</h2>
          <dl>
            <div><dt>Status</dt><dd><StatusBadge value={run.status} /></dd></div>
            {flow && <div><dt>Workflow</dt><dd><Link to={`/workflows/${flow.id}`} title={flow.name}>{flow.name}</Link></dd></div>}
            <div><dt>Trigger</dt><dd>
              {run.triggered_by
                ? <Link to={`/runs/${run.triggered_by.run_id}`}>after #{run.triggered_by.run_id}</Link>
                : triggerLabel(run.trigger_type)}
            </dd></div>
            <div><dt>Duration</dt><dd>{liveDuration(run, now)}</dd></div>
            <div><dt>Tasks</dt><dd>{run.task_runs?.length || 0}</dd></div>
            {/* A run that queues behind a lock does so because of THIS, and the
                run page is where that question gets asked. */}
            {flow?.lock_resource && (
              <div><dt>Resource lock</dt><dd><LockBadge resource={flow.lock_resource} mode={flow.lock_mode} /></dd></div>
            )}
            {Boolean(run.resume_count) && <div><dt>Resumed</dt><dd>{run.resume_count}×</dd></div>}
            <div><dt>Created</dt><dd>{formatDate(run.created_at)}</dd></div>
            {run.started_at && <div><dt>Started</dt><dd>{formatDate(run.started_at)}</dd></div>}
            {run.finished_at && <div><dt>Finished</dt><dd>{formatDate(run.finished_at)}</dd></div>}
          </dl>
          {params.length > 0 && (
            <div className="facts-extra">
              <h3>Parameters</h3>
              <p>Values injected into task templates for this run</p>
              <div className="facts-chips">
                {params.map(([key, value]) => (
                  <span key={key} className="path-chip" style={{ margin: 0 }}>{`${key} = ${String(value)}`}</span>
                ))}
              </div>
            </div>
          )}
        </section>
      </aside>
      </div>

      {shareOpen && (
        <ShareRunModal runId={run.id} runStatus={run.status}
                       workflowName={flow?.name} onClose={() => setShareOpen(false)} />
      )}
    </>
  );
}

function TaskTimeline({ taskRuns, runStart, now }: { taskRuns: TaskRun[]; runStart: string; now: number }) {
  const origin = new Date(runStart).getTime();
  /* An approval gate is a task row that never executed: it has no started_at,
     only a created_at (when the run parked) and a finished_at (when someone
     decided). Treating it like a task put every gate at left: 0 — which is why
     a gated task showed a stray mark at the very start of the run — and left an
     unexplained gap where the waiting actually happened. It gets its own span
     instead, from parked to decided. */
  const GATE = new Set(['awaiting_approval', 'approved', 'rejected']);
  const isGate = (t: TaskRun) => GATE.has(t.status);
  const isLive = (t: TaskRun) => Boolean(t.started_at) && !t.finished_at && t.status === 'running';
  const isWaiting = (t: TaskRun) => t.status === 'awaiting_approval';

  const startMs = (t: TaskRun) =>
    t.started_at ? new Date(t.started_at).getTime()
    : isGate(t) && t.created_at ? new Date(t.created_at).getTime()
    : origin;
  const endMs = (t: TaskRun) =>
    t.finished_at ? new Date(t.finished_at).getTime()
    : isLive(t) || isWaiting(t) ? now
    : startMs(t);

  const placeable = taskRuns.filter(t => t.started_at || (isGate(t) && t.created_at));
  if (!placeable.length) return null;

  // A running task or an open gate has no duration yet: measure against the
  // wall clock so the bar grows every tick, and extend the axis to "now".
  const spanMs = (t: TaskRun) =>
    isGate(t) ? Math.max(0, endMs(t) - startMs(t))
    : t.duration_seconds != null ? t.duration_seconds * 1000
    : t.finished_at ? new Date(t.finished_at).getTime() - startMs(t)
    : isLive(t) ? Math.max(0, now - startMs(t))
    : 0;
  const totalMs = Math.max(
    ...placeable.map(t => Math.max(endMs(t) - origin, startMs(t) - origin + 1000)), 1000
  );
  // One lane per task; retry attempts render as separate bars on the same lane.
  const lanes = new Map<string, TaskRun[]>();
  for (const t of taskRuns) {
    const key = t.task_name ?? `#${t.task_id}`;
    lanes.set(key, [...(lanes.get(key) ?? []), t]);
  }
  const ticks = [0.25, 0.5, 0.75];
  // Bare on purpose: RunDetail owns the panel, because Graph and Timeline
  // share one.
  return (
    /* Scrolls sideways below its minimum width rather than squeezing: at phone
       width the track was ~180px, so bars shrank to slivers and the axis labels
       ran into each other. */
    <div className="tl-scroll">
    <div className="task-timeline">
        {[...lanes.entries()].map(([name, attempts], laneIndex) => {
          const executions = attempts.filter(t => !isGate(t) && t.started_at).sort(byAttemptOrder);
          const resumed = executions.some(t => (t.resume_index ?? 0) > 0);
          const labelled = executions.length > 1;
          return (
          <div key={name} className="tl-row">
            <span className="tl-name" title={name}>{name}</span>
            <div className="tl-track">
              {ticks.map(f => <span key={f} className="tl-tick" style={{ left: `${f * 100}%` }} />)}
              {/* The wait between one attempt ending and the next starting —
                  the retry delay, 60s by default. Drawn rather than left blank,
                  because a blank gap between two short bars read as the
                  chart having lost something. */}
              {executions.slice(1).map((next, i) => {
                const prev = executions[i];
                const from = (endMs(prev) - origin) / totalMs * 100;
                const to = (startMs(next) - origin) / totalMs * 100;
                if (to - from < 0.5) return null;
                const gap = formatDuration((startMs(next) - endMs(prev)) / 1000);
                return (
                  <span key={`wait-${next.id}`} className="tl-retry-wait"
                        style={{ left: `${from}%`, width: `${to - from}%` }}
                        title={`${name} · waited ${gap} before ${attemptLabel(next, resumed).toLowerCase()}`} />
                );
              })}
              {attempts.filter(t => t.started_at || (isGate(t) && t.created_at)).map(t => {
                const gate = isGate(t);
                const start = (startMs(t) - origin) / totalMs * 100;
                const width = Math.max(spanMs(t) / totalMs * 100, gate ? 0.8 : 1.2);
                const left = Math.min(Math.max(start, 0), 98);
                const waited = formatDuration(spanMs(t) / 1000);
                const tip = gate ? [
                  `${name} · approval gate · ${t.status.replace(/_/g, ' ')}`,
                  `parked +${formatDuration((startMs(t) - origin) / 1000)}`,
                  isWaiting(t) ? `waiting ${waited}` : `waited ${waited} for a decision`,
                ].filter(Boolean).join('\n') : [
                  `${name} · ${attemptLabel(t, resumed).toLowerCase()} · ${t.status}`,
                  `started +${formatDuration((startMs(t) - origin) / 1000)}`,
                  t.duration_seconds != null ? `took ${formatDuration(t.duration_seconds)}`
                    : isLive(t) ? `running ${formatDuration(spanMs(t) / 1000)}` : null,
                  t.exit_code != null ? `exit ${t.exit_code}` : null,
                ].filter(Boolean).join('\n');
                // A gate stops a few pixels short of where it ended, so the
                // task that ran after it reads as its own bar, not its tip.
                const span = `${Math.min(width, 100 - left)}%`;
                return (
                  <div key={t.id}
                       className={clsx('tl-bar', t.status, gate && 'tl-bar--gate',
                                       isWaiting(t) && 'tl-bar--gate-open')}
                       style={{ left: `${left}%`, width: gate ? `calc(${span} - 3px)` : span, animationDelay: `${Math.min(laneIndex * 30, 240)}ms` }} title={tip}>
                    {!gate && (t.status === 'running' ? <CometCanvas kind="fill" />
                      : (t.status === 'success' || t.status === 'failed') && <CometCanvas kind="still" />)}
                    {/* Every attempt is labelled once there is more than one:
                        marking only the retries left the first bar ambiguous. */}
                    {!gate && labelled && <span className="tl-attempt">{attemptTag(t, resumed)}</span>}
                  </div>
                );
              })}
            </div>
            <LaneDuration attempts={attempts} isGate={isGate} isWaiting={isWaiting} spanMs={spanMs} />
          </div>
          );
        })}
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

/** A lane's figure: the time its task actually ran, and — on its own line,
 *  in the gate's amber — the time it waited for a person. Summed together,
 *  a 3-second task read as "2m 38s". */
function LaneDuration({ attempts, isGate, isWaiting, spanMs }: {
  attempts: TaskRun[]; isGate: (t: TaskRun) => boolean; isWaiting: (t: TaskRun) => boolean;
  spanMs: (t: TaskRun) => number;
}) {
  const ran = attempts.filter(t => !isGate(t)).reduce((sum, t) => sum + spanMs(t) / 1000, 0);
  const gates = attempts.filter(isGate);
  const waited = gates.reduce((sum, t) => sum + spanMs(t) / 1000, 0);
  const open = gates.some(isWaiting);
  return (
    <span className="tl-dur">
      {ran > 0 ? formatDuration(ran) : open ? null : formatDuration(null)}
      {gates.length > 0 && (
        <small className="tl-dur-wait">{open ? 'waiting' : 'waited'} {formatDuration(waited)}</small>
      )}
    </span>
  );
}

/** Execution order for one lane's attempts: segment, then attempt. */
const byAttemptOrder = (a: TaskRun, b: TaskRun) =>
  (a.resume_index ?? 0) - (b.resume_index ?? 0) || a.attempt - b.attempt || a.id - b.id;

/** A TaskRun row that never produced logs: the approval gate rows, plus the
 *  statuses that were already excluded. Expanding one into an empty LogViewer
 *  is a promise the row cannot keep. */
const NO_LOGS = new Set([
  'skipped', 'cancelled', 'awaiting_approval', 'approved', 'rejected',
]);

function TaskRunCard({ group, index, trend, openByDefault, console: fixed = false }: {
  group: AttemptGroup<TaskRun>; index: number; trend?: TaskDurationSeries; openByDefault?: boolean;
  /** The run page's console shows one task at a time, always open. */
  console?: boolean;
}) {
  /* One card per task. A retried task used to get a card per attempt, each
     headed with the same name, which read as the task running twice; the
     attempts now live inside the card, switchable, defaulting to the last. */
  const runs = group.attempts.length ? group.attempts : group.gates;
  const final = runs[runs.length - 1];
  const [pickedId, setPickedId] = useState<number | null>(null);
  const task = runs.find(r => r.id === pickedId) ?? final;
  const collapsible = runs.some(r => !NO_LOGS.has(r.status));
  const [open, setOpen] = useState(
    collapsible && (openByDefault || index === 1 || final.status === 'failed' || final.status === 'running'));
  const many = runs.length > 1;

  const gate = group.gates[group.gates.length - 1];
  const gateNote = gate && (gate.approved_at || gate.approval_note)
    ? [gate.status === 'rejected' ? 'Rejected' : 'Approved',
       gate.approved_at ? formatDate(gate.approved_at) : null,
       gate.approval_note].filter(Boolean).join(' · ')
    : null;
  const meta = !group.attempts.length
    ? (gateNote ?? 'Waiting for approval')
    : (many ? `${attemptLabel(task, group.resumed)} of ${runs.length}` : `Attempt ${task.attempt}`)
      + (task.exit_code != null ? ` · exit ${task.exit_code}` : '')
      + (task.duration_seconds != null ? ` · ${formatDuration(task.duration_seconds)}` : '')
      + (task.rendered_command ? ` · ${task.rendered_command}` : '');

  return (
    <article className="run-task" id={`task-${group.name}`}>
      <button className={clsx('run-task-head', fixed && 'is-fixed')} onClick={() => !fixed && collapsible && setOpen(!open)}>
        <span className="task-order">{index}</span>
        <div>
          <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {final.task_type && <TaskTypeBadge type={final.task_type} />}
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={group.name}>{group.name}</span>
            {many && <span className="attempt-count">{runs.length} attempts</span>}
            <TrendSpark series={trend} taskName={final.task_name ?? undefined}
                        size="md" highlightTaskRunId={task.id} showLabel />
          </h3>
          <p title={meta}>{meta}</p>
        </div>
        {/* What the task ended as, whichever attempt is being looked at. */}
        <StatusBadge value={final.status} />
        {collapsible && !fixed && <b>{open ? '⌃' : '⌄'}</b>}
      </button>
      {gateNote && group.attempts.length > 0 && (
        <div className="run-task-gate">Approval · {gateNote}</div>
      )}
      {(open || fixed) && collapsible && (
        <div className="log-area">
          {many && (
            <div className="attempt-switch" role="group" aria-label={`${group.name} attempts`}>
              {runs.map(r => (
                <button key={r.id} type="button"
                        className={clsx('nb-attempt', `nb-attempt--${r.status}`, r === task && 'is-active')}
                        aria-pressed={r === task}
                        onClick={() => setPickedId(r.id)}>
                  {attemptLabel(r, group.resumed)}<i aria-hidden="true" />
                </button>
              ))}
            </div>
          )}
          {NO_LOGS.has(task.status) ? (
            <div className="attempt-empty">This attempt produced no output — it was {task.status.replace(/_/g, ' ')}.</div>
          ) : (
            <LogViewer key={task.id} taskRunId={task.id} taskStatus={task.status}
              initialTab={task.status === 'failed' ? 'stderr' : 'stdout'}
              errorMessage={task.error_message ?? undefined} />
          )}
        </div>
      )}
      {!collapsible && final.error_message && (
        <div style={{ padding: '0 20px 14px', fontSize: 12.5, color: 'var(--text-3)' }}>{final.error_message}</div>
      )}
      {fixed && !collapsible && !final.error_message && (
        <div className="attempt-empty">Nothing to read here — this task was {final.status.replace(/_/g, ' ')}.</div>
      )}
    </article>
  );
}

/* ─── Report pages ────────────────────────────────────────
   Two permalinks, both safe to paste somewhere that outlives this session:
   one pinned to a run id (never reused, so it survives a rename), one pinned
   to a workflow and always showing its newest successful notebook. */

function RunReportPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run>();
  const [flow, setFlow] = useState<Workflow>();
  useEffect(() => {
    api<Run>(`/runs/${id}`).then(setRun).catch(() => {});
  }, [id]);
  useEffect(() => {
    if (run) api<Workflow>(`/workflows/${run.workflow_id}`).then(setFlow).catch(() => {});
  }, [run?.workflow_id]);

  return (
    <>
      <div className="detail-head">
        <Link to={`/runs/${id}`}><ArrowLeft size={14} /> Run #{id}</Link>
      </div>
      <PageHeader
        eyebrow="OUTPUTS"
        title={flow ? `${flow.name} · report for #${id}` : `Report for run #${id}`}
        subtitle="The notebook this run executed, rendered. This URL is pinned to the run id, so it keeps meaning the same thing forever."
        action={run && <StatusBadge value={run.status} />}
      />
      {/* hideWhenEmpty=false: someone followed this link on purpose and is
          owed a reason when there is nothing to show. */}
      <ReportPanel runId={id!} runStatus={run?.status} hideWhenEmpty={false} defaultOpen />
    </>
  );
}

function LatestReport() {
  const { workflow } = useParams<{ workflow: string }>();
  const [meta, setMeta] = useState<LatestReportMeta | null>(null);
  return (
    <>
      <div className="detail-head">
        <Link to="/workflows"><ArrowLeft size={14} /> Workflows</Link>
      </div>
      <PageHeader
        eyebrow="OUTPUTS"
        title={meta ? `${meta.workflow_name} · latest` : 'Latest report'}
        subtitle="A stable link to the newest successful run that produced a notebook. Safe to pin in a wiki."
        action={meta && (
          <Link className="btn btn-ghost btn-md" to={`/workflows/${meta.workflow_id}`}>
            <GitBranch size={13} /> Workflow
          </Link>
        )}
      />
      <LatestReportPanel workflow={workflow!} onMeta={setMeta} />
    </>
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
        <section className="panel list list--projects">
          <div className="list-row list-row--head" aria-hidden="true">
            <span>Project</span><span>Folder</span><span>Used by</span><span />
          </div>
          {items.map((x, index) => {
            const used = (flows ?? []).filter(w => w.project_id === x.id).length;
            return (
              <article className="list-row" key={x.id} style={{ '--i': Math.min(index, 16) } as CSSProperties}>
                <div className="list-main">
                  <h3 title={x.name}>{x.name}</h3>
                  <p>{x.description || 'No description yet'}</p>
                </div>
                <code className="list-path" title={x.root_path}>{x.root_path}</code>
                <span className="list-meta">
                  {used ? `${used} workflow${used === 1 ? '' : 's'}` : <Link className="edit-link" to="/workflows">Build a workflow</Link>}
                </span>
                <div className="row-actions">
                  <button className="edit-link" onClick={() => setEditing(x)}>Edit</button>
                  <button className="delete-link" onClick={() => removeProject(x)}>Remove</button>
                </div>
              </article>
            );
          })}
        </section>
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
          <CancelButton />
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
          <CancelButton />
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
  useVisibleInterval(
    items.some(x => x.status === 'creating' || x.status === 'building'),
    () => void load(),
    1500,
  );

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
        <section className="panel list list--envs">
          <div className="list-row list-row--head" aria-hidden="true">
            <span /><span>Environment</span><span>Kind</span><span>Python</span><span>State</span><span />
          </div>
          {items.map((x, index) => {
            const building = x.status === 'creating' || x.status === 'building';
            const tone = building ? 'is-running' : x.last_error || x.status === 'failed' ? 'is-failing'
              : x.status === 'degraded' ? 'is-waiting' : x.status === 'ready' ? 'is-ok' : 'is-idle';
            return (
              <article className={clsx('list-row', tone)} key={x.id} style={{ '--i': Math.min(index, 16) } as CSSProperties}>
                <i className="lane-signal" aria-hidden="true" />
                <div className="list-main">
                  <h3 title={x.name}>{x.name}</h3>
                  {building
                    ? <div className="list-building">
                        <LoadingBar size="sm" />
                        <small>{x.status === 'building' ? 'Installing packages…' : 'Preparing environment…'}</small>
                      </div>
                    : <p>{x.description || (x.managed ? `${x.active_packages_json.length} active managed libraries` : x.executable) || 'External runtime'}</p>}
                  {x.last_error && <small className="list-error">{x.last_error}</small>}
                </div>
                <span className="list-meta">{x.managed ? 'Managed' : x.env_type}</span>
                <span className="list-meta list-mono">{x.python_version ?? '—'}</span>
                <span className="list-meta list-state">{x.status}</span>
                <div className="row-actions">
                  <button className="edit-link" disabled={building} onClick={() => setEditing(x)}>Edit</button>
                  <button className="delete-link" disabled={building} onClick={() => removeEnv(x)}>Remove</button>
                </div>
              </article>
            );
          })}
        </section>
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
          <CancelButton />
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
          <CancelButton />
          <Button type="submit">{env.managed ? 'Save & rebuild' : 'Save changes'}</Button>
        </div>
      </form>
    </Modal>
  );
}

/* ─── Workflows ───────────────────────────────────────── */
/** A countdown with its own ticker, so a second passing re-renders one cell
 *  and not the list around it. */
function Countdown({ at }: { at: number }) {
  const now = useNow(true, 1000);
  return <>{countdown(at - now)}</>;
}

const HISTORY_TICKS = 16;

type FlowFilter = 'all' | 'scheduled' | 'chained' | 'manual' | 'paused' | 'failing';

function Workflows() {
  const [items, setItems] = useState<Workflow[] | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<FlowFilter>('all');
  const navTo = useNavigate();
  const peek = usePeek();
  const { toast } = useToast();
  // Half a minute is fine for "which fire is next"; the countdowns tick on
  // their own.
  const tick = Math.floor(useNow(true, 30_000) / 30_000);
  // The same verdict the home screen and the dock give, not a second opinion
  // worked out from this page's own slice of history.
  const { failing: failingNow } = usePulse();
  const failing = useMemo(() => new Set(failingNow.map(f => f.flow.id)), [failingNow]);
  const load = () => {
    api<Workflow[]>('/workflows').then(setItems).catch(() => setItems(prev => prev ?? []));
    api<Run[]>('/runs?limit=500').then(setRuns).catch(() => {});
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {
    const u1 = rrws.on('run_created', load);
    const u2 = rrws.on('run_updated', load);
    return () => { u1(); u2(); };
  }, []);
  useLiveRefresh(runs.some(r => LIVE(r.status)), load, 5000);

  const runWorkflow = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation();
    try {
      const run = await post<Run>(`/workflows/${id}/run`, { parameters: {} });
      // Watch it beside the list rather than losing your place in it.
      load();
      peek.open(run.id);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
    }
  };

  const rows = useMemo(() => {
    const flows = items ?? [];
    const history = new Map<number, Run[]>();
    for (const run of runs) {
      const list = history.get(run.workflow_id);
      if (list) { if (list.length < HISTORY_TICKS) list.push(run); } else history.set(run.workflow_id, [run]);
    }
    const owed = upcomingFires(flows, Date.now(), 14 * DAY, 1);
    return flows.map(flow => {
      const recent = history.get(flow.id) ?? [];
      return {
        flow, recent,
        next: owed.get(flow.id)?.[0] ?? null,
        state: laneState(flow, recent.filter(r => LIVE(r.status)), failing.has(flow.id)),
      };
    }).sort((a, b) => {
      const urgent = (s: string) => (s === 'failing' ? 0 : s === 'waiting' ? 1 : 2);
      return urgent(a.state) - urgent(b.state)
        || a.flow.name.localeCompare(b.flow.name, undefined, { numeric: true });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, runs, tick, failing]);

  const FILTERS: { key: FlowFilter; label: string; match: (row: typeof rows[number]) => boolean }[] = [
    { key: 'all',       label: 'All',       match: () => true },
    { key: 'scheduled', label: 'Scheduled', match: r => Boolean(r.flow.schedule_cron) && r.flow.enabled },
    { key: 'chained',   label: 'Chained',   match: r => !r.flow.schedule_cron && Boolean(r.flow.after?.length) && r.flow.enabled },
    { key: 'manual',    label: 'On demand', match: r => !r.flow.schedule_cron && !r.flow.after?.length && r.flow.enabled },
    { key: 'paused',    label: 'Paused',    match: r => !r.flow.enabled || r.state === 'paused' },
    { key: 'failing',   label: 'Failing',   match: r => r.state === 'failing' },
  ];
  const names = new Map((items ?? []).map(w => [w.id, w.name]));
  const needle = query.trim().toLowerCase();
  const named = needle
    ? rows.filter(r => r.flow.name.toLowerCase().includes(needle) || (r.flow.description ?? '').toLowerCase().includes(needle))
    : rows;
  const shown = named.filter(FILTERS.find(f => f.key === filter)!.match);

  if (items === null) {
    return <div><div style={{ height: 40, width: 220, marginBottom: 24 }} className="skeleton-line" /><SkeletonCard /><SkeletonCard /></div>;
  }

  return (
    <>
      <PageHeader
        title="Workflows"
        subtitle={items.length
          ? `${items.length} workflow${items.length === 1 ? '' : 's'}. Each line shows how its recent runs went and when it runs next.`
          : 'Orchestrate scripts and notebooks with schedules and dependencies.'}
        action={<Button onClick={() => setOpen(true)}><Plus size={15} /> New workflow</Button>}
      />
      {items.length > 0 ? (
        <>
          <div className="toolbar">
            <div className="tabs" role="tablist" aria-label="Show">
              {FILTERS.map(f => {
                const n = named.filter(f.match).length;
                if (f.key !== 'all' && f.key !== filter && n === 0) return null;
                return (
                  <button key={f.key} type="button" role="tab" aria-selected={filter === f.key}
                          className={clsx('tab-pill', filter === f.key && 'active', f.key === 'failing' && n > 0 && 'is-failed')}
                          onClick={() => setFilter(f.key)}>
                    {f.label}<small>{n}</small>
                  </button>
                );
              })}
            </div>
            <div className="toolbar-fields">
              <div className="filterbar-search">
                <Search size={14} />
                <input placeholder="Find a workflow" value={query} onChange={e => setQuery(e.target.value)} aria-label="Find a workflow" />
              </div>
            </div>
          </div>

          <section className="panel flow-list">
            <div className="flow-row flow-row--head" aria-hidden="true">
              <span />
              <span>Workflow</span>
              <span className="flow-schedule">Schedule</span>
              <span className="flow-history">Recent runs</span>
              <span className="flow-last">Last run</span>
              <span className="flow-next">Next</span>
              <span />
            </div>
            {shown.map(({ flow: w, recent, next, state }, index) => {
              const last = recent[0];
              return (
                <article key={w.id} className={clsx('flow-row', `is-${state}`)} style={{ '--i': Math.min(index, 16) } as CSSProperties}
                         onClick={e => { if (!(e.target as HTMLElement).closest('a, button')) navigateWithTransition(navTo, `/workflows/${w.id}`); }}>
                  <i className="lane-signal" aria-hidden="true" />
                  <div className="flow-main">
                    <h3 title={w.name}>
                      <Link to={`/workflows/${w.id}`} style={{ viewTransitionName: `wf-${w.id}` } as React.CSSProperties}
                            onClick={e => { e.preventDefault(); navigateWithTransition(navTo, `/workflows/${w.id}`); }}>{w.name}</Link>
                      {/* Both render nothing for the unlocked, unmuted majority. */}
                      <LockBadge resource={w.lock_resource} mode={w.lock_mode} />
                      <SnoozeBadge workflow={w} className="workflow-mute"
                                   onChange={u => setItems(list => (list ?? []).map(x => x.id === u.id ? u : x))} />
                      {!w.enabled && <StatusBadge value="disabled" />}
                    </h3>
                    <p>{w.description || 'No description yet'}</p>
                  </div>
                  <span className="flow-schedule">
                    {startsLabel(w, names, 'On demand')}
                  </span>
                  <span className="flow-history">
                    {/* Oldest on the left, so the strip reads the way time does. */}
                    {Array.from({ length: HISTORY_TICKS - recent.length }, (_, i) => <i key={`e${i}`} className="tick" />)}
                    {[...recent].reverse().map(r => (
                      <Link key={r.id} to={`/runs/${r.id}`} className={`tick is-${r.status}`}
                            title={`#${r.id} · ${r.status.replace(/_/g, ' ')} · ${timeAgo(r.started_at || r.created_at)}`}
                            aria-label={`Run ${r.id}, ${r.status.replace(/_/g, ' ')}`} />
                    ))}
                  </span>
                  <span className="flow-last">
                    {last ? timeAgo(last.started_at || last.created_at) : 'Not yet'}
                  </span>
                  <span className="flow-next">{next ? <Countdown at={next} /> : '—'}</span>
                  <button type="button" className="flow-run" onClick={e => runWorkflow(e, w.id)}
                          title={`Run ${w.name} now`} aria-label={`Run ${w.name} now`}>
                    <Play size={12} /><span>Run</span>
                  </button>
                </article>
              );
            })}
            {shown.length === 0 && (
              <p className="flow-empty">No workflow matches{needle ? ` “${query.trim()}”` : ' this filter'}.</p>
            )}
          </section>
        </>
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
  const [workflows, setWorkflows] = useState<Workflow[] | null>(null);
  useEffect(() => {
    api<Workflow[]>('/workflows').then(setWorkflows).catch(() => setWorkflows([]));
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
        schedule_cron: f.get('cron') || null,
        schedule_timezone: f.get('schedule_timezone') || null, enabled: true,
        after: afterValue(f),
        max_concurrent_runs: Number(f.get('concurrency')),
        project_id: projectId ? Number(projectId) : null,
        default_environment_id: environmentId ? Number(environmentId) : null,
        notify_webhook_url: f.get('webhook') || null,
        auto_pause_failures: f.get('autopause') ? Number(f.get('autopause')) : null,
        ...watchdogValues(f),
        ...lockValues(f),
      });
      toast('Workflow created');
      done(w.id);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not create workflow', 'error');
    }
  }
  return (
    <Modal title="Create a workflow" subtitle="Start with the basics. Add tasks on the next screen." onClose={onClose} wide>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field"><span>Workflow name</span><input name="name" placeholder="Daily customer refresh" required autoFocus /></label>
        <label className="field"><span>Description <em>Optional</em></span><textarea name="description" placeholder="What does this workflow accomplish?" /></label>
        <ScheduleBuilder initialCron={null} initialTimezone={null} chain={{ workflows: workflows ?? [] }} />
        <div className="field-row">
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
        {/* Publishes missed_grace_minutes / sla_minutes through hidden inputs,
            read back by watchdogValues(f) above. */}
        <WatchdogFields />
        {/* Same idiom: publishes lock_resource / lock_mode through hidden
            inputs, read back by lockValues(f). */}
        <LockField />
        <div className="modal-actions">
          <CancelButton />
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
  const [workflows, setWorkflows] = useState<Workflow[] | null>(null);
  useEffect(() => {
    api<Workflow[]>('/workflows').then(setWorkflows).catch(() => setWorkflows([]));
    api<Project[]>('/projects').then(setProjects).catch(() => {});
    api<Env[]>('/environments').then(setEnvs).catch(() => {});
  }, []);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const runForm = runFormValue(f);
    if ('error' in runForm) { toast(runForm.error, 'error'); return; }
    try {
      await put(`/workflows/${w.id}`, {
        name: f.get('name'), description: f.get('description') || null,
        schedule_cron: f.get('cron') || null,
        schedule_timezone: f.get('schedule_timezone') || null, enabled: f.get('enabled') === 'on',
        // Always sent: a schedule and a chain are one or the other, and the
        // server refuses a schedule saved beside a chain it was not told to drop.
        after: afterValue(f),
        max_concurrent_runs: Number(f.get('concurrency')),
        project_id: projectId ? Number(projectId) : null,
        default_environment_id: environmentId ? Number(environmentId) : null,
        notify_webhook_url: f.get('webhook') || null,
        auto_pause_failures: f.get('autopause') ? Number(f.get('autopause')) : null,
        // Not optional: apply_update writes every key of WorkflowIn, so
        // omitting these would silently wipe a configured watchdog — or
        // release a resource lock this workflow depends on.
        ...watchdogValues(f),
        ...lockValues(f),
        ...runForm,
      });
      toast('Workflow saved');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not save workflow', 'error');
    }
  }
  return (
    <Modal title="Edit workflow" subtitle="Update this workflow's settings." onClose={onClose} wide>
      <form className="modal-body form-stack" onSubmit={submit}>
        <label className="field"><span>Workflow name</span><input name="name" defaultValue={w.name} required autoFocus /></label>
        <label className="field"><span>Description <em>Optional</em></span><textarea name="description" defaultValue={w.description || ''} placeholder="What does this workflow accomplish?" /></label>
        {/* Rendered at once, list or not: the hidden inputs carry the saved
            schedule and chain even if Save is pressed before the list loads. */}
        <ScheduleBuilder initialCron={w.schedule_cron || null} initialTimezone={w.schedule_timezone || null}
                         chain={{ workflows: workflows ?? [], selfId: w.id, initialAfter: w.after }} />
        <div className="field-row">
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
            <WebhookTest workflow={w} />
          </label>
          <label className="field compact"><span>Auto-pause after <em>Optional</em></span>
            <input name="autopause" type="number" min="1" defaultValue={w.auto_pause_failures ?? ''} placeholder="e.g. 5" />
            <small>Consecutive failures before pausing.</small>
          </label>
        </div>
        <WatchdogFields
          missedGraceMinutes={w.missed_run_grace_minutes}
          slaMinutes={w.sla_minutes}
          hasSchedule={Boolean(w.schedule_cron)}
        />
        {/* maxConcurrentRuns is the SAVED value, not the live box above — it
            only drives an advisory warning, so a stale read is harmless. */}
        <LockField resource={w.lock_resource} mode={w.lock_mode}
                   workflowId={w.id} maxConcurrentRuns={w.max_concurrent_runs} />
        <RunFormEditor initial={w.run_parameters_json} />
        <label className="field toggle-field">
          <span>Enabled</span>
          <label className="toggle"><input type="checkbox" name="enabled" defaultChecked={w.enabled} /><span /></label>
        </label>
        <div className="modal-actions">
          <CancelButton />
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
  const [chainInfo, setChainInfo] = useState<ChainInfo | null>(null);
  // The task whose details are open under the pipeline, and the latest run
  // in full — its task statuses are what light the drawing.
  const [pickedName, setPickedName] = useState<string | null>(null);
  const [latest, setLatest] = useState<Run | null>(null);
  const [gapsOpen, setGapsOpen] = useState(false);
  const peek = usePeek();
  const navTo = useNavigate();
  const { toast } = useToast();

  const loadChain = () => void api<ChainInfo>(`/workflows/${id}/chain`).then(setChainInfo).catch(() => {});
  const load = () => {
    loadChain();
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
    // A required value with no default: nothing to start until someone says it.
    if (needsAsking(w?.run_parameters_json).length) { setRunParamsOpen(true); return; }
    try {
      const run = await post<Run>(`/workflows/${id}/run`, { parameters: {} });
      navTo(`/runs/${run.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
    }
  };

  const trends = useTaskDurations(id);
  // One scan feeds two surfaces: the heatmap's per-day marks and the panel
  // below it. The page pays for it once.
  const gaps = useScheduleGaps(Number(id));
  const gapFeed = heatmapGapFeed(gaps.data);

  useEffect(() => { load(); }, [id]);
  useEffect(() => {
    const refreshRuns = () => void api<Run[]>(`/runs?workflow_id=${id}`).then(setRuns).catch(() => {});
    const u1 = rrws.on('run_created', e => { if (String(e.workflow_id) === id) { refreshRuns(); loadChain(); } });
    // Any run finishing may be an upstream's, which moves the chain strip.
    const u2 = rrws.on('run_updated', () => { refreshRuns(); loadChain(); });
    return () => { u1(); u2(); };
  }, [id]);
  useLiveRefresh(runs.some(r => LIVE(r.status)), () => void api<Run[]>(`/runs?workflow_id=${id}`).then(setRuns).catch(() => {}));
  // The newest run with its tasks, re-read whenever it changes and every two
  // seconds while it is in flight, so the pipeline moves as the run does.
  const newest = runs[0];
  const loadLatest = () => { if (newest) void api<Run>(`/runs/${newest.id}`).then(setLatest).catch(() => {}); };
  useEffect(loadLatest, [newest?.id, newest?.status]);
  useVisibleInterval(Boolean(newest && LIVE(newest.status)), loadLatest, 2000);
  // Durations only change when a run finishes — a much slower beat than the
  // 3s run poll above, and one whole query per tick.
  useLiveRefresh(runs.some(r => LIVE(r.status)), trends.reload, 15_000);
  // A scheduled run landing changes the answer, but the whole scan is a walk
  // of the crontab — the slowest beat on the page.
  useLiveRefresh(runs.some(r => LIVE(r.status)), gaps.reload, 60_000);

  if (!w) return (
    <div>
      <div style={{ height: 28, width: 120, marginBottom: 24 }} className="skeleton-line" />
      <SkeletonCard /><SkeletonCard />
    </div>
  );

  const orderedTasks = topologicallyOrdered(tasks);
  const projectName = projects.find(p => p.id === w.project_id)?.name ?? '—';
  const defaultEnvName = envs.find(e => e.id === w.default_environment_id)?.name ?? '—';
  const standing = flowStanding(w, runs, chainInfo);
  const head = runs[0];
  const picked = orderedTasks.find(t => t.name === pickedName) ?? null;
  // Later attempts overwrite earlier ones, so a retried task shows how it ended.
  const lit = { statuses: {} as Record<string, string>, durations: {} as Record<string, number | null | undefined> };
  if (latest && head && latest.id === head.id) {
    for (const row of [...(latest.task_runs ?? [])].sort((x, y) => x.attempt - y.attempt)) {
      if (!row.task_name) continue;
      lit.statuses[row.task_name] = row.status;
      lit.durations[row.task_name] = row.duration_seconds;
    }
  }
  // What starts it: a clock, other workflows, or a person.
  const starts: PipeEnd[] = w.schedule_cron
    ? [{ key: 'clock', kind: 'clock', label: cronLabel(w.schedule_cron, w.schedule_timezone),
         sub: standing.next ? <>next in <Countdown at={standing.next} /></> : 'paused',
         tone: standing.next ? undefined : 'waiting' }]
    : chainInfo?.after.length
    ? chainInfo.after.map(u => ({
        key: `after-${u.id}`, kind: 'flow' as const, label: u.name, to: `/workflows/${u.id}`,
        sub: !u.enabled ? 'switched off' : u.done ? 'succeeded since the last start'
          : u.last_status === 'running' ? 'running now' : u.last_status === 'failed' ? 'its last run failed' : 'not succeeded yet',
        tone: u.done ? 'success' as const : u.last_status === 'failed' ? 'failed' as const
          : u.last_status === 'running' ? 'running' as const : 'waiting' as const,
      }))
    : [{ key: 'hand', kind: 'hand', label: 'On demand', sub: 'starts when you run it' }];
  const ends: PipeEnd[] = (chainInfo?.then ?? []).map(next => ({
    key: `then-${next.id}`, kind: 'flow', label: next.name, sub: 'runs after this', to: `/workflows/${next.id}`,
  }));
  const missed = gaps.data?.totals.missed ?? 0;
  const gapLine = !gaps.data || !gaps.data.schedule_cron ? null
    : missed > 0
      ? { tone: 'warning', text: `${missed.toLocaleString()} scheduled run${missed === 1 ? '' : 's'} never happened in the window checked` }
      : { tone: 'calm', text: 'Every scheduled run in the window checked happened' };
  return (
    <>
      <header className={`hero-head is-${standing.tone}`}>
        <BackLink to="/workflows" label="Workflows" />
        <p className="hero-eyebrow">
          Workflow<i>·</i>{startsLabel(w, new Map((chainInfo?.after ?? []).map(u => [u.id, u.name])), 'On demand')}
          {w.project_id && <><i>·</i>{projectName}</>}
        </p>
        <h1 style={{ viewTransitionName: `wf-${w.id}` } as React.CSSProperties}>{w.name}</h1>
        {/* Where the workflow stands, in a sentence. */}
        <p className="hero-status"><i aria-hidden="true" />{standing.line}</p>
        {w.description && <p className="hero-desc">{w.description}</p>}
        {w.webhook_error && (
          // A failing webhook used to fail silently forever; this is the
          // one place the person who set it up will look.
          <p className="webhook-warn" title={`Last attempt ${w.webhook_error_at ? new Date(w.webhook_error_at).toLocaleString() : ''}`}>
            <AlertTriangle size={12} /> Alerts are not being delivered: {w.webhook_error}.
            <button type="button" className="text-link" onClick={() => setEditOpen(true)}>Check the webhook</button>
          </p>
        )}
        <div className="detail-actions">
          <Button onClick={runNow}><Play size={13} /> Run now</Button>
          <Button variant="ghost" onClick={() => setRunParamsOpen(true)}
                  title={w.run_parameters_json?.length ? 'Choose the values for this run' : 'Run with one-off parameters'}>
            <SlidersHorizontal size={13} /> Run with…
          </Button>
          <Button variant="ghost" onClick={() => setBackfillOpen(true)}><Calendar size={13} /> Backfill</Button>
          <span className="detail-actions-rule" aria-hidden="true" />
          <Button variant="ghost" onClick={() => setEditOpen(true)}><Pencil size={13} /> Edit</Button>
          <Button variant="ghost" onClick={() => setAddOpen(true)}><Plus size={13} /> Add task</Button>
          <SnoozeControl workflow={w} onChange={setW} />
          {tasks.some(t => t.task_type === 'notebook') && (
            <Link className="btn btn-ghost btn-md" to={`/reports/${w.id}/latest`}
                  title="A stable link to the newest report — safe to pin in a wiki">
              <BookOpen size={13} /> Latest report
            </Link>
          )}
          <Button variant="danger" onClick={deleteWorkflow} title="Delete this workflow"><Trash2 size={13} /></Button>
        </div>
      </header>

      {/* The workflow itself comes first: what starts it, the tasks and how
          they hand off, and what follows — lit by its latest run. History
          and settings are what you look up afterwards. */}
      <section className="panel pipeline-panel">
        <div className="panel-head">
          <div>
            <h2>Pipeline</h2>
            <p>
              {head
                ? <>Lit by the latest run, <button type="button" className="edit-link" onClick={() => peek.open(head.id)}>#{head.id}</button> — {runSentence(head, Date.now()).toLowerCase()}</>
                : 'Each task starts once everything it depends on has succeeded. Select one to see what it runs.'}
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => setAddOpen(true)}><Plus size={13} /> Add task</Button>
        </div>
        {orderedTasks.length > 0 ? (
          <Pipeline
            tasks={orderedTasks.map(t => ({
              name: t.name, task_type: t.task_type, depends_on: t.depends_on_json || [],
              detail: t.command || t.script_path || t.notebook_path || t.sql_path,
              gate: Boolean(t.requires_approval), retries: t.retries,
            }))}
            statuses={lit.statuses} durations={lit.durations}
            starts={starts} ends={ends}
            selected={picked?.name ?? null}
            onSelect={name => setPickedName(current => (current === name ? null : name))}
          />
        ) : (
          <EmptyState
            icon={<Zap size={22} />}
            title="No tasks yet"
            text="Add your first task to start building this workflow."
            action={<Button onClick={() => setAddOpen(true)}><Plus size={15} /> Add first task</Button>}
          />
        )}
        {picked && (
          <div className="task-inspector" key={picked.id}>
            <div className="task-inspector-head">
              <TaskTypeBadge type={picked.task_type} />
              <h3 title={picked.name}>{picked.name}</h3>
              <TrendSpark series={trends.byTaskId.get(picked.id)} taskName={picked.name} size="sm" showLabel />
              <div className="task-card-actions">
                <Button variant="ghost" size="sm" onClick={() => setEditingTask(picked)}><Pencil size={12} /> Edit task</Button>
                <button className="delete-link" onClick={() => removeTask(picked)}>Remove</button>
                <button type="button" className="peek-close task-inspector-close" onClick={() => setPickedName(null)} aria-label="Close"><X size={14} /></button>
              </div>
            </div>
            {(picked.command || picked.script_path || picked.notebook_path || picked.sql_path) && (
              <pre className="task-inspector-cmd">{picked.command || picked.script_path || picked.notebook_path || picked.sql_path}</pre>
            )}
            <div className="task-card-meta">
              {picked.requires_approval && (
                <span className="is-gate" title={picked.approval_prompt || undefined}>
                  <ShieldAlert size={11} />Parks the run for approval{picked.approval_prompt ? `: “${picked.approval_prompt}”` : ''}
                </span>
              )}
              {picked.depends_on_json.length > 0 && (
                <span><GitMerge size={11} />Depends on: {picked.depends_on_json.join(', ')}</span>
              )}
              {picked.parameters_json && Object.keys(picked.parameters_json).length > 0 && (
                <span><SlidersHorizontal size={11} />{Object.entries(picked.parameters_json).map(([k, v]) => `${k}=${String(v)}`).join(', ')}</span>
              )}
              {picked.retries > 0 && <span><RefreshCw size={11} />{picked.retries} {picked.retries === 1 ? 'retry' : 'retries'}</span>}
              {picked.timeout_seconds && <span><Clock size={11} />Timeout: {picked.timeout_seconds}s</span>}
              {picked.environment_id && <span><Cpu size={11} />{envs.find(e => e.id === picked.environment_id)?.name ?? 'Custom env'}</span>}
              {picked.task_type && <span><Terminal size={11} />{picked.task_type}</span>}
            </div>
          </div>
        )}
      </section>

      <div className="detail-cols">
      <div className="detail-main">
      {/* How it has been going: run by run, then day by day. */}
      <div className="detail-pair">
      {runs.length > 0 && <RunMiniHistory runs={runs} />}

      {/* The grid's missed-run marks stop at the scan's own floor (30 days)
          and its footnote says so — widening the scan to match a 6-month range
          would trip the fire cap on anything hourly and replace real marks
          with a "scan stopped early" banner. */}
      {runs.length > 0 && (
        <div className="panel" style={{ marginBottom: 20 }}>
          <div className="panel-head"><div><h2>Activity</h2>
            <p>{gapFeed ? 'Runs per day, and the fires that never ran' : 'Runs per day, this workflow only'}</p>
          </div></div>
          <RunHeatmap workflowId={Number(id)} selectable
                      gaps={gapFeed?.gaps} gapsSince={gapFeed?.gapsSince}
                      gapsComplete={gapFeed?.gapsComplete} />
        </div>
      )}
      </div>

      {/* Missed runs, folded to the one line that matters: most visits to
          this page are not about the schedule's history, and the full scan is
          a screen tall. It opens itself only when something was missed. */}
      {gapLine && (
        <details className={clsx('gaps-fold', `is-${gapLine.tone}`)} open={gapsOpen}
                 onToggle={e => setGapsOpen((e.currentTarget as HTMLDetailsElement).open)}>
          <summary>
            <i aria-hidden="true" />
            <span>{gapLine.text}</span>
            <small>{gapsOpen ? 'Hide' : 'Details'} <ChevronDown size={13} /></small>
          </summary>
          {gapsOpen && <ScheduleGapsPanel workflowId={Number(id)} state={gaps} chained={Boolean(w.after?.length)} />}
        </details>
      )}

      {runs.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <div><h2>Recent runs</h2><p>{runs.length} total executions</p></div>
            <Link className="panel-link" to="/runs">See all →</Link>
          </div>
          <RunTable runs={runs.slice(0, 8)} flows={[w]} onChanged={load} />
        </div>
      )}

      </div>

      {/* The facts, beside the story rather than above it. */}
      <aside className="detail-aside">
        <section className="panel facts">
          <h2>Details</h2>
          <dl>
            <div><dt>Status</dt><dd><StatusBadge value={w.enabled ? 'enabled' : 'disabled'} /></dd></div>
            {w.snooze_until && (
              <div><dt>Alerts</dt><dd><SnoozeBadge workflow={w} onChange={setW} onExpire={load} /></dd></div>
            )}
            {(() => {
              const starts = startsLabel(w, new Map((chainInfo?.after ?? []).map(u => [u.id, u.name])));
              return <div><dt>Starts</dt><dd title={w.schedule_timezone || starts}>{starts}</dd></div>;
            })()}
            {standing.next && <div><dt>Next run</dt><dd className="facts-mono"><Countdown at={standing.next} /></dd></div>}
            {gapLine && (
              <div><dt>Missed runs</dt><dd>
                {missed > 0
                  ? <button type="button" className="edit-link" onClick={() => { setGapsOpen(true); document.querySelector('.gaps-fold')?.scrollIntoView({ behavior: REDUCED_MOTION() ? 'auto' : 'smooth', block: 'center' }); }}>{missed.toLocaleString()}</button>
                  : 'None'}
              </dd></div>
            )}
            <div><dt>Tasks</dt><dd>{tasks.length}</dd></div>
            <div><dt>Max active runs</dt><dd>{w.max_concurrent_runs}</dd></div>
            {w.lock_resource && (
              <div><dt>Resource lock</dt><dd><LockBadge resource={w.lock_resource} mode={w.lock_mode} /></dd></div>
            )}
            <div><dt>Total runs</dt><dd>{runs.length}</dd></div>
            {w.project_id && <div><dt>Project</dt><dd title={projectName}>{projectName}</dd></div>}
            {w.default_environment_id && <div><dt>Default env</dt><dd title={defaultEnvName}>{defaultEnvName}</dd></div>}
          </dl>
        </section>
      </aside>
      </div>

      {addOpen && <TaskModal workflowId={id!} tasks={tasks} project={projects.find(p => p.id === w.project_id)} defaultEnvironment={envs.find(e => e.id === (w.default_environment_id || projects.find(p => p.id === w.project_id)?.default_environment_id))} onClose={() => setAddOpen(false)} done={() => { setAddOpen(false); load(); }} />}
      {editOpen && <EditWorkflowModal w={w} onClose={() => setEditOpen(false)} done={() => { setEditOpen(false); load(); }} />}
      {runParamsOpen && w && <RunParamsModal workflow={w} onClose={() => setRunParamsOpen(false)} />}
      {backfillOpen && w && <BackfillModal workflow={w} onClose={() => setBackfillOpen(false)} />}
      {editingTask && <EditTaskModal task={editingTask} tasks={tasks} project={projects.find(p => p.id === (editingTask.project_id || w.project_id))} onClose={() => setEditingTask(null)} done={() => { setEditingTask(null); load(); }} />}
    </>
  );
}

/** Where a workflow stands, as one sentence and one tone: the workflow page
 *  leads with it the way the run page leads with a run's verdict. `runs` is
 *  newest first. */
function flowStanding(w: Workflow, runs: Run[], chain?: ChainInfo | null): { tone: string; line: string; next: number | null } {
  const paused = !w.enabled || Boolean(w.snoozed && w.snooze_pauses_runs);
  const fire = w.schedule_cron && !paused ? nextCronOccurrence(w.schedule_cron, w.schedule_timezone, new Date()) : null;
  const next = fire ? fire.getTime() : null;
  // A chained workflow has no clock; what it is waiting on is its "next".
  const waitingOn = chain ? chainSummary(chain, w.enabled) : null;
  const soon = next ? ` · next run ${untilLabel(next - Date.now())}`
    : waitingOn ? ` · ${waitingOn.charAt(0).toLowerCase()}${waitingOn.slice(1).replace(/\.$/, '')}` : '';
  const settled = runs.find(r => r.status === 'success' || r.status === 'failed');
  const ago = (r: Run) => timeAgo(r.finished_at || r.started_at || r.created_at);
  if (runs.some(r => r.status === 'waiting_approval')) {
    return { tone: 'waiting_approval', line: 'A run is waiting for your approval', next };
  }
  const running = runs.find(r => r.status === 'running');
  if (running) return { tone: 'running', line: `Running now · started ${timeAgo(running.started_at || running.created_at)}`, next };
  if (settled?.status === 'failed') return { tone: 'failed', line: `Failing · the last run failed ${ago(settled)}${soon}`, next };
  if (paused) return { tone: 'cancelled', line: w.enabled ? 'Muted, with runs paused' : 'Paused · it will not run on its schedule', next };
  if (settled) return { tone: 'success', line: `On track · last run succeeded ${ago(settled)}${soon}`, next };
  return { tone: 'queued', line: `Has not run yet${soon}`, next };
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

/** The last runs as bars: colour is how each ended, height is how long it
 *  took, so a workflow that is getting slower shows it before it fails. */
function RunMiniHistory({ runs }: { runs: Run[] }) {
  const recent = [...runs].slice(0, 60).reverse();
  const stats = { success: runs.filter(r => r.status === 'success').length, failed: runs.filter(r => r.status === 'failed').length };
  const durs = runs.filter(r => r.duration_seconds != null);
  const avg = durs.length ? durs.reduce((a, r) => a + r.duration_seconds!, 0) / durs.length : 0;
  const longest = Math.max(1, ...recent.map(r => r.duration_seconds ?? 0));
  return (
    <div className="panel run-spark">
      <div className="panel-head">
        <div>
          <h2>Run history</h2>
          <p>{runs.length} runs · {stats.success} succeeded · {stats.failed} failed{avg ? ` · typically ${formatDuration(avg)}` : ''}</p>
        </div>
        <span className="run-spark-key">Height is duration · newest on the right</span>
      </div>
      <div className="mini-bars">
        {recent.map(r => (
          <Link key={r.id} to={`/runs/${r.id}`} className={`mini-bar is-${r.status}`}
                style={{ height: `${Math.max(10, ((r.duration_seconds ?? 0) / longest) * 100)}%` }}
                title={`#${r.id} · ${r.status.replace(/_/g, ' ')} · ${r.duration_seconds != null ? formatDuration(r.duration_seconds) : 'no duration yet'} · ${formatDate(r.created_at)}`}
                aria-label={`Run ${r.id}, ${r.status.replace(/_/g, ' ')}`} />
        ))}
      </div>
    </div>
  );
}

/** Extra values typed as JSON, for parameters no form declares. */
function parseExtra(raw: string): Record<string, unknown> | string {
  const text = raw.trim();
  if (!text) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : 'Extra parameters must be a JSON object';
  } catch { return 'Extra parameters must be valid JSON'; }
}

function RunParamsModal({ workflow, onClose }: { workflow: Workflow; onClose: () => void }) {
  const { toast } = useToast();
  const navTo = useNavigate();
  const form = workflow.run_parameters_json ?? [];
  const [values, setValues] = useState<FormValues>(() => initialValues(form, workflow.schedule_timezone));
  const [extraOpen, setExtraOpen] = useState(form.length === 0);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const extra = parseExtra(String(new FormData(e.currentTarget).get('parameters') || ''));
    if (typeof extra === 'string') { toast(extra, 'error'); return; }
    const collected = collect(form, values);
    if ('error' in collected) { toast(collected.error, 'error'); return; }
    setBusy(true);
    try {
      const run = await post<Run>(`/workflows/${workflow.id}/run`, { parameters: { ...extra, ...collected.parameters } });
      navTo(`/runs/${run.id}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not start workflow', 'error');
      setBusy(false);
    }
  }
  return (
    <Modal title={form.length ? `Run ${workflow.name}` : 'Run with parameters'}
           subtitle={form.length ? 'Choose the values for this run.' : 'Start a run with one-off values for this execution.'} onClose={onClose}>
      <form className="modal-body form-stack" onSubmit={submit}>
        {form.length > 0 && <RunFormFields form={form} values={values} onChange={setValues} zone={workflow.schedule_timezone} />}
        {form.length > 0 && !extraOpen ? (
          <button type="button" className="text-button runform-more" onClick={() => setExtraOpen(true)}>Add parameters the form does not list…</button>
        ) : (
          <label className="field">
            <span>{form.length ? 'More parameters' : 'Parameters'} <em>JSON</em></span>
            <textarea className="code-input" name="parameters" placeholder={'{"region": "ca", "ds": "2026-07-01"}'} autoFocus={form.length === 0} />
          </label>
        )}
        {form.length === 0 && (
          <div className="callout">
            Every task template can reference these values, e.g. <code>{'{{ region }}'}</code>.
            They override the built-ins (<code>ds</code>, <code>ts</code>) and merge under any per-task parameters.
            To get a form here instead, add a <b>run form</b> when editing the workflow.
          </div>
        )}
        <div className="modal-actions">
          <CancelButton />
          <Button type="submit" disabled={busy}>{busy ? 'Starting…' : 'Run workflow'}</Button>
        </div>
      </form>
    </Modal>
  );
}

function BackfillModal({ workflow, onClose }: { workflow: Workflow; onClose: () => void }) {
  const { toast } = useToast();
  const workflowId = workflow.id;
  // The date comes from the backfill; every other field starts empty, so a
  // relative default counts from each backfilled day, not from today.
  const form = (workflow.run_parameters_json ?? []).filter(p => p.name !== 'ds');
  const [values, setValues] = useState<FormValues>(() =>
    Object.fromEntries(form.map(p => [p.name, p.type === 'bool' ? p.default === true : ''])));
  const [busy, setBusy] = useState(false);
  const today = new Date().toISOString().slice(0, 10);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const from = String(f.get('from')), to = String(f.get('to'));
    const extra = parseExtra(String(f.get('parameters') || ''));
    if (typeof extra === 'string') { toast(extra, 'error'); return; }
    const collected = collect(form, values);
    if ('error' in collected) { toast(collected.error, 'error'); return; }
    const parameters = { ...extra, ...collected.parameters };
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
        {form.length > 0 && <RunFormFields form={form} values={values} onChange={setValues} zone={workflow.schedule_timezone} blankDefaults />}
        <label className="field"><span>{form.length ? 'More parameters' : 'Parameters'} <em>JSON, optional — added to every run</em></span>
          <textarea className="code-input" name="parameters" placeholder={'{"region": "ca"}'} />
        </label>
        <div className="callout">Dates are inclusive. Days that already have a backfill run are skipped automatically.</div>
        <div className="modal-actions">
          <CancelButton />
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
        // All three, explicitly: the server treats an omitted field as
        // unchanged, so sending only the active one would leave the old
        // path behind when the task type changes.
        script_path: type === 'python' ? path || null : null,
        notebook_path: type === 'notebook' ? path || null : null,
        sql_path: type === 'sql' ? path || null : null,
        cwd: cwd || null, depends_on_json: f.getAll('dependencies'),
        parameters_json: parameters,
        retries: Number(f.get('retries')), retry_delay_seconds: Number(f.get('delay')),
        timeout_seconds: timeoutSeconds(f),
        environment_id: f.get('environment_id') ? Number(f.get('environment_id')) : null,
        ...approvalValues(f),
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
                <label key={t.id}><input type="checkbox" name="dependencies" value={t.name} /><span title={t.name}>{t.name}</span></label>
              ))}
            </div>
          </fieldset>
        )}
        <div className="field-row">
          <label className="field"><span>Retries</span><input name="retries" type="number" min="0" defaultValue="0" /></label>
          <label className="field"><span>Retry delay (sec)</span><input name="delay" type="number" min="0" defaultValue="0" /></label>
        </div>
        <TimeoutField />
        <ApprovalField />
        <div className="modal-actions">
          <CancelButton />
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
        // All three, explicitly: the server treats an omitted field as
        // unchanged, so sending only the active one would leave the old
        // path behind when the task type changes.
        script_path: type === 'python' ? path || null : null,
        notebook_path: type === 'notebook' ? path || null : null,
        sql_path: type === 'sql' ? path || null : null,
        cwd: cwd || null, depends_on_json: f.getAll('dependencies'),
        parameters_json: parameters,
        retries: Number(f.get('retries')), retry_delay_seconds: Number(f.get('delay')),
        timeout_seconds: timeoutSeconds(f),
        project_id: task.project_id ?? null,
        environment_id: environmentId ? Number(environmentId) : null,
        ...approvalValues(f),
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
                <label key={t.id}><input type="checkbox" name="dependencies" value={t.name} defaultChecked={task.depends_on_json.includes(t.name)} /><span title={t.name}>{t.name}</span></label>
              ))}
            </div>
          </fieldset>
        )}
        <div className="field-row">
          <label className="field"><span>Retries</span><input name="retries" type="number" min="0" defaultValue={task.retries} /></label>
          <label className="field"><span>Retry delay (sec)</span><input name="delay" type="number" min="0" defaultValue={task.retry_delay_seconds} /></label>
        </div>
        <TimeoutField seconds={task.timeout_seconds} />
        <ApprovalField defaultOn={task.requires_approval ?? false}
                       defaultPrompt={task.approval_prompt ?? ''} />
        <div className="modal-actions">
          <CancelButton />
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

/** How many artifacts the page draws before asking for a narrower filter. */
const ARTIFACT_CAP = 200;

function Artifacts() {
  const [items, setItems] = useState<ArtifactItem[] | null>(null);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('');
  useEffect(() => { api<ArtifactItem[]>('/artifacts').then(setItems).catch(() => setItems([])); }, []);

  if (items === null) {
    return <div><div style={{ height: 40, width: 200, marginBottom: 24 }} className="skeleton-line" /><SkeletonCard /></div>;
  }

  const needle = query.trim().toLowerCase();
  const named = needle ? items.filter(x => x.name.toLowerCase().includes(needle) || String(x.workflow_run_id ?? '') === needle) : items;
  const kinds = [...new Set(items.map(x => x.artifact_type))].sort();
  const shown = kind ? named.filter(x => x.artifact_type === kind) : named;
  const bytes = items.reduce((sum, x) => sum + (x.size_bytes ?? 0), 0);
  const today = localDateKey(new Date());
  let lastDay = '';

  return (
    <>
      <PageHeader
        title="Artifacts"
        subtitle={items.length
          ? `${items.length.toLocaleString()} file${items.length === 1 ? '' : 's'} your runs have produced${bytes ? ` · ${formatBytes(bytes)} in all` : ''}.`
          : 'Browse notebooks, reports, and files produced by your workflows.'}
      />
      {items.length > 0 ? (
        <>
          <div className="toolbar">
            <div className="tabs" role="tablist" aria-label="Kind">
              <button type="button" role="tab" aria-selected={kind === ''} className={clsx('tab-pill', kind === '' && 'active')}
                      onClick={() => setKind('')}>All<small>{named.length}</small></button>
              {kinds.map(k => (
                <button key={k} type="button" role="tab" aria-selected={kind === k} className={clsx('tab-pill', kind === k && 'active')}
                        onClick={() => setKind(k)}>{k}<small>{named.filter(x => x.artifact_type === k).length}</small></button>
              ))}
            </div>
            <div className="toolbar-fields">
              <div className="filterbar-search">
                <Search size={14} />
                <input placeholder="File name or run id" value={query} onChange={e => setQuery(e.target.value)} aria-label="Find an artifact" />
              </div>
            </div>
          </div>
          <section className="panel list list--artifacts">
            {shown.slice(0, ARTIFACT_CAP).map((x, index) => {
              const day = localDateKey(new Date(x.created_at));
              const newDay = day !== lastDay;
              lastDay = day;
              return (
                <Fragment key={x.id}>
                  {newDay && (
                    <div className="run-day">
                      {day === today ? 'Today' : new Date(x.created_at).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
                    </div>
                  )}
                  <article className="list-row" style={{ '--i': Math.min(index, 16) } as CSSProperties}>
                    <span className="list-glyph" aria-hidden="true"><Package size={15} /></span>
                    <div className="list-main">
                      <h3 title={x.name}>{x.name}</h3>
                      <p>
                        {x.artifact_type}
                        {x.workflow_run_id && <> · from <Link className="edit-link" to={`/runs/${x.workflow_run_id}`}>run #{x.workflow_run_id}</Link></>}
                      </p>
                    </div>
                    <span className="list-meta list-mono">{x.size_bytes ? formatBytes(x.size_bytes) : '—'}</span>
                    <span className="list-meta" title={formatDate(x.created_at)}>
                      {new Date(x.created_at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}
                    </span>
                    <a className="btn btn-ghost btn-sm" href={`/api/artifacts/${x.id}/download`}>
                      <Download size={13} /> Download
                    </a>
                  </article>
                </Fragment>
              );
            })}
            {shown.length === 0 && <p className="flow-empty">No artifact matches{needle ? ` “${query.trim()}”` : ' this filter'}.</p>}
            {shown.length > ARTIFACT_CAP && (
              <p className="ledger-foot">Showing the newest {ARTIFACT_CAP} of {shown.length.toLocaleString()}. Search to narrow them down.</p>
            )}
          </section>
        </>
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

// `waiting` outranks `running`: a run nobody has decided on is not progress,
// it is a queue of one person's attention.
const WB_RANK: Record<string, number> = {
  failed: 0, waiting: 1, overdue: 2, running: 3, queued: 4, success: 5, cancelled: 6, never: 7,
};

function Wallboard() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [flows, setFlows] = useState<Workflow[]>([]);
  const names = useMemo(() => new Map(flows.map(w => [w.id, w.name])), [flows]);
  const [clock, setClock] = useState(() => new Date());
  const [blooms, setBlooms] = useState<Record<number, string>>({});
  const prevStatuses = useRef(new Map<number, string>());
  const gridRef = useRef<HTMLDivElement>(null);
  // Open gates by run: which task is waiting, and since when (the run's own
  // timestamps say when it started, not when it stopped to ask).
  const approvals = useOpenApprovals();
  const gates = new Map((approvals ?? []).map(a => [a.run_id, a]));

  const load = () => {
    api<Run[]>('/runs?limit=300').then(setRuns).catch(() => {});
    api<Workflow[]>('/workflows').then(setFlows).catch(() => {});
  };
  useEffect(() => {
    load();
    const tick = window.setInterval(() => setClock(new Date()), 1000);
    const u1 = rrws.on('run_updated', load);
    const u2 = rrws.on('run_created', load);
    return () => { window.clearInterval(tick); u1(); u2(); };
  }, []);
  useVisibleInterval(true, load, 5000);
  const now = useNow(runs.some(r => r.status === 'running'));
  const services = useServices({ supervisor: false, pollIdle: false });
  const servicesDown = serviceCounts(services.items).down;

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
    const waiting = live.some(r => r.workflow_id === w.id && r.status === 'waiting_approval');
    const next = w.enabled && w.schedule_cron ? nextCronOccurrence(w.schedule_cron, w.schedule_timezone, clock) : null;
    // Overdue: the schedule should have fired after the latest run, gave it a
    // 2-minute grace, and no newer run ever appeared — a silently dead scheduler.
    let overdue = false;
    if (w.enabled && w.schedule_cron && mine.length) {
      const expected = nextCronOccurrence(w.schedule_cron, w.schedule_timezone, new Date(mine[0].created_at));
      overdue = !!expected && expected.getTime() < clock.getTime() - 120_000
        && !mine.some(r => new Date(r.created_at) >= expected);
    }
    const status = waiting ? 'waiting'
      : running ? 'running'
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
  const waitingCount = tiles.filter(t => t.status === 'waiting').length;
  const runningCount = live.filter(r => r.status === 'running').length;
  const mood = failing || overdueCount || servicesDown ? 'attention'
    : waitingCount || runningCount ? 'active' : 'calm';
  const verdict = failing ? `${failing} failing`
    : overdueCount ? `${overdueCount} overdue`
    : servicesDown ? `${servicesDown} service${servicesDown === 1 ? '' : 's'} down`
    : waitingCount ? `${waitingCount} waiting for approval`
    : runningCount ? `${runningCount} running`
    : 'All systems nominal';
  const nextUp = tiles.filter(t => t.next).sort((a, b) => a.next!.getTime() - b.next!.getTime())[0];
  const many = (n: number, one: string, more: string) => (n === 1 ? one : `${n} ${more}`);
  // The headline is the same verdict the home screen and the sky give, so a
  // TV and a laptop in the same room never disagree. Overdue is the one thing
  // only this board works out, so it speaks when nothing else does.
  const pulse = usePulse();
  const pulseState = { down: pulse.serviceCount.down, waiting: pulse.waitingRuns.length, failing: pulse.failing.length };
  const wallSentence = pulse.mood === 'calm' && overdueCount
    ? `${many(overdueCount, 'A workflow is', 'workflows are')} overdue.`
    : verdictLine(pulseState);
  const failingIds = new Set(tiles.filter(t => t.status === 'failed').map(t => t.flow.id));
  const clockParts = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' }).formatToParts(clock);

  return (
    <div className={`wallboard wallboard--${mood}`}>
      <div className="atmosphere" aria-hidden="true"><i /><i /><i /></div>
      <header className="wallboard-head">
        <span className="wallboard-brand">
          <RunRailMark size={24} title="" />
          RunRail
        </span>
        <Link to="/" className="wallboard-exit" title="Back to the app" aria-label="Back to the app"><X size={16} /></Link>
      </header>

      {/* Read from across a room: the verdict, and the time it was true. */}
      <section className={`wall-hero wall-hero--${mood}`}>
        <div className="wall-lead">
          <p className="now-date">
            <span>{clock.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}</span>
          </p>
          <h1 className="now-verdict">{wallSentence}</h1>
          <p className="now-sub">
            <span className={`wallboard-health wallboard-health--${mood}`}>
              <span className="wallboard-health-dot" />{verdict}
            </span>
            {nextUp && (
              <span className="wallboard-next" title={`${nextUp.flow.name} · ${nextUp.next!.toLocaleString()}`}>
                Next: <span className="wallboard-next-name">{nextUp.flow.name}</span> in {formatEta(nextUp.next!.getTime() - clock.getTime())}
              </span>
            )}
          </p>
        </div>
        <span className="wallboard-clock">
          {clockParts.map((part, i) =>
            part.type === 'second' || part.type === 'dayPeriod' || (part.type === 'literal' && clockParts[i + 1]?.type !== 'minute')
              ? <small key={i}>{part.value}</small> : part.value)}
        </span>
      </section>

      <WallboardServices items={services.items ?? []} now={clock.getTime()} />

      {/* The same timetable as the home screen: what ran, what is running,
          what is due, on one clock. */}
      {flows.length > 0 && (
        <Board runs={runs} flows={flows} complete={runs.length < 300} failing={failingIds} max={10} />
      )}

      <section className="wb-section" aria-label="Workflows">
        <div className="wb-section-head">
          <span className="wb-section-title"><GitBranch size={15} /> Workflows</span>
          <em className="wb-section-count">{flows.length}</em>
          {runningCount > 0 && <span className="wb-section-note wb-section-note--live">{runningCount} running</span>}
          {waitingCount > 0 && <span className="wb-section-note wb-section-note--busy">{waitingCount} waiting for approval</span>}
          {failing > 0 && <span className="wb-section-note wb-section-note--bad">{failing} failing</span>}
          {overdueCount > 0 && <span className="wb-section-note wb-section-note--busy">{overdueCount} overdue</span>}
          <span className="wb-section-rule" aria-hidden="true" />
        </div>
        {live.length > 0 && (
          <div className="wallboard-strip">
            {live.map(r => {
              const expected = r.status === 'running' ? medianDuration(runs, r.workflow_id) : null;
              const elapsed = r.started_at ? (now - new Date(r.started_at).getTime()) / 1000 : 0;
              const pct = expected ? Math.min(100, elapsed / expected * 100) : null;
              const over = expected != null && elapsed > expected * 1.15;
              // 0 at/under median → 1 at 135% of median (fully amber)
              const overRatio = expected ? Math.min(1, Math.max(0, (elapsed / expected - 1) / 0.35)) : 0;
              const flowName = flows.find(w => w.id === r.workflow_id)?.name ?? `Workflow ${r.workflow_id}`;
              return (
                <Link key={r.id} to={`/runs/${r.id}`} className={`wallboard-live-card ${r.status}`}
                      title={r.status === 'waiting_approval' ? `${flowName} #${r.id} · review the approval` : `${flowName} #${r.id}`}>
                  <span className={`run-pulse ${r.status}`} />
                  <div className="wallboard-live-body">
                    <b title={flowName}>{flowName}</b>
                    <small>#{r.id} · {r.status === 'waiting_approval'
                      ? waitingLine(gates.get(r.id), now)
                      : `${r.status.replace(/_/g, ' ')} · ${liveDuration(r, now)}`}</small>
                    {pct != null && (
                      <div className="wb-progress" style={{ '--over-ratio': overRatio.toFixed(3) } as CSSProperties}>
                        <div className="wb-progress-fill" style={{ width: `${pct}%` }}>
                          <CometCanvas kind="fill" />
                        </div>
                      </div>
                    )}
                  </div>
                  {expected != null && r.status === 'running' && (
                    <span className={`wb-eta${over ? ' wb-eta--over' : ''}`}>
                      {over ? 'running long' : `~${formatEta(Math.max(0, (expected - elapsed) * 1000))} left`}
                    </span>
                  )}
                  {r.status === 'waiting_approval' && <span className="wb-eta wb-eta--review">Review</span>}
                  <ChevronRight className="wallboard-live-arrow" size={18} />
                </Link>
              );
            })}
          </div>
        )}

        <div className="wallboard-grid" ref={gridRef}>
          {tiles.map(({ flow: w, last, streak, streakStart, rate7d, next, status }) => (
            <Link key={w.id} to={`/workflows/${w.id}`} data-flip-id={String(w.id)}
                 className={`wallboard-tile wb-${status}${blooms[w.id] ? ` wb-bloom wb-bloom-${blooms[w.id]}` : ''}`}
                 onAnimationEnd={e => {
                   if (e.animationName === 'tile-bloom') setBlooms(({ [w.id]: _, ...rest }) => rest);
                 }}>
              <div className="wallboard-tile-top">
                <div className="wallboard-tile-name" title={w.name}>{w.name}</div>
                <ChevronRight className="wallboard-tile-arrow" size={20} />
              </div>
              <div className="wallboard-tile-status" key={status}>{status === 'never' ? 'no runs' : status}</div>
              {status === 'failed' && streak > 0 && (
                <div className="wallboard-tile-streak">
                  {streak} consecutive failure{streak === 1 ? '' : 's'}
                  {streakStart ? ` · red for ${formatEta(clock.getTime() - new Date(streakStart).getTime())}` : ''}
                </div>
              )}
              <div className="wallboard-tile-meta">
                {last ? `${timeAgo(last.finished_at || last.created_at)}` : '—'}
                {` · ${startsLabel(w, names, 'manual')}`}
                {next ? ` · next in ${formatEta(next.getTime() - clock.getTime())}` : ''}
                {status !== 'failed' && rate7d != null ? ` · ${rate7d}% · 7d` : ''}
                {!w.enabled ? ' · paused' : ''}
              </div>
              <WorkflowSparkline runs={runs.filter(r => r.workflow_id === w.id).slice(0, 16)} />
            </Link>
          ))}
          {flows.length === 0 && <div className="wallboard-empty">No workflows yet.</div>}
        </div>

        {failures.length > 0 && (
          <div className="wallboard-failures">
            <span className="wallboard-failures-label"><AlertTriangle size={14} /> Failures · 24h</span>
            {failures.map(r => {
              const name = flows.find(w => w.id === r.workflow_id)?.name ?? String(r.workflow_id);
              return (
                <span key={r.id} className="wallboard-failure" title={`${name} #${r.id} · ${timeAgo(r.created_at)}`}>
                  {name} #{r.id} · {timeAgo(r.created_at)}
                </span>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

/* ─── App Entry ───────────────────────────────────────── */
export default function App() {
  const location = useLocation();
  // The wallboard stands outside the shell, but it shares the sky.
  if (location.pathname === '/wallboard') return <PulseProvider><Wallboard /></PulseProvider>;
  return <Shell />;
}
