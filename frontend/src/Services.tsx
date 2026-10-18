import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, CalendarClock, Lock, Pencil, Play, Power, RotateCcw, Server, Square,
  Trash2, Undo2, Users, Zap,
} from 'lucide-react';
import clsx from 'clsx';
import { ApiError, api, del, post, put } from './api';
import { rrws } from './ws';
import { useNow, useVisibleInterval } from './hooks';
import { formatBytes, formatDuration, timeAgo, viewerZone } from './format';
import { Button, CancelButton, EmptyState, Modal, PageHeader, StatusBadge } from './components/ui';
import { useToast } from './components/toast';
import { FilePicker } from './components/FilePicker';
import { zoneOptions } from './components/ScheduleBuilder';
import { zoneTag } from './cron';
import { TodayBar, WeekEditor, WeekStrip } from './components/WeekEditor';
import {
  isActive, isKnownZone, scheduleSummary, untilLabel, windowLabel, type ServiceWindow,
} from './serviceWindows';
import {
  STATE_WORD, byAttention, handovers, heldBy, isLive, resumeLabel, serviceCounts, stoppingDetail, toneOf,
  type LockMode, type Policy, type Service, type ServiceSession, type StartMode,
} from './serviceStatus';
export { serviceCounts };
export type { Service };

/* ─── Services ─────────────────────────────────────────────
   Long-running processes RunRail keeps up — a model server, a folder sync —
   as opposed to workflows, which run to completion. Everything a service says
   about itself here is what the supervisor last decided and wrote down; the
   page never guesses. A click does not touch a process either: it records
   what the person wants, and the supervisor acts on it within a second. */

interface Named { id: number; name: string; root_path?: string; status?: string; lock_resource?: string | null }

const MODE_LABEL: Record<StartMode, string> = {
  startup: 'On startup', schedule: 'Scheduled', manual: 'On demand',
};
const MODE_HINT: Record<StartMode, string> = {
  startup: 'Starts with RunRail and is kept running.',
  schedule: 'Runs inside the weekly windows below, and is stopped when one closes.',
  manual: 'Runs only after you press Start, until you press Stop.',
};
const POLICY_LABEL: Record<Policy, string> = {
  always: 'Always restart', on_failure: 'Restart only if it fails', never: 'Never restart',
};
const REASON_LABEL: Record<string, string> = {
  stopped_by_user: 'Stopped by you', window_closed: 'Window closed', restarted: 'Restarted',
  exited: 'Exited', crashed: 'Crashed', killed_after_timeout: 'Force-stopped at timeout',
  failed_to_start: 'Could not start', runrail_shutdown: 'RunRail stopped',
  interrupted: 'RunRail was interrupted', deleted: 'Deleted',
};
const TIMEOUT_PRESETS = [10, 30, 60, 180, 600];

const zoneOf = (s: Service) => s.timezone || 'UTC';

function formatAt(iso: string, zone: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: zone, weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(new Date(iso));
  } catch { return iso; }
}

/** " · 12% CPU · 1.3 GB" while a service runs and has been sampled. */
function resourcesLabel(s: Service): string {
  if (s.cpu_percent == null || s.rss_bytes == null) return '';
  return ` · ${Math.round(s.cpu_percent)}% CPU · ${formatBytes(s.rss_bytes)}`;
}

const timeoutLabel = (seconds: number) =>
  seconds < 60 ? `${seconds}s` : seconds % 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds / 60}m`;

/** The one line under a service's name: what it is doing and why. */
export function describe(s: Service, now: number): { text: string; resumable: boolean } {
  const zone = zoneOf(s);
  const back = s.override_until ? ` · back to its schedule ${formatAt(s.override_until, zone)}` : '';
  switch (s.state) {
    case 'running': {
      const since = s.current_session?.started_at;
      const up = since ? `Up ${formatDuration((now - new Date(since).getTime()) / 1000)}` : 'Running';
      const pid = s.current_session?.pid ? ` · pid ${s.current_session.pid}` : '';
      const using = resourcesLabel(s);
      if (s.override_running === true && s.override_reason === 'user') {
        return { text: `${up}${pid}${using} · started by you${back}`, resumable: s.start_mode === 'schedule' };
      }
      if (s.start_mode === 'schedule' && s.next_change) {
        return { text: `${up}${pid}${using} · stops ${formatAt(s.next_change, zone)}`, resumable: false };
      }
      return { text: `${up}${pid}${using}`, resumable: false };
    }
    case 'stopping':
      return { text: stoppingDetail(s, now), resumable: false };
    case 'waiting_lock':
    case 'backoff':
    case 'crash_loop':
    case 'starting':
      return { text: s.state_detail || '', resumable: false };
  }
  // Stopped.
  if (s.override_running === false && s.override_reason === 'user') {
    // A stop with no expiry (startup mode, or a schedule with nothing to
    // return to) holds until a person acts, and should say so.
    return { text: `Stopped by you${back || ' · stays off until you start it'}`, resumable: true };
  }
  if (s.override_reason === 'exited' && s.state_detail) return { text: s.state_detail, resumable: true };
  if (s.start_mode === 'manual') {
    const last = s.last_session;
    return {
      text: last?.stopped_at ? `Last ran ${timeAgo(last.stopped_at, now)} · ${REASON_LABEL[last.stop_reason ?? ''] ?? 'stopped'}`
        : 'Starts when you start it',
      resumable: false,
    };
  }
  if (s.start_mode === 'schedule') return { text: scheduleSummary(s.windows, zone, now), resumable: false };
  return { text: s.state_detail || 'Stopped', resumable: false };
}

/** The list every surface shares. `supervisor` also asks whether anything is
 *  supervising — only the Services page has somewhere to say so. `pollIdle`
 *  keeps the 5s poll going with no services; the dashboard turns it off, since
 *  a page that shows nothing about services should not spend a request on them
 *  every few seconds (the socket still announces the first one). */
export function useServices({ supervisor = true, pollIdle = true } = {}) {
  const [items, setItems] = useState<Service[] | null>(null);
  const [supervised, setSupervised] = useState(true);
  const load = () => {
    void api<Service[]>('/services').then(setItems).catch(() => setItems(prev => prev ?? []));
    if (supervisor) void api<{ active: boolean }>('/services/supervisor').then(r => setSupervised(r.active)).catch(() => {});
  };
  useEffect(load, []);
  // A supervisor pass can announce several services at once; one reload a
  // beat later is enough for all of them.
  useEffect(() => {
    let timer = 0;
    const off = rrws.on('service_updated', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(load, 250);
    });
    return () => { off(); window.clearTimeout(timer); };
  }, []);
  // The socket carries every change; the poll covers a dropped connection.
  useVisibleInterval(pollIdle || items === null || items.length > 0, load, 5000);
  return { items, supervised, load };
}

/* ─── Actions ───────────────────────────────────────────── */

function useServiceActions(onDone: () => void) {
  const { toast } = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (s: Service, action: 'start' | 'stop' | 'restart' | 'resume') => {
    setBusy(action);
    try {
      await post(`/services/${s.id}/${action}`, {});
      const said: Record<string, string> = {
        start: `Starting ${s.name}`, stop: `Stopping ${s.name}`,
        restart: `Restarting ${s.name}`, resume: `${s.name} is back on its schedule`,
      };
      toast(said[action], 'info');
      onDone();
    } catch (error) {
      toast(error instanceof Error ? error.message : `Could not ${action} ${s.name}`, 'error');
    } finally { setBusy(null); }
  };
  return { act, busy };
}

function PrimaryAction({ s, act, busy, size = 'sm' }: {
  s: Service; act: ReturnType<typeof useServiceActions>['act']; busy: string | null; size?: 'sm' | 'md';
}) {
  return s.should_run || isLive(s)
    ? <Button variant="ghost" size={size} disabled={!!busy || s.state === 'stopping'}
              onClick={() => act(s, 'stop')}><Square size={12} /> Stop</Button>
    : <Button size={size} disabled={!!busy} onClick={() => act(s, 'start')}><Play size={12} /> Start</Button>;
}

/* ─── List ──────────────────────────────────────────────── */

export function ServicesPage() {
  const { items, supervised, load } = useServices();
  const [creating, setCreating] = useState(false);
  const live = (items ?? []).some(isLive);
  const now = useNow(live);
  const { act, busy } = useServiceActions(load);
  const running = (items ?? []).filter(s => s.state === 'running').length;

  return (
    <>
      <PageHeader
        eyebrow="PROCESSES"
        title="Services"
        subtitle="Long-running processes RunRail keeps up — model servers, folder syncs — on startup, on a schedule, or on demand."
        action={<Button onClick={() => setCreating(true)}><Server size={14} /> New service</Button>}
      />
      {!supervised && (items ?? []).length > 0 && (
        <div className="callout callout--warning services-supervisor-note">
          Nothing is supervising services right now, so none will start or stop. They are
          managed by <code>runrail serve</code> or <code>runrail worker</code>; <code>runrail api</code> alone does not.
        </div>
      )}
      {items && items.length > 0 && (
        <p className="services-summary">
          {running} of {items.length} running
        </p>
      )}
      {items && items.length === 0 && (
        <EmptyState
          icon={<Server size={24} />}
          title="No services yet"
          text="A service is a command RunRail keeps running: a model server like llama.cpp, a script that syncs folders. Start it with RunRail, within weekly windows, or by hand — and two services can take turns on one GPU."
          action={<Button onClick={() => setCreating(true)}>Add a service</Button>}
        />
      )}
      <div className="service-grid">
        {(items ?? []).map(s => {
          const line = describe(s, now);
          return (
            <article key={s.id} className={clsx('service-card', `service-card--${s.state}`)}>
              <div className="service-card-head">
                <span className={clsx('service-glyph', isLive(s) && 'is-live')}><Server size={16} /></span>
                <div className="service-card-title">
                  <Link to={`/services/${s.id}`}><h3 title={s.name}>{s.name}</h3></Link>
                  <div className="service-chips">
                    <span className="type-chip">{MODE_LABEL[s.start_mode]}</span>
                    {s.lock_resource && (
                      <span className="type-chip lock-chip" title={`${s.lock_mode === 'exclusive' ? 'Exclusive' : 'Shared'} lock on ${s.lock_resource}`}>
                        {s.lock_mode === 'exclusive' ? <Lock size={10} /> : <Users size={10} />} {s.lock_resource}
                      </span>
                    )}
                  </div>
                </div>
                <StatusBadge value={s.state} />
              </div>
              <p className="service-line">{line.text || ' '}</p>
              {s.start_mode === 'schedule' && <WeekStrip windows={s.windows} zone={zoneOf(s)} compact />}
              <div className="service-card-actions">
                <PrimaryAction s={s} act={act} busy={busy} />
                {isLive(s) && (
                  <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => act(s, 'restart')}>
                    <RotateCcw size={12} /> Restart
                  </Button>
                )}
                {line.resumable && (
                  <Button variant="ghost" size="sm" disabled={!!busy} onClick={() => act(s, 'resume')}
                          title="Drop your Start/Stop and follow the service's own mode again">
                    <Undo2 size={12} /> {resumeLabel(s)}
                  </Button>
                )}
                <Link className="service-open" to={`/services/${s.id}`}>Logs & history →</Link>
              </div>
            </article>
          );
        })}
      </div>
      {creating && <ServiceModal onClose={() => setCreating(false)} done={() => { setCreating(false); load(); }} />}
    </>
  );
}

/* ─── Detail ────────────────────────────────────────────── */

export function ServiceDetail() {
  const { id } = useParams();
  const navTo = useNavigate();
  const { toast } = useToast();
  const [s, setS] = useState<Service | null>(null);
  const [missing, setMissing] = useState(false);
  const [sessions, setSessions] = useState<ServiceSession[]>([]);
  const [editing, setEditing] = useState(false);
  const load = () => {
    // Only a 404 means gone. A dropped connection keeps showing what we last
    // knew rather than telling the person their service was deleted.
    void api<Service>(`/services/${id}`)
      .then(next => { setS(next); setMissing(false); })
      .catch(error => { if (error instanceof ApiError && error.status === 404) setMissing(true); });
    void api<ServiceSession[]>(`/services/${id}/sessions?limit=25`).then(setSessions).catch(() => {});
  };
  useEffect(load, [id]);
  useEffect(() => {
    let timer = 0;
    const off = rrws.on('service_updated', e => {
      if (String(e.id) !== id) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(load, 250);
    });
    return () => { off(); window.clearTimeout(timer); };
  }, [id]);
  useVisibleInterval(true, load, 5000);
  const now = useNow(!!s && isLive(s));
  const { act, busy } = useServiceActions(load);

  if (missing) {
    return <EmptyState icon={<Server size={24} />} title="Service not found"
                       text="It may have been deleted." action={<Link to="/services">Back to services</Link>} />;
  }
  if (!s) return null;
  const line = describe(s, now);
  const zoneKnown = isKnownZone(zoneOf(s));
  const remove = async () => {
    if (!confirm(`Delete service "${s.name}"?`)) return;
    try {
      await del(`/services/${s.id}`);
      toast('Service deleted', 'info');
      navTo('/services');
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not delete the service', 'error');
    }
  };

  return (
    <>
      <div className="detail-head">
        <Link to="/services"><ArrowLeft size={14} /> Services</Link>
        <div className="detail-head-row">
          <div className={clsx('workflow-glyph large service-glyph', isLive(s) && 'is-live')}><Server size={22} /></div>
          <div className="detail-head-text">
            <span className="eyebrow">SERVICE · {MODE_LABEL[s.start_mode].toUpperCase()}</span>
            <h1>{s.name}</h1>
            <p>{s.description || line.text}</p>
          </div>
          <div className="detail-actions">
            <PrimaryAction s={s} act={act} busy={busy} size="md" />
            {isLive(s) && <Button variant="ghost" disabled={!!busy} onClick={() => act(s, 'restart')}><RotateCcw size={13} /> Restart</Button>}
            {line.resumable && <Button variant="ghost" disabled={!!busy} onClick={() => act(s, 'resume')}><Undo2 size={13} /> {resumeLabel(s)}</Button>}
            <Button variant="ghost" onClick={() => setEditing(true)}><Pencil size={13} /> Edit</Button>
            {/* The API answers 409 while the process is alive: a delete never kills anything. */}
            <Button variant="danger" onClick={remove} disabled={isLive(s)}
                    title={isLive(s) ? 'Stop the service before deleting it' : 'Delete service'}><Trash2 size={13} /></Button>
          </div>
        </div>
      </div>

      <div className="service-detail-grid">
        <section className="panel service-status">
          <div className="panel-head">
            <div className="service-status-head">
              <StatusBadge value={s.state} />
              <span className="service-line">{line.text}</span>
            </div>
          </div>
          <dl className="service-facts panel-pad">
            <dt>Command</dt><dd><code className="code-inline">{s.command}</code></dd>
            {s.cwd && <><dt>Folder</dt><dd><code className="code-inline">{s.cwd}</code></dd></>}
            <dt>Restarts</dt><dd>{POLICY_LABEL[s.restart_policy]}</dd>
            <dt>Stop timeout</dt><dd>{timeoutLabel(s.stop_timeout_seconds)} before a forced stop</dd>
            {s.lock_resource && (
              <><dt>Lock</dt><dd>{s.lock_mode === 'exclusive' ? 'Runs alone on' : 'Shares'} <code className="code-inline">{s.lock_resource}</code></dd></>
            )}
            {s.current_session?.pid && <><dt>Process</dt><dd>pid {s.current_session.pid}</dd></>}
            {s.cpu_percent != null && s.rss_bytes != null && (
              <><dt>Using</dt><dd>{Math.round(s.cpu_percent)}% CPU · {formatBytes(s.rss_bytes)} memory</dd></>
            )}
          </dl>
        </section>
        {s.start_mode === 'schedule' && (
          <section className="panel service-schedule">
            <div className="panel-head">
              <div><h2><CalendarClock size={14} /> Schedule</h2>
                <p>{zoneKnown ? `${zoneOf(s)} · ${scheduleSummary(s.windows, zoneOf(s), now)}`
                  : <span className="window-error">Unknown timezone "{zoneOf(s)}" — edit the service to fix it.</span>}</p></div>
            </div>
            <div className="panel-pad">
              <WeekStrip windows={s.windows} zone={zoneOf(s)} />
              <ul className="service-window-list">
                {s.windows.map((w, i) => <li key={i}><span className={`window-swatch week-bar--c${i % 6}`} />{windowLabel(w)}</li>)}
              </ul>
            </div>
          </section>
        )}
      </div>

      <ServiceLog service={s} sessions={sessions} />

      <section className="panel">
        <div className="panel-head"><div><h2>History</h2><p>Each start, how long it ran, and why it ended</p></div></div>
        {sessions.length === 0 ? <p className="muted panel-pad">It has not run yet.</p> : (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Started</th><th>Ran for</th><th>Ended</th><th>Exit</th></tr></thead>
              <tbody>
                {sessions.map(x => (
                  <tr key={x.id}>
                    <td title={new Date(x.started_at).toLocaleString()}>{timeAgo(x.started_at, now)}</td>
                    <td>{formatDuration(((x.stopped_at ? new Date(x.stopped_at).getTime() : now) - new Date(x.started_at).getTime()) / 1000)}</td>
                    <td>{x.stopped_at ? <span className={clsx('reason', `reason--${x.stop_reason}`)}>{REASON_LABEL[x.stop_reason ?? ''] ?? x.stop_reason}</span>
                                      : <StatusBadge value="running" />}</td>
                    <td>{x.exit_code ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {editing && <ServiceModal service={s} onClose={() => setEditing(false)} done={() => { setEditing(false); load(); }} />}
    </>
  );
}

function ServiceLog({ service, sessions }: { service: Service; sessions: ServiceSession[] }) {
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [text, setText] = useState('');
  const [follow, setFollow] = useState(true);
  const box = useRef<HTMLPreElement>(null);
  const viewing = sessionId ?? sessions[0]?.id ?? null;
  const live = sessions.find(x => x.id === viewing)?.stopped_at === null;
  // Plain text, so fetched directly rather than through the JSON helper.
  const url = `/api/services/${service.id}/log?tail_bytes=200000&session_id=${viewing}`;
  // Every read is numbered, and only the latest may land: a slow answer for
  // the previous run, or an older poll, must not overwrite a newer one.
  const latest = useRef(0);
  const read = () => {
    const ticket = ++latest.current;
    void fetch(url).then(r => (r.ok ? r.text() : '')).then(t => { if (ticket === latest.current) setText(t); }).catch(() => {});
  };
  useEffect(() => {
    if (viewing == null) { latest.current++; setText(''); return; }
    read();
  }, [url, viewing]);
  // A live session's log is re-read every two seconds while the tab is visible.
  useVisibleInterval(live, read, 2000);
  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [text, follow]);

  return (
    <section className="panel service-log">
      <div className="panel-head service-log-head">
        <div><h2>{live ? <><span className="live-dot" /> Live log</> : 'Log'}</h2><p>{live ? 'Following new output' : viewing == null ? 'Nothing has run yet' : 'Output of the selected run'}</p></div>
        {sessions.length > 1 && (
          <select aria-label="Session" value={viewing ?? ''} onChange={e => setSessionId(Number(e.target.value))}>
            {sessions.map((x, i) => (
              <option key={x.id} value={x.id}>
                {i === 0 && !x.stopped_at ? 'Current run' : `${new Date(x.started_at).toLocaleString()} · ${REASON_LABEL[x.stop_reason ?? ''] ?? 'running'}`}
              </option>
            ))}
          </select>
        )}
      </div>
      <pre className="log-content service-log-body panel-pad" ref={box}
           onScroll={e => {
             const el = e.currentTarget;
             setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
           }}>
        {viewing == null ? 'Nothing yet — the log appears when the service first starts.' : text || '(no output yet)'}
      </pre>
    </section>
  );
}

/* ─── Create / edit ─────────────────────────────────────── */

const envText = (vars: Record<string, string> | null) =>
  Object.entries(vars ?? {}).map(([k, v]) => `${k}=${v}`).join('\n');

function parseEnv(text: string): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const at = line.indexOf('=');
    if (at < 1) throw new Error(`"${line}" is not KEY=value`);
    out[line.slice(0, at).trim()] = line.slice(at + 1);
  }
  return Object.keys(out).length ? out : null;
}

export function ServiceModal({ service, onClose, done }: {
  service?: Service; onClose: () => void; done: () => void;
}) {
  const { toast } = useToast();
  const [mode, setMode] = useState<StartMode>(service?.start_mode ?? 'startup');
  const [windows, setWindows] = useState<ServiceWindow[]>(
    service?.windows ?? [{ days: ['mon', 'tue', 'wed', 'thu', 'fri'], start: '09:00', end: '17:00' }]);
  // A stored null means UTC to the supervisor; only a brand-new service starts
  // from the viewer's zone.
  const [zone, setZone] = useState(service ? service.timezone ?? 'UTC' : viewerZone());
  const [policy, setPolicy] = useState<Policy | ''>(service?.restart_policy ?? '');
  // Kept as typed, so clearing the field to type "120" does not snap to 1.
  const [timeoutText, setTimeoutText] = useState(String(service?.stop_timeout_seconds ?? 30));
  const timeout = Number(timeoutText);
  const [cwd, setCwd] = useState(service?.cwd ?? '');
  const [projectId, setProjectId] = useState<string>(service?.project_id ? String(service.project_id) : '');
  const [envId, setEnvId] = useState<string>(service?.environment_id ? String(service.environment_id) : '');
  const [lockOn, setLockOn] = useState(Boolean(service?.lock_resource));
  const [resource, setResource] = useState(service?.lock_resource ?? '');
  const [lockMode, setLockMode] = useState<LockMode>(service?.lock_mode ?? 'exclusive');
  const [projects, setProjects] = useState<Named[]>([]);
  const [envs, setEnvs] = useState<Named[]>([]);
  const [resourcesInUse, setResourcesInUse] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const zones = useMemo(() => zoneOptions(zone), [zone]);
  const effectivePolicy: Policy = policy || (mode === 'manual' ? 'never' : 'always');
  const project = projects.find(p => String(p.id) === projectId);

  useEffect(() => {
    void api<Named[]>('/projects').then(setProjects).catch(() => {});
    void api<Named[]>('/environments').then(setEnvs).catch(() => {});
    void Promise.all([api<Named[]>('/workflows').catch(() => []), api<Named[]>('/services').catch(() => [])])
      .then(([flows, services]) => setResourcesInUse([...new Set(
        [...flows, ...services].map(x => x.lock_resource).filter((r): r is string => !!r))].sort()));
  }, []);

  const submit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    if (mode === 'schedule' && (!windows.length || windows.some(w => !w.days.length))) {
      toast('A scheduled service needs at least one window, each with a day', 'error');
      return;
    }
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 3600) {
      toast('The stop timeout must be a whole number of seconds, from 1 to 3600', 'error');
      return;
    }
    let env_vars_json: Record<string, string> | null;
    try { env_vars_json = parseEnv(String(f.get('env_vars') ?? '')); } catch (error) {
      toast(error instanceof Error ? error.message : 'Bad environment variables', 'error');
      return;
    }
    const body = {
      name: String(f.get('name') ?? '').trim(),
      description: String(f.get('description') ?? '').trim() || null,
      command: String(f.get('command') ?? '').trim(),
      cwd: cwd.trim() || null,
      project_id: projectId ? Number(projectId) : null,
      environment_id: envId ? Number(envId) : null,
      env_vars_json,
      start_mode: mode,
      windows: mode === 'schedule' ? windows : [],
      timezone: mode === 'schedule' ? zone : null,
      restart_policy: effectivePolicy,
      stop_timeout_seconds: timeout,
      lock_resource: lockOn && resource.trim() ? resource.trim() : null,
      lock_mode: lockOn && resource.trim() ? lockMode : 'exclusive',
      notify_webhook_url: String(f.get('notify_webhook_url') ?? '').trim() || null,
    };
    setSaving(true);
    try {
      if (service) await put(`/services/${service.id}`, body);
      else await post('/services', body);
      toast(service ? 'Service saved' : 'Service created', 'success');
      done();
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not save the service', 'error');
    } finally { setSaving(false); }
  };

  return (
    <Modal wide title={service ? 'Edit service' : 'New service'}
           subtitle={service ? 'Changes to the command apply the next time it starts.' : 'A command RunRail keeps running.'}
           onClose={onClose}>
      <form className="modal-body form-stack service-form" onSubmit={submit}>
        <div className="field-row">
          <label className="field"><span>Name</span>
            <input name="name" defaultValue={service?.name} placeholder="llama-server" required autoFocus maxLength={255} />
          </label>
          <label className="field"><span>Description <em>Optional</em></span>
            <input name="description" defaultValue={service?.description ?? ''} placeholder="Qwen 32B for the team UI" />
          </label>
        </div>
        <label className="field"><span>Command <em>Run by the shell, in the folder below</em></span>
          <textarea className="code-input" name="command" required defaultValue={service?.command}
                    placeholder="llama-server -m models/qwen-32b.gguf --port 8081" />
        </label>
        <div className="field-row">
          <label className="field"><span>Project <em>Optional — its folder is the default</em></span>
            <select value={projectId} onChange={e => setProjectId(e.target.value)}>
              <option value="">None</option>
              {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label className="field"><span>Environment <em>Optional — its interpreter comes first on PATH</em></span>
            {/* Controlled: the options arrive after the first render, when a
                defaultValue would already have settled on "None". */}
            <select value={envId} onChange={e => setEnvId(e.target.value)}>
              <option value="">None</option>
              {envs.map(x => <option key={x.id} value={x.id}>{x.name}{x.status && x.status !== 'ready' ? ` (${x.status})` : ''}</option>)}
            </select>
          </label>
        </div>
        <FilePicker value={cwd} onChange={setCwd} browseFrom={project?.root_path} mode="directories"
                    label="Working directory" placeholder={project ? `Optional — defaults to ${project.name}` : 'Optional — defaults to the RunRail home folder'} />

        <fieldset className="service-section">
          <legend>When it runs</legend>
          <div className="segmented" role="radiogroup" aria-label="When it runs">
            {(['startup', 'schedule', 'manual'] as StartMode[]).map(m => (
              <button key={m} type="button" role="radio" aria-checked={mode === m}
                      className={mode === m ? 'active' : ''} onClick={() => setMode(m)}>
                {m === 'startup' ? <Power size={13} /> : m === 'schedule' ? <CalendarClock size={13} /> : <Zap size={13} />}
                {MODE_LABEL[m]}
              </button>
            ))}
          </div>
          <p className="field-hint">{MODE_HINT[mode]}</p>
          {mode === 'schedule' && (
            <>
              <label className="field service-zone"><span>Timezone</span>
                <select value={zone} onChange={e => setZone(e.target.value)}>
                  {zones.map(z => <option key={z} value={z}>{z === viewerZone() ? `${z} — your timezone` : z} ({zoneTag(z)})</option>)}
                </select>
              </label>
              <WeekEditor windows={windows} zone={zone} onChange={setWindows} />
            </>
          )}
        </fieldset>

        <fieldset className="service-section">
          <legend>Stopping and restarts</legend>
          <div className="field-row">
            <label className="field"><span>If it exits on its own</span>
              <select value={effectivePolicy} onChange={e => setPolicy(e.target.value as Policy)}>
                {(Object.keys(POLICY_LABEL) as Policy[]).map(p => <option key={p} value={p}>{POLICY_LABEL[p]}</option>)}
              </select>
              <small>Restarts wait 1s, 2s, 4s… up to 5 minutes. Five exits in ten minutes stops it and sends a notification.</small>
            </label>
            <label className="field"><span>Stop timeout</span>
              <div className="timeout-row">
                <input type="number" min={1} max={3600} step={1} value={timeoutText} aria-label="Stop timeout in seconds"
                       required onChange={e => setTimeoutText(e.target.value)} />
                <span className="timeout-unit">seconds</span>
                {TIMEOUT_PRESETS.map(p => (
                  <button key={p} type="button" className={clsx('preset-chip', timeout === p && 'on')}
                          onClick={() => setTimeoutText(String(p))}>{timeoutLabel(p)}</button>
                ))}
              </div>
              <small>Asked to stop politely first; force-stopped if still running after this. Give a model server time to finish the request it is serving.</small>
            </label>
          </div>
        </fieldset>

        <fieldset className="service-section">
          <legend>Sharing a resource</legend>
          <div className={clsx('lock-field', lockOn && 'is-on')}>
            <label className="lock-head">
              <span className="lock-glyph"><Lock size={14} /></span>
              <span className="lock-text">
                <strong>Take turns on a resource</strong>
                <small>Name something only one thing should use at a time, like <code>gpu</code>. Services and workflows that name it wait for each other — nothing is ever interrupted.</small>
              </span>
              <span className="toggle">
                <input type="checkbox" checked={lockOn} onChange={e => setLockOn(e.target.checked)} aria-label="Lock a resource" />
                <span />
              </span>
            </label>
            {lockOn && (
              <div className="lock-body">
                <label className="field"><span>Resource name</span>
                  <input list="service-resources" value={resource} onChange={e => setResource(e.target.value)}
                         placeholder="gpu" required maxLength={255} autoComplete="off" spellCheck={false} />
                  <datalist id="service-resources">{resourcesInUse.map(r => <option key={r} value={r} />)}</datalist>
                </label>
                <div className="lock-modes" role="radiogroup" aria-label="How this service uses the resource">
                  {(['exclusive', 'shared'] as LockMode[]).map(m => (
                    <label key={m} className={clsx('lock-mode', `lock-mode--${m}`, lockMode === m && 'on')}>
                      <input type="radio" name="service-lock-mode" checked={lockMode === m} onChange={() => setLockMode(m)} />
                      <span className="lock-mode-glyph">{m === 'exclusive' ? <Lock size={13} /> : <Users size={13} />}</span>
                      <span className="lock-mode-text">
                        <strong>{m === 'exclusive' ? 'Runs alone' : 'Runs alongside'}</strong>
                        <small>{m === 'exclusive'
                          ? `While it runs, nothing else using ${resource.trim() || 'this'} starts; it waits its turn when it is busy.`
                          : `Shares ${resource.trim() || 'it'} with other shared users, but waits for one that runs alone.`}</small>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>
        </fieldset>

        <details className="service-advanced" open={Boolean(service?.env_vars_json || service?.notify_webhook_url)}>
          <summary>More options</summary>
          <label className="field"><span>Environment variables <em>KEY=value, one per line</em></span>
            <textarea className="code-input" name="env_vars" defaultValue={envText(service?.env_vars_json ?? null)}
                      placeholder={'OLLAMA_HOST=0.0.0.0\nCUDA_VISIBLE_DEVICES=0'} />
          </label>
          <label className="field"><span>Notify webhook <em>Optional — crash loops are posted here, else to the default</em></span>
            <input name="notify_webhook_url" type="url" defaultValue={service?.notify_webhook_url ?? ''}
                   placeholder="https://hooks.slack.com/…" />
          </label>
        </details>

        <div className="modal-actions">
          <CancelButton />
          <Button type="submit" disabled={saving}>{service ? 'Save' : 'Create service'}</Button>
        </div>
      </form>
    </Modal>
  );
}

/* ─── On the dashboard and the wallboard ─────────────────
   Services are things that should be up, so the question these answer is
   "is anything not as it should be?" — the ones that need a person come
   first, then the ones the supervisor is busy with, then the healthy ones. */

const DASH_ROWS = 6;

/** Dashboard panel. Hidden until there is a service to show. */
export function ServicesPanel({ items, onChanged }: { items: Service[] | null; onChanged: () => void }) {
  const live = (items ?? []).some(isLive);
  const now = useNow(live);
  const { act, busy } = useServiceActions(onChanged);
  if (!items || items.length === 0) return null;
  const { up, down } = serviceCounts(items);
  const ordered = [...items].sort(byAttention);
  return (
    <div className="panel services-panel" style={{ marginBottom: 16 }}>
      <div className="panel-head">
        <div>
          <h2>Services</h2>
          <p>{up} of {items.length} up{down ? ` · ${down} need${down === 1 ? 's' : ''} you` : ''}</p>
        </div>
        <Link className="panel-link" to="/services">All →</Link>
      </div>
      <div className="svc-rows">
        {ordered.slice(0, DASH_ROWS).map(s => {
          const tone = toneOf(s);
          const running = s.should_run || isLive(s);
          return (
            <div key={s.id} className={clsx('svc-row', `svc-row--${tone}`)}>
              <span className={clsx('svc-dot', `svc-dot--${tone}`)} aria-hidden="true" />
              <Link className="svc-row-main" to={`/services/${s.id}`}>
                <span className="svc-row-name">
                  <b title={s.name}>{s.name}</b>
                  <em className={`svc-word svc-word--${tone}`}>{STATE_WORD[s.state] ?? s.state}</em>
                </span>
                <small title={describe(s, now).text}>{describe(s, now).text || MODE_LABEL[s.start_mode]}</small>
                {s.start_mode === 'schedule' && <TodayBar windows={s.windows} zone={zoneOf(s)} />}
              </Link>
              <button type="button" className="svc-row-action" disabled={!!busy || s.state === 'stopping'}
                      onClick={() => act(s, running ? 'stop' : 'start')}
                      title={running ? `Stop ${s.name}` : `Start ${s.name}`}
                      aria-label={running ? `Stop ${s.name}` : `Start ${s.name}`}>
                {running ? <Square size={12} /> : <Play size={12} />}
              </button>
            </div>
          );
        })}
      </div>
      {items.length > DASH_ROWS && (
        <Link className="svc-more" to="/services">{items.length - DASH_ROWS} more →</Link>
      )}
    </div>
  );
}

function clockTime(iso: string, zone: string): string {
  try {
    return new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', hour12: false })
      .format(new Date(iso));
  } catch { return iso; }
}

/** The wallboard's services band: big enough to read across a room. */
export function WallboardServices({ items, now }: { items: Service[]; now: number }) {
  if (!items.length) return null;
  const ordered = [...items].sort(byAttention);
  const { up } = serviceCounts(items);
  const swaps = handovers(items);
  return (
    <section className="wb-section wb-services" aria-label="Services">
      <div className="wb-section-head">
        <span className="wb-section-title"><Server size={15} /> Services</span>
        <em className="wb-section-count">{up}/{items.length} up</em>
        {swaps.map(h => (
          <span key={h.resource} className="wb-handover" title={`Who has ${h.resource}`}>
            <Lock size={12} /> <code>{h.resource}</code>
            {h.holders.length
              ? <b className={`wb-handover-holder wb-handover-holder--${toneOf(h.holders[0])}`}>{heldBy(h)}</b>
              : <span className="wb-handover-free">{heldBy(h)}</span>}
            {h.waiting
              ? <><span className="wb-handover-arrow">→</span><span>{h.waiting.name} waiting</span></>
              : h.upcoming && <><span className="wb-handover-arrow">→</span>
                  <span>{h.upcoming.name} at {clockTime(h.upcoming.next_change!, zoneOf(h.upcoming))}</span></>}
          </span>
        ))}
        <span className="wb-section-rule" aria-hidden="true" />
      </div>
      <div className="wb-service-grid">
        {ordered.map(s => {
          const tone = toneOf(s);
          const since = s.current_session?.started_at;
          // Stopped inside its window means a person stopped it, and next_change
          // is then the window's END — "starts" would promise the wrong thing.
          const inWindow = s.start_mode === 'schedule' && isKnownZone(zoneOf(s)) && isActive(s.windows, zoneOf(s), new Date(now));
          const detail = s.state === 'running' && since
            ? `up ${formatDuration((now - new Date(since).getTime()) / 1000)}`
            : s.state === 'stopped' && s.start_mode === 'schedule' && s.next_change
              ? `${inWindow ? 'stopped by hand · window ends' : 'starts'} ${clockTime(s.next_change, zoneOf(s))} · ${untilLabel(new Date(s.next_change), now)}`
              : s.state === 'stopped' && s.start_mode === 'manual' ? 'on demand'
              : s.state === 'stopping' ? stoppingDetail(s, now)
              : s.state_detail || describe(s, now).text;
          return (
            <Link key={s.id} to={`/services/${s.id}`} className={clsx('wb-service', `wb-service--${tone}`)}>
              <span className="wb-service-top">
                <span className="wb-service-name" title={s.name}>{s.name}</span>
                <span className="wb-service-state">{STATE_WORD[s.state] ?? s.state}</span>
              </span>
              <span className="wb-service-detail" title={detail}>{detail}</span>
              {s.start_mode === 'schedule' && <TodayBar windows={s.windows} zone={zoneOf(s)} className="today-bar--wb" />}
            </Link>
          );
        })}
      </div>
    </section>
  );
}
