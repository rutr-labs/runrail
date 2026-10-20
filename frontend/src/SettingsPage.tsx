import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent, type ReactNode } from 'react';
import {
  Archive, Bell, BellOff, CalendarClock, Check, Copy, Cpu, Database, Download, ExternalLink, Eye, EyeOff, FileArchive,
  FileCode2, HardDrive, KeyRound, Lock, Minus, Moon, Pause, Pencil, Play, Plus, RefreshCw, Server, ShieldCheck, Sun, Trash2, Upload,
} from 'lucide-react';
import clsx from 'clsx';
import { ApiError, api, errorMessage, post, put } from './api';
import { useNow, useVisibleInterval } from './hooks';
import { formatDate, timeAgo, viewerZone } from './format';
import { Button, PageHeader } from './components/ui';
import { useToast } from './components/toast';
import {
  CODE_TRACKING_OPTIONS, MASK, RETENTION_PRESETS, basename, cleanupLine, eventLabel, keyLine, lastCleanupLine, formatCountdown, formatSize, formatUptime, groupEvents,
  lockedNote, overrideLine, parseCount, parseDays, pausedBanner, provenance, quietSpans, restoreSummaryLine,
  retentionSentence, sameValue, secretValueProblem, settledSentence, storageSegments, systemHeadline, variableNameProblem,
  type CodeTracking, type VariableRow, type VariablesResponse,
  type BackupPreview, type CleanupState, type LastCleanup, type NotifyTestResult, type QuietHours, type RestorePoint,
  type RestoreSummary, type RetentionPreview, type Setting, type SettingKey,
  type SettingValues, type SettingsResponse, type SystemStatus,
} from './settingsModel';

/* ─── Settings ─────────────────────────────────────────────
   The operator's panel. A hero says what state the whole system is in and
   holds the one switch that pauses everything new; tiles show the worker,
   services, the next scheduled run, the database and what the home folder
   holds; then the settings that apply without a restart, backups, and the
   theme. Every setting the environment pins is shown locked and names the
   variable; every save is per group and all-or-nothing, with the server's
   reason shown next to the group it refused. Long paths are never printed:
   a chip shows the last segment and copies the whole. */

const STATUS_POLL_MS = 10_000;
const MEASURING_POLL_MS = 3_000;
const PREVIEW_DEBOUNCE_MS = 400;
const GITHUB = 'https://github.com/rutr-labs/runrail';

type SettingsMap = Map<string, Setting>;

/** The typed value of one setting; the server's shape is pinned by key. */
function valueOf<K extends SettingKey>(settings: SettingsMap, key: K): SettingValues[K] | undefined {
  return settings.get(key)?.value as SettingValues[K] | undefined;
}

/** A zip posted as the request body, the way /system/restore wants it. */
async function uploadZip<T>(path: string, file: File): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/zip' }, body: file,
  });
  if (!response.ok) throw new ApiError(errorMessage(await response.text(), response.status), response.status);
  return response.json();
}

const reasonOf = (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback;

/* ─── Page ─────────────────────────────────────────────────── */

export function SettingsPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [settings, setSettings] = useState<SettingsMap | null>(null);
  const [eventKinds, setEventKinds] = useState<string[]>([]);
  const now = useNow(true, 30_000);

  const loadStatus = useCallback((refresh = false) => {
    void api<SystemStatus>(`/system/status${refresh ? '?refresh=1' : ''}`)
      .then(next => { setStatus(next); setStatusError(null); })
      .catch(error => setStatusError(reasonOf(error, 'Could not reach the server')));
  }, []);
  const applySettings = useCallback((response: SettingsResponse) => {
    setSettings(new Map(response.settings.map(s => [s.key, s])));
    setEventKinds(response.event_kinds);
  }, []);
  const loadSettings = useCallback(() => {
    void api<SettingsResponse>('/settings').then(applySettings)
      .catch(error => toast(reasonOf(error, 'Could not load settings'), 'error'));
  }, [applySettings, toast]);

  useEffect(() => { loadStatus(); loadSettings(); }, [loadStatus, loadSettings]);
  useVisibleInterval(true, () => loadStatus(), STATUS_POLL_MS);
  // Storage sizes arrive from a background measurement; while one is
  // running, ask again sooner so the bar fills in.
  useVisibleInterval(status != null && (status.storage.measuring || status.retention.cleanup.running),
                     () => loadStatus(), MEASURING_POLL_MS);

  // A save changes what the worker does next; the tiles should agree.
  const onSaved = useCallback((response: SettingsResponse) => { applySettings(response); loadStatus(); }, [applySettings, loadStatus]);

  return (
    <div className="settings-page">
      <PageHeader eyebrow="SYSTEM" title="Settings"
                  subtitle="What this RunRail is doing, and the settings that apply without a restart." />
      {statusError && !status && (
        <div className="callout callout--warning" role="alert">Could not read the system status: {statusError}</div>
      )}
      <SystemHero status={status} now={now} help={settings?.get('paused')?.help ?? null}
                  onChanged={next => { setStatus(next); loadSettings(); }} />
      <StatusTiles status={status} now={now} onRefresh={() => loadStatus(true)} />

      {settings && (
        <div className="settings-grid">
          <WorkersGroup settings={settings} now={now} onSaved={onSaved} />
          <RetentionGroup settings={settings} now={now} onSaved={onSaved} cleanup={status?.retention.cleanup}
                          last={status?.retention.last} />
          <NotificationsGroup settings={settings} eventKinds={eventKinds} now={now} onSaved={onSaved} />
          <CodeTrackingGroup settings={settings} now={now} onSaved={onSaved} />
        </div>
      )}
      <VariablesSection />
      <BackupSection home={status?.home ?? null} />
      <AppearanceSection />
      {status && <SettingsFooter status={status} />}
    </div>
  );
}

/* ─── Shared pieces ───────────────────────────────────────── */

/** A section's heading: an icon tile, a title, and what the section is for. */
function SectionHead({ icon, title, children, tone = 'accent' }: {
  icon: ReactNode; title: string; children?: ReactNode; tone?: 'accent' | 'warning' | 'success' | 'danger';
}) {
  return (
    <header className="settings-head">
      <span className={clsx('settings-head-icon', `is-${tone}`)}>{icon}</span>
      <div>
        <h3>{title}</h3>
        {children && <p>{children}</p>}
      </div>
    </header>
  );
}

/** A path shown as its last segment; the whole path on hover, copied on click. */
function PathChip({ path, what }: { path: string; what: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(path).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <button type="button" className={clsx('path-chip', copied && 'is-copied')} onClick={copy}
            title={`${path}\nClick to copy`} aria-label={`Copy the ${what} path: ${path}`}>
      <span className="path-chip-name">{basename(path)}</span>
      {copied ? <Check size={11} /> : <Copy size={11} />}
      <span className="path-chip-done" aria-live="polite">{copied ? 'Copied' : ''}</span>
    </button>
  );
}

/* ─── Hero: the system's state and the pause switch ───────── */

function SystemHero({ status, now, help, onChanged }: {
  status: SystemStatus | null; now: number; help: string | null; onChanged: (next: SystemStatus) => void;
}) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const set = async (next: boolean) => {
    if (next && !confirm('Pause everything new? Queued runs and services wait until you resume; what is executing finishes.')) return;
    setBusy(true);
    try { onChanged(await post<SystemStatus>('/system/pause', { paused: next })); }
    catch (error) { toast(reasonOf(error, next ? 'Could not pause' : 'Could not resume'), 'error'); }
    finally { setBusy(false); }
  };
  if (!status) {
    return <section className="settings-hero is-loading" aria-busy="true"><div className="settings-hero-orb" /><p className="muted">Reading the system status…</p></section>;
  }
  const headline = systemHeadline(status);
  const paused = status.worker.paused;
  return (
    <section className={clsx('settings-hero', `is-${headline.tone}`)} aria-label="System">
      <div className="settings-hero-main">
        <span className="settings-hero-orb" aria-hidden="true"><span /></span>
        <div className="settings-hero-text">
          <span className="settings-hero-eyebrow">System</span>
          <h2>{headline.title}</h2>
          <p className="settings-hero-meta">
            <span>Up {formatUptime(status.started_at, now)}</span>
            <span>{headline.detail}</span>
          </p>
        </div>
      </div>
      {paused ? (
        <div className="settings-pause-banner" role="status">
          <span>{pausedBanner(status.worker.running)}</span>
          <Button onClick={() => set(false)} disabled={busy}><Play size={13} /> Resume</Button>
        </div>
      ) : (
        <div className="settings-hero-action">
          <Button variant="ghost" className="settings-pause-btn" onClick={() => set(true)} disabled={busy}
                  title={help ?? undefined}>
            <Pause size={13} /> Pause everything new
          </Button>
          <small>Queued runs and services wait; what is executing finishes.</small>
        </div>
      )}
    </section>
  );
}

/* ─── Tiles ───────────────────────────────────────────────── */

function Tile({ icon, label, value, children, tone = 'default', action, className }: {
  icon: ReactNode; label: string; value: ReactNode; children?: ReactNode;
  tone?: 'default' | 'warning' | 'success'; action?: ReactNode; className?: string;
}) {
  return (
    <div className={clsx('sys-card', `sys-card--${tone}`, className)}>
      <div className="sys-card-top">
        <span className="sys-card-icon">{icon}</span>
        <span className="sys-card-label">{label}</span>
        {action}
      </div>
      <strong className="sys-card-value">{value}</strong>
      {children && <div className="sys-card-note">{children}</div>}
    </div>
  );
}

/** One cell per worker slot, filled for each run executing: how busy the
 *  pool is, at a glance. Capped so a pool of 64 still fits. */
function SlotMeter({ used, size }: { used: number; size: number }) {
  const cells = Math.min(size, 16);
  const filled = size > cells ? Math.round((used / size) * cells) : used;
  return (
    <div className="slot-meter" role="img" aria-label={`${used} of ${size} run slots in use`}>
      {Array.from({ length: cells }, (_, i) => <span key={i} className={clsx(i < filled && 'is-on')} />)}
    </div>
  );
}

function StatusTiles({ status, now, onRefresh }: { status: SystemStatus | null; now: number; onRefresh: () => void }) {
  if (!status) return <div className="sys-grid" aria-hidden="true">{[0, 1, 2, 3, 4].map(i => <div key={i} className="sys-card is-skeleton" />)}</div>;
  const { worker, services, storage, database } = status;
  const bar = storageSegments(storage);
  return (
    <div className="sys-grid settings-status" aria-label="Status">
      <Tile icon={<Cpu size={15} />} label="Worker" tone={worker.paused ? 'warning' : 'default'}
            value={worker.paused ? <>Paused <em>· {worker.running} finishing</em></> : <>{worker.running} <em>running</em></>}>
        <SlotMeter used={worker.running} size={worker.concurrency} />
        <span>{worker.concurrency} at once · {worker.queued} queued · {worker.waiting_approval} waiting approval</span>
      </Tile>
      <Tile icon={<Server size={15} />} label="Services" tone={services.supervised || services.total === 0 ? 'default' : 'warning'}
            value={<>{services.running} <em>of {services.total} running</em></>}>
        {services.total > 0 && (
          <div className="service-dots" aria-hidden="true">
            {Array.from({ length: Math.min(services.total, 16) }, (_, i) => <span key={i} className={clsx(i < services.running && 'is-on')} />)}
          </div>
        )}
        <span>{services.total === 0 ? 'None defined'
          : services.supervised ? 'Supervised' : 'Nothing is supervising them — start runrail serve or runrail worker'}</span>
      </Tile>
      <Tile icon={<CalendarClock size={15} />} label="Next scheduled run"
            value={status.scheduler.next_fire_at ? formatCountdown(status.scheduler.next_fire_at, now) : 'Nothing scheduled'}>
        <span>{status.scheduler.next_fire_at ? formatDate(status.scheduler.next_fire_at) : 'No enabled workflow has a schedule'}</span>
      </Tile>
      <Tile icon={<Database size={15} />} label="Database"
            value={<>{database.backend === 'postgresql' ? 'PostgreSQL' : database.backend === 'sqlite' ? 'SQLite' : database.backend}
              {database.size_bytes != null && <em> · {formatSize(database.size_bytes)}</em>}</>}>
        <PathChip path={database.location} what="database" />
      </Tile>
      <Tile icon={<HardDrive size={15} />} label="Home folder" className="sys-card--storage"
            value={<>{bar ? formatSize(bar.total) : 'Measuring…'}
              {storage.measured_at && <em> · measured {timeAgo(storage.measured_at, now)}</em>}</>}
            action={
              <button type="button" className="icon-button sys-card-action" onClick={onRefresh}
                      disabled={storage.measuring} aria-label="Measure storage again" title="Measure again">
                <RefreshCw size={12} className={storage.measuring ? 'icon-spin' : undefined} />
              </button>
            }>
        {bar ? (
          <>
            <div className="storage-bar" role="img"
                 aria-label={bar.segments.map(s => `${s.label} ${formatSize(s.bytes)}`).join(', ')}>
              {bar.segments.filter(s => s.share > 0).map(s => (
                <span key={s.key} className={`is-${s.key}`} style={{ flexGrow: s.share }} />
              ))}
            </div>
          </>
        ) : <div className="storage-bar is-measuring" />}
        <div className="sys-card-foot">
          {bar && (
            <ul className="storage-legend">
              {bar.segments.map(s => (
                <li key={s.key}><i className={`is-${s.key}`} />{s.label} <b>{formatSize(s.bytes)}</b></li>
              ))}
            </ul>
          )}
          <PathChip path={status.home} what="home folder" />
        </div>
      </Tile>
    </div>
  );
}

/* ─── Setting groups ──────────────────────────────────────── */

interface GroupProps { settings: SettingsMap; now: number; onSaved: (response: SettingsResponse) => void }

/** The save shared by every group: all-or-nothing, with the server's reason
 *  kept next to the group that was refused. */
function useGroupSave(onSaved: (response: SettingsResponse) => void) {
  const { toast } = useToast();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async (values: Partial<SettingValues>): Promise<boolean> => {
    setSaving(true); setError(null);
    try {
      onSaved(await put<SettingsResponse>('/settings', values));
      toast('Saved');
      return true;
    } catch (caught) {
      const message = reasonOf(caught, 'Could not save');
      setError(message);
      // A validation (422) or pinned (409) answer is the group's own business;
      // anything else — the server is away — deserves the toast as well.
      if (!(caught instanceof ApiError) || (caught.status !== 422 && caught.status !== 409)) toast(message, 'error');
      return false;
    } finally { setSaving(false); }
  };
  return { save, saving, error, setError };
}

function SettingField({ setting, now, htmlFor, children }: { setting: Setting; now: number; htmlFor?: string; children: ReactNode }) {
  const locked = lockedNote(setting);
  return (
    <div className={clsx('field settings-field', setting.locked && 'is-locked')}>
      <span>
        {htmlFor ? <label htmlFor={htmlFor}>{setting.title}</label> : setting.title}
        <em className={clsx('settings-provenance', `is-${setting.source}`)}>{provenance(setting, now)}</em>
      </span>
      {children}
      <small>{setting.help}</small>
      {locked && <small className="settings-locked">{locked}</small>}
    </div>
  );
}

function GroupFooter({ error, dirty, children }: { error: string | null; dirty: boolean; children: ReactNode }) {
  return (
    <div className="settings-actions">
      {error ? <p className="settings-error" role="alert">{error}</p>
        : dirty ? <p className="settings-dirty">Unsaved changes</p> : null}
      {children}
    </div>
  );
}

function Toggle({ id, checked, disabled, onChange, label }: {
  id: string; checked: boolean; disabled?: boolean; onChange: (next: boolean) => void; label: string;
}) {
  return (
    <label className="toggle">
      <input id={id} type="checkbox" checked={checked} disabled={disabled} aria-label={label}
             onChange={e => onChange(e.target.checked)} />
      <span />
    </label>
  );
}

/** − [n] + : the number stays typeable, the buttons step within range. */
function Stepper({ id, text, onText, min, max, disabled }: {
  id: string; text: string; onText: (next: string) => void; min: number; max: number; disabled?: boolean;
}) {
  const current = Number(text);
  const step = (delta: number) => {
    const base = Number.isInteger(current) ? current : min;
    onText(String(Math.min(max, Math.max(min, base + delta))));
  };
  return (
    <div className={clsx('stepper', disabled && 'is-disabled')}>
      <button type="button" onClick={() => step(-1)} disabled={disabled || current <= min} aria-label="Fewer"><Minus size={13} /></button>
      <input id={id} type="number" min={min} max={max} step={1} inputMode="numeric" value={text}
             disabled={disabled} onChange={e => onText(e.target.value)} />
      <button type="button" onClick={() => step(1)} disabled={disabled || current >= max} aria-label="More"><Plus size={13} /></button>
    </div>
  );
}

/** Only the keys the page may change go in a save; a pinned key would make
 *  the whole save a 409. */
function unlocked(settings: SettingsMap, values: Partial<SettingValues>): Partial<SettingValues> {
  const out: Partial<SettingValues> = {};
  for (const key of Object.keys(values) as SettingKey[]) {
    if (!settings.get(key)?.locked) Object.assign(out, { [key]: values[key] });
  }
  return out;
}

/* Workers: how much runs at once. */
function WorkersGroup({ settings, now, onSaved }: GroupProps) {
  const concurrency = settings.get('worker_concurrency');
  const parallelism = settings.get('task_parallelism');
  const [runsText, setRunsText] = useState(String(valueOf(settings, 'worker_concurrency') ?? ''));
  const [tasksText, setTasksText] = useState(String(valueOf(settings, 'task_parallelism') ?? ''));
  useEffect(() => { setRunsText(String(valueOf(settings, 'worker_concurrency') ?? '')); }, [concurrency?.value]);
  useEffect(() => { setTasksText(String(valueOf(settings, 'task_parallelism') ?? '')); }, [parallelism?.value]);
  const { save, saving, error, setError } = useGroupSave(onSaved);
  if (!concurrency || !parallelism) return null;
  const runs = parseCount(runsText, 1, 64);
  const tasks = parseCount(tasksText, 1, 64);
  const dirty = (runs.ok && runs.value !== concurrency.value) || (tasks.ok && tasks.value !== parallelism.value)
    || !runs.ok || !tasks.ok;
  const submit = () => {
    if (!runs.ok) { setError(`${concurrency.title}: ${runs.reason}`); return; }
    if (!tasks.ok) { setError(`${parallelism.title}: ${tasks.reason}`); return; }
    void save(unlocked(settings, { worker_concurrency: runs.value, task_parallelism: tasks.value }));
  };
  return (
    <section className="settings-section settings-group" aria-label="Workers">
      <SectionHead icon={<Cpu size={15} />} title="Workers">How much executes at once.</SectionHead>
      <div className="form-stack">
        <SettingField setting={concurrency} now={now} htmlFor="setting-worker-concurrency">
          <Stepper id="setting-worker-concurrency" text={runsText} onText={setRunsText} min={1} max={64} disabled={concurrency.locked} />
        </SettingField>
        <SettingField setting={parallelism} now={now} htmlFor="setting-task-parallelism">
          <Stepper id="setting-task-parallelism" text={tasksText} onText={setTasksText} min={1} max={64} disabled={parallelism.locked} />
        </SettingField>
      </div>
      <GroupFooter error={error} dirty={dirty && runs.ok && tasks.ok}>
        <Button onClick={submit} disabled={!dirty || saving || (concurrency.locked && parallelism.locked)}>{saving ? 'Saving…' : 'Save'}</Button>
      </GroupFooter>
    </section>
  );
}

/* Retention: how long finished runs are kept, with what a change would delete
   said before it can be saved. */
type RetentionChoice = 'forever' | 'custom' | number;

function RetentionGroup({ settings, now, onSaved, cleanup, last }: GroupProps & {
  cleanup?: CleanupState; last?: LastCleanup | null;
}) {
  const setting = settings.get('retention_days');
  const saved = valueOf(settings, 'retention_days');
  const choiceFor = (days: number | null | undefined): RetentionChoice =>
    days == null ? 'forever' : RETENTION_PRESETS.some(p => p.days === days) ? days : 'custom';
  const [choice, setChoice] = useState<RetentionChoice>(choiceFor(saved));
  const [daysText, setDaysText] = useState(saved == null ? '' : String(saved));
  useEffect(() => { setChoice(choiceFor(saved)); setDaysText(saved == null ? '' : String(saved)); }, [saved]);
  const [preview, setPreview] = useState<RetentionPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const { save, saving, error, setError } = useGroupSave(onSaved);

  const parsed = choice === 'forever' ? { ok: true as const, value: null }
    : typeof choice === 'number' ? { ok: true as const, value: choice } : parseDays(daysText);
  const wanted = parsed.ok ? parsed.value : undefined;
  // The preview for the choice on screen, ~400ms after the last change; an
  // older answer never lands over a newer question.
  // Asked again when a cleanup finishes: what it would delete has changed.
  const cleaned = cleanup && !cleanup.running ? cleanup.finished_at : null;
  const ticket = useRef(0);
  useEffect(() => {
    const mine = ++ticket.current;
    setPreview(null); setPreviewError(null);
    if (wanted == null) return;
    const timer = window.setTimeout(() => {
      void api<RetentionPreview>(`/system/retention-preview?days=${wanted}`)
        .then(result => { if (ticket.current === mine) setPreview(result); })
        .catch(caught => { if (ticket.current === mine) setPreviewError(reasonOf(caught, 'Could not preview')); });
    }, PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [wanted, cleaned]);
  const cleaning = cleanupLine(cleanup, now);
  // The fresh notice says it louder; otherwise the record says it quietly.
  const lastLine = cleaning ? null : lastCleanupLine(last, now);

  if (!setting) return null;
  const dirty = !parsed.ok || wanted !== saved;
  const previewReady = wanted == null || (preview != null && preview.days === wanted);
  const pick = (next: RetentionChoice) => {
    setChoice(next);
    if (typeof next === 'number') setDaysText(String(next));
    if (next === 'custom' && !daysText) setDaysText(String(saved ?? 60));
  };
  const submit = () => {
    if (!parsed.ok) { setError(`${setting.title}: ${parsed.reason}`); return; }
    if (preview && preview.runs > 0 && !confirm(`${retentionSentence(preview)}. Finished runs older than ${preview.days} days are deleted with their logs and artifacts now, and every six hours after. Continue?`)) return;
    void save({ retention_days: parsed.value });
  };
  const shown = preview && preview.days === wanted ? preview : null;
  // The saved figure is a status, not a warning; only a new choice is.
  const settled = !dirty && wanted != null;
  const options: { value: RetentionChoice; label: string }[] = [
    { value: 'forever', label: 'Forever' }, ...RETENTION_PRESETS.map(p => ({ value: p.days, label: p.label })),
    { value: 'custom', label: 'Custom' },
  ];
  return (
    <section className="settings-section settings-group" aria-label="Retention">
      <SectionHead icon={<Trash2 size={15} />} title="Retention" tone={saved == null ? 'accent' : 'warning'}>
        How long finished runs are kept.
      </SectionHead>
      <div className="form-stack">
        <SettingField setting={setting} now={now}>
          <div className="segmented" role="radiogroup" aria-label="Keep runs for">
            {options.map(option => (
              <label key={String(option.value)} className={clsx('segmented-option', choice === option.value && 'is-on')}>
                <input type="radio" name="retention" checked={choice === option.value} disabled={setting.locked}
                       onChange={() => pick(option.value)} />
                {option.label}
              </label>
            ))}
          </div>
          {choice === 'custom' && (
            <div className="settings-inline-field">
              <input id="setting-retention-days" type="number" min={1} step={1} inputMode="numeric" value={daysText}
                     aria-label="Days to keep runs" disabled={setting.locked} onChange={e => setDaysText(e.target.value)} />
              <span className="settings-unit">days</span>
            </div>
          )}
        </SettingField>
        {cleaning && (
          <p className={clsx('retention-cleanup', `is-${cleaning.tone}`)} role="status">
            {cleaning.tone === 'busy' ? <RefreshCw size={12} className="icon-spin" /> : <Check size={12} />}
            {cleaning.text}
          </p>
        )}
        {lastLine && <p className="retention-last">{lastLine}</p>}
        {!setting.locked && !cleanup?.running && (
          <div className={clsx('retention-card', !settled && shown && shown.runs > 0 && 'is-destructive',
                               wanted === null && 'is-forever', settled && 'is-settled')}>
            {shown && !settled && (
              <div className="retention-figures" aria-hidden="true">
                <div><b>{shown.partial ? '≥ ' : ''}{shown.runs.toLocaleString('en-US')}</b><span>runs deleted</span></div>
                <div><b>{shown.partial ? '≥ ' : ''}{formatSize(shown.bytes)}</b><span>freed</span></div>
                <div><b>{shown.runs_kept.toLocaleString('en-US')}</b><span>runs kept</span></div>
              </div>
            )}
            <p className="retention-preview" role="status">
              {wanted === null ? 'Nothing is ever deleted.'
                : !parsed.ok ? parsed.reason
                : previewError ? `Could not preview: ${previewError}`
                : shown ? (settled ? settledSentence(shown) : retentionSentence(shown))
                : 'Working out what this would delete…'}
            </p>
          </div>
        )}
      </div>
      <GroupFooter error={error} dirty={dirty && parsed.ok}>
        <Button onClick={submit} disabled={!dirty || saving || setting.locked || !previewReady}>{saving ? 'Saving…' : 'Save'}</Button>
      </GroupFooter>
    </section>
  );
}

/** The 24 hours with the quiet span shaded. */
function QuietBar({ start, end }: { start: string; end: string }) {
  const spans = quietSpans(start, end);
  return (
    <div className="quiet-bar" aria-hidden="true">
      <div className="quiet-track">
        {spans.map(([from, to], i) => <span key={i} style={{ left: `${from * 100}%`, width: `${(to - from) * 100}%` }} />)}
      </div>
      <div className="quiet-ticks"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>
    </div>
  );
}

/* Notifications: where alerts go, which ones, and when not to. */
function NotificationsGroup({ settings, eventKinds, now, onSaved }: GroupProps & { eventKinds: string[] }) {
  const webhook = settings.get('notify_webhook_url');
  const events = settings.get('notify_events');
  const quiet = settings.get('quiet_hours');
  const savedUrl = valueOf(settings, 'notify_webhook_url') ?? null;
  const savedEvents = valueOf(settings, 'notify_events') ?? [];
  const savedQuiet = valueOf(settings, 'quiet_hours') ?? null;
  const [urlText, setUrlText] = useState(savedUrl ?? '');
  const [chosen, setChosen] = useState<string[]>(savedEvents);
  const [quietOn, setQuietOn] = useState(savedQuiet != null);
  const [start, setStart] = useState(savedQuiet?.start ?? '22:00');
  const [end, setEnd] = useState(savedQuiet?.end ?? '07:00');
  const [zone, setZone] = useState(savedQuiet?.timezone ?? viewerZone());
  useEffect(() => { setUrlText(savedUrl ?? ''); }, [savedUrl]);
  useEffect(() => { setChosen(savedEvents); }, [events?.value]);
  useEffect(() => {
    setQuietOn(savedQuiet != null);
    if (savedQuiet) { setStart(savedQuiet.start); setEnd(savedQuiet.end); setZone(savedQuiet.timezone); }
  }, [quiet?.value]);
  const zones = useMemo(() => typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : [], []);
  const groups = useMemo(() => groupEvents(eventKinds), [eventKinds]);
  const { save, saving, error } = useGroupSave(onSaved);
  const [test, setTest] = useState<{ busy: boolean; result?: NotifyTestResult | string }>({ busy: false });

  if (!webhook || !events || !quiet) return null;
  const quietValue: QuietHours | null = quietOn ? { start, end, timezone: zone.trim() || 'UTC' } : null;
  const values: Partial<SettingValues> = { notify_webhook_url: urlText.trim() || null, notify_events: chosen, quiet_hours: quietValue };
  const dirty = !sameValue(values.notify_webhook_url, webhook.value) || !sameValue(chosen, events.value)
    || !sameValue(quietValue, quiet.value);
  const toggleKind = (kind: string, on: boolean) =>
    setChosen(prev => on ? [...prev.filter(k => k !== kind), kind] : prev.filter(k => k !== kind));
  const sendTest = async () => {
    setTest({ busy: true });
    try { setTest({ busy: false, result: await post<NotifyTestResult>('/settings/notify-test', {}) }); }
    catch (caught) { setTest({ busy: false, result: reasonOf(caught, 'Could not send') }); }
  };
  const r = test.result;
  const allOn = eventKinds.every(kind => chosen.includes(kind));
  return (
    <section className="settings-section settings-group settings-group--wide" aria-label="Notifications">
      <SectionHead icon={<Bell size={15} />} title="Notifications">
        Where alerts go, which ones, and when to keep quiet. Everything still shows in Activity.
      </SectionHead>
      <div className="notify-layout">
        <div className="form-stack">
          <SettingField setting={webhook} now={now} htmlFor="setting-webhook-url">
            <input id="setting-webhook-url" type="url" value={urlText} disabled={webhook.locked} placeholder="https://hooks.slack.com/services/…"
                   onChange={e => setUrlText(e.target.value)} />
            <div className="webhook-test">
              <button type="button" className="btn btn-ghost btn-sm" disabled={test.busy || !savedUrl} onClick={sendTest}
                      title={savedUrl ? 'Post a test message to the saved webhook' : 'Save a webhook first'}>
                <Bell size={12} /> {test.busy ? 'Sending…' : 'Send a test message'}
              </button>
              {r && (typeof r === 'string'
                ? <small className="webhook-test-result is-bad">{r}</small>
                : r.ok
                  ? <small className="webhook-test-result is-ok">Delivered to {r.url_host}.</small>
                  : <small className="webhook-test-result is-bad">{r.url_host} answered: {r.error}</small>)}
              {!r && urlText.trim() && urlText.trim() !== savedUrl && <small className="muted">Save first to test this URL.</small>}
            </div>
          </SettingField>
          <SettingField setting={quiet} now={now}>
            <div className="settings-toggle-row">
              <span className="settings-hint">
                {quietOn ? <><BellOff size={12} /> Nothing is posted from {start} to {end} ({zone || 'UTC'})</> : 'Off — alerts go out around the clock'}
              </span>
              <Toggle id="setting-quiet-hours" label={quiet.title} checked={quietOn} onChange={setQuietOn} />
            </div>
            {quietOn && (
              <>
                <QuietBar start={start} end={end} />
                <div className="settings-quiet">
                  <label className="field"><span>From</span>
                    <input type="time" step={60} value={start} onChange={e => setStart(e.target.value)} aria-label="Quiet hours start" /></label>
                  <label className="field"><span>Until</span>
                    <input type="time" step={60} value={end} onChange={e => setEnd(e.target.value)} aria-label="Quiet hours end" /></label>
                  <label className="field"><span>Timezone</span>
                    <input type="text" list="settings-zones" value={zone} onChange={e => setZone(e.target.value)} aria-label="Quiet hours timezone" />
                  </label>
                  {zones.length > 0 && <datalist id="settings-zones">{zones.map(z => <option key={z} value={z} />)}</datalist>}
                </div>
              </>
            )}
          </SettingField>
        </div>
        <SettingField setting={events} now={now}>
          <div className="event-head">
            <span className="settings-hint">{chosen.length} of {eventKinds.length} kinds go out</span>
            <button type="button" className="text-button" onClick={() => setChosen(allOn ? [] : [...eventKinds])}>
              {allOn ? 'None' : 'All'}
            </button>
          </div>
          <div className="event-groups" role="group" aria-label={events.title}>
            {groups.map(group => (
              <div key={group.title} className="event-group">
                <span className="event-group-title">{group.title}</span>
                <div className="event-chips">
                  {group.kinds.map(kind => {
                    const on = chosen.includes(kind);
                    return (
                      <label key={kind} className={clsx('event-chip', on && 'is-on')}>
                        <input type="checkbox" checked={on} onChange={e => toggleKind(kind, e.target.checked)} />
                        <span className="event-chip-box">{on && <Check size={11} />}</span>
                        {eventLabel(kind)}
                      </label>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </SettingField>
      </div>
      <GroupFooter error={error} dirty={dirty}>
        <Button onClick={() => save(unlocked(settings, values))} disabled={!dirty || saving}>{saving ? 'Saving…' : 'Save'}</Button>
      </GroupFooter>
    </section>
  );
}

function CodeTrackingGroup({ settings, now, onSaved }: GroupProps) {
  const setting = settings.get('code_tracking');
  const saved = (valueOf(settings, 'code_tracking') ?? 'auto') as CodeTracking;
  const [choice, setChoice] = useState<CodeTracking>(saved);
  useEffect(() => { setChoice(saved); }, [saved]);
  const { save, saving, error } = useGroupSave(onSaved);
  if (!setting) return null;
  const dirty = choice !== saved;
  return (
    <section className="settings-section settings-group settings-group--wide" aria-label="Code tracking">
      <SectionHead icon={<FileCode2 size={15} />} title="Code tracking">
        What each task records of its code, so a failed run can show what changed since the last good one.
        Read-only: nothing in your folders is written, and .env files, keys and credentials are never read.
      </SectionHead>
      <SettingField setting={setting} now={now}>
        <div className="tracking-options" role="radiogroup" aria-label={setting.title}>
          {CODE_TRACKING_OPTIONS.map(option => (
            <label key={option.value} className={clsx('tracking-option', choice === option.value && 'is-on')}>
              <input type="radio" name="code-tracking" value={option.value} checked={choice === option.value}
                     disabled={setting.locked} onChange={() => setChoice(option.value)} />
              <strong>{option.title}</strong>
              <span>{option.text}</span>
            </label>
          ))}
        </div>
      </SettingField>
      <GroupFooter error={error} dirty={dirty}>
        <Button onClick={() => save(unlocked(settings, { code_tracking: choice }))} disabled={!dirty || saving || setting.locked}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </GroupFooter>
    </section>
  );
}

/* ─── Variables & secrets ─────────────────────────────────── */

/** Sends JSON with any method; api() sets the content type. */
const send = <T,>(path: string, method: 'POST' | 'PATCH' | 'DELETE', body?: unknown) =>
  api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

function VariablesSection() {
  const [data, setData] = useState<VariablesResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  useEffect(() => {
    void api<VariablesResponse>('/variables').then(next => { setData(next); setLoadError(null); })
      .catch(error => setLoadError(reasonOf(error, 'Could not load variables')));
  }, []);
  const rows = data?.variables ?? [];
  const names = rows.map(r => r.name);
  const secrets = rows.filter(r => r.secret).length;
  const key = data?.key;
  return (
    <section className="settings-section settings-section--wide settings-vars" aria-label="Variables and secrets">
      <SectionHead icon={<KeyRound size={15} />} title="Variables & secrets">
        Every task and service gets these as environment variables. An environment&apos;s or a service&apos;s own variables win over them.
      </SectionHead>
      {loadError && <p className="settings-error" role="alert">{loadError}</p>}
      {key?.problem && <div className="callout callout--warning" role="alert">{keyLine(key)}</div>}
      <AddVariable taken={names} onSaved={setData} />
      {data && (rows.length === 0 ? (
        <p className="vars-empty">Nothing yet. Add a database host, a folder, an API token — anything your scripts read from the environment.</p>
      ) : (
        <ul className="vars-list" aria-label="Saved variables">
          {rows.map(row => <VariableItem key={row.id} row={row} onSaved={setData} />)}
        </ul>
      ))}
      {key && (
        <div className="vars-foot">
          <p className="vars-foot-line">
            <Lock size={12} /> {secrets > 0 || key.source ? keyLine(key) : 'Secrets are stored encrypted.'} They show as <code>{MASK}</code> in logs, notebooks and reports.
            {key.source === 'file' && <> <PathChip path={key.path} what="secrets key" /></>}
          </p>
          <p className="vars-foot-line">
            Read them as <code>$NAME</code>, <code>%NAME%</code> in Windows commands, or <code>os.environ[&quot;NAME&quot;]</code> in Python.
          </p>
        </div>
      )}
    </section>
  );
}

function KindSwitch({ secret, onChange }: { secret: boolean; onChange: (secret: boolean) => void }) {
  return (
    <div className="vars-kind" role="group" aria-label="Kind">
      <button type="button" aria-pressed={!secret} className={clsx(!secret && 'is-on')} onClick={() => onChange(false)}>Variable</button>
      <button type="button" aria-pressed={secret} className={clsx(secret && 'is-on')} onClick={() => onChange(true)}><Lock size={11} /> Secret</button>
    </div>
  );
}

function ValueInput({ id, value, onChange, secret, placeholder, autoFocus }: {
  id: string; value: string; onChange: (value: string) => void; secret: boolean; placeholder: string; autoFocus?: boolean;
}) {
  const [shown, setShown] = useState(false);
  return (
    <div className="vars-value-input">
      <input id={id} type={secret && !shown ? 'password' : 'text'} value={value} placeholder={placeholder}
             autoComplete="off" spellCheck={false} autoFocus={autoFocus} onChange={e => onChange(e.target.value)} />
      {secret && (
        <button type="button" className="vars-peek" onClick={() => setShown(s => !s)}
                aria-label={shown ? 'Hide what you typed' : 'Show what you typed'} title={shown ? 'Hide' : 'Show what you typed'}>
          {shown ? <EyeOff size={13} /> : <Eye size={13} />}
        </button>
      )}
    </div>
  );
}

function AddVariable({ taken, onSaved }: { taken: string[]; onSaved: (next: VariablesResponse) => void }) {
  const { toast } = useToast();
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [note, setNote] = useState('');
  const [secret, setSecret] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = variableNameProblem(name, taken) ?? secretValueProblem(value, secret);
  const ready = name.trim() !== '' && !problem && !busy && (!secret || value !== '');
  const submit = async (e: { preventDefault: () => void }) => {
    e.preventDefault();
    if (!ready) return;
    setBusy(true); setError(null);
    try {
      const saved = await send<VariablesResponse>('/variables', 'POST',
        { name: name.trim(), value, secret, description: note.trim() || null });
      onSaved(saved);
      toast(secret ? `Secret ${name.trim()} saved` : `Variable ${name.trim()} saved`);
      setName(''); setValue(''); setNote('');
    } catch (caught) { setError(reasonOf(caught, 'Could not save')); }
    finally { setBusy(false); }
  };
  return (
    <form className="vars-add" onSubmit={submit} aria-label="Add a variable">
      <KindSwitch secret={secret} onChange={setSecret} />
      <label className="field vars-add-name"><span>Name</span>
        <input value={name} placeholder={secret ? 'DB_PASSWORD' : 'DATA_DIR'} autoComplete="off" spellCheck={false}
               onChange={e => setName(e.target.value)} />
      </label>
      <label className="field vars-add-value" htmlFor="new-variable-value"><span>Value</span>
        <ValueInput id="new-variable-value" value={value} onChange={setValue} secret={secret}
                    placeholder={secret ? 'Shown only while you type it' : '/srv/data'} />
      </label>
      <label className="field vars-add-note"><span>Note <em>optional</em></span>
        <input value={note} placeholder="What it is for" onChange={e => setNote(e.target.value)} />
      </label>
      <Button type="submit" disabled={!ready}><Plus size={13} /> {busy ? 'Saving…' : 'Add'}</Button>
      {(error || (name.trim() && problem)) && <p className="settings-error vars-add-error" role="alert">{error ?? problem}</p>}
    </form>
  );
}

function VariableItem({ row, onSaved }: { row: VariableRow; onSaved: (next: VariablesResponse) => void }) {
  const { toast } = useToast();
  const now = useNow(true, 60_000);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const start = () => { setValue(row.secret ? '' : row.value ?? ''); setError(null); setEditing(true); };
  const run = async (action: () => Promise<VariablesResponse>, done: string) => {
    setBusy(true); setError(null);
    try { onSaved(await action()); setEditing(false); toast(done); }
    catch (caught) { setError(reasonOf(caught, 'Could not save')); }
    finally { setBusy(false); }
  };
  const save = () => run(() => send<VariablesResponse>(`/variables/${row.id}`, 'PATCH', { value }),
                         row.secret ? `Secret ${row.name} replaced` : `Variable ${row.name} saved`);
  const makeSecret = () => {
    if (!confirm(`Make ${row.name} a secret? Its value will be encrypted, hidden in logs, and never shown again.`)) return;
    void run(() => send<VariablesResponse>(`/variables/${row.id}`, 'PATCH', { secret: true }), `${row.name} is now a secret`);
  };
  const remove = () => {
    if (!confirm(`Delete ${row.name}? Tasks and services started from now on will not get it.`)) return;
    void run(() => send<VariablesResponse>(`/variables/${row.id}`, 'DELETE'), `${row.name} deleted`);
  };
  const valueProblem = secretValueProblem(value, row.secret);
  const override = overrideLine(row.overridden_in);
  return (
    <li className={clsx('vars-item', row.secret && 'is-secret', row.problem && 'has-problem')}>
      <div className="vars-item-name">
        <code>{row.name}</code>
        {row.secret && <span className="vars-badge"><Lock size={10} /> Secret</span>}
        {row.description && <span className="vars-note">{row.description}</span>}
      </div>
      <div className="vars-item-value">
        {editing ? (
          <form className="vars-edit" onSubmit={e => { e.preventDefault(); if (!valueProblem && !busy) void save(); }}>
            <ValueInput id={`variable-${row.id}`} value={value} onChange={setValue} secret={row.secret} autoFocus
                        placeholder={row.secret ? 'The new value' : ''} />
            <Button type="submit" size="sm" disabled={busy || !!valueProblem || (row.secret && value === '')}>{busy ? 'Saving…' : 'Save'}</Button>
            <Button variant="ghost" size="sm" onClick={() => setEditing(false)} disabled={busy}>Cancel</Button>
          </form>
        ) : row.secret ? (
          row.problem
            ? <span className="vars-problem">Not set for tasks: {row.problem}. Enter it again.</span>
            : <span className="vars-masked" title="The value is never shown again"><span aria-hidden="true">{MASK}</span> <small>set {timeAgo(row.updated_at, now)}</small></span>
        ) : (
          <span className="vars-plain" title={row.value ?? ''}>{row.value === '' ? <em>empty</em> : row.value}</span>
        )}
        {(error || (editing && valueProblem)) && <small className="settings-error" role="alert">{error ?? valueProblem}</small>}
        {override && !editing && <small className="vars-override">{override}</small>}
      </div>
      {!editing && (
        <div className="vars-item-actions">
          <button type="button" className="icon-button" onClick={start} disabled={busy}
                  title={row.secret ? 'Replace the value' : 'Edit the value'} aria-label={row.secret ? `Replace ${row.name}` : `Edit ${row.name}`}>
            <Pencil size={13} />
          </button>
          {!row.secret && (
            <button type="button" className="icon-button" onClick={makeSecret} disabled={busy}
                    title="Make it a secret" aria-label={`Make ${row.name} a secret`}><Lock size={13} /></button>
          )}
          <button type="button" className="icon-button is-danger" onClick={remove} disabled={busy}
                  title="Delete" aria-label={`Delete ${row.name}`}><Trash2 size={13} /></button>
        </div>
      )}
    </li>
  );
}

/* ─── Backup & restore ────────────────────────────────────── */

function BackupSection({ home }: { home: string | null }) {
  const { toast } = useToast();
  const [withKey, setWithKey] = useState(false);
  const [points, setPoints] = useState<{ points: RestorePoint[]; directory: string } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<BackupPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [summary, setSummary] = useState<RestoreSummary | null>(null);
  const [busy, setBusy] = useState<'preview' | 'restore' | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void api<{ points: RestorePoint[]; directory: string }>('/system/restore-points').then(setPoints).catch(() => {});
  }, []);

  const inspect = async (chosen: File | null) => {
    setFile(chosen); setPreview(null); setPreviewError(null); setSummary(null);
    if (!chosen) return;
    setBusy('preview');
    try { setPreview(await uploadZip<BackupPreview>('/system/restore/preview', chosen)); }
    catch (error) { setPreviewError(reasonOf(error, 'Could not read the backup')); }
    finally { setBusy(null); }
  };
  const pick = (e: ChangeEvent<HTMLInputElement>) => void inspect(e.target.files?.[0] ?? null);
  const drop = (e: DragEvent<HTMLLabelElement>) => {
    e.preventDefault(); setDragging(false);
    if (busy == null) void inspect(e.dataTransfer.files?.[0] ?? null);
  };
  const restore = async () => {
    if (!file || !preview) return;
    if (!confirm('Restore the configuration from this backup? Workflows, services, projects and environments in it are created or updated; nothing is deleted.')) return;
    setBusy('restore');
    try {
      const result = await uploadZip<{ summary: RestoreSummary }>('/system/restore', file);
      setSummary(result.summary);
      toast('Configuration restored');
    } catch (error) { toast(reasonOf(error, 'Could not restore'), 'error'); }
    finally { setBusy(null); }
  };
  const reset = () => {
    setFile(null); setPreview(null); setPreviewError(null); setSummary(null);
    if (fileInput.current) fileInput.current.value = '';
  };

  const counts = preview?.manifest.counts ?? {};
  const figure = (key: 'workflows' | 'services' | 'projects' | 'environments') =>
    counts[key] ?? preview?.configuration[key] ?? 0;
  return (
    <section className="settings-section settings-section--wide settings-backup" aria-label="Backup and restore">
      <SectionHead icon={<Archive size={15} />} title="Backup & restore" tone="success">
        Take your configuration and run history with you, and bring them back.
      </SectionHead>
      <div className="backup-layout">
        <div className="backup-card">
          <span className="backup-card-icon"><FileArchive size={20} /></span>
          <div className="backup-card-body">
            <strong>Download a backup</strong>
            <ul className="backup-contents">
              <li><Check size={12} /> Workflows, services, projects and environments</li>
              <li><Check size={12} /> A consistent snapshot of the database, secrets encrypted</li>
              <li className="is-out"><Minus size={12} /> Logs and artifacts — copy {home ? <PathChip path={home} what="home folder" /> : 'the home folder'} for those</li>
            </ul>
            <label className="backup-key-option">
              <input type="checkbox" checked={withKey} onChange={e => setWithKey(e.target.checked)} />
              <span>
                <strong>Include the secrets key</strong>
                <small>{withKey
                  ? 'Anyone with this file can read your secrets. Keep it like a password.'
                  : 'Without it, the secrets in this backup open only here.'}</small>
              </span>
            </label>
            <a className="btn btn-primary btn-md" href={withKey ? '/api/system/backup?include_key=true' : '/api/system/backup'} download>
              <Download size={14} /> Download backup
            </a>
          </div>
        </div>

        <div className="restore-pane">
          {!preview ? (
            <label htmlFor="restore-file"
                   className={clsx('dropzone', dragging && 'is-dragging', busy === 'preview' && 'is-busy')}
                   onDragOver={e => { e.preventDefault(); setDragging(true); }}
                   onDragLeave={() => setDragging(false)} onDrop={drop}>
              <Upload size={18} />
              <strong>{busy === 'preview' ? 'Reading the backup…' : 'Restore from a backup'}</strong>
              <span>Drop a zip here, or click to choose one. It is inspected before anything is applied.</span>
              <input id="restore-file" ref={fileInput} type="file" accept=".zip,application/zip" onChange={pick}
                     disabled={busy != null} />
            </label>
          ) : (
            <div className="restore-manifest">
              <div className="restore-manifest-head">
                <FileArchive size={16} />
                <div>
                  <strong>{file?.name ?? 'Backup'}</strong>
                  <span>Made {preview.manifest.created_at ? formatDate(preview.manifest.created_at) : 'at an unknown time'} by RunRail {preview.manifest.runrail_version ?? '?'}</span>
                </div>
              </div>
              <div className="restore-figures">
                <div><b>{figure('workflows')}</b><span>workflows</span></div>
                <div><b>{figure('services')}</b><span>services</span></div>
                <div><b>{figure('projects')}</b><span>projects</span></div>
                <div><b>{figure('environments')}</b><span>environments</span></div>
                {counts.runs != null && <div><b>{counts.runs.toLocaleString('en-US')}</b><span>runs</span></div>}
              </div>
              <p className="settings-hint">
                {preview.includes_database ? 'Includes a database snapshot.' : 'Configuration only — no database inside.'}
                {(counts.secrets ?? 0) > 0 && (preview.includes_secrets_key
                  ? ` ${counts.secrets} secret${counts.secrets === 1 ? '' : 's'}, with the key to open them.`
                  : ` ${counts.secrets} secret${counts.secrets === 1 ? '' : 's'}, without the key: they come back with the database only where they were made.`)}
              </p>
              {!summary ? (
                <div className="settings-inline-actions">
                  <Button onClick={restore} disabled={busy != null}><Upload size={13} /> {busy === 'restore' ? 'Restoring…' : 'Restore configuration'}</Button>
                  <Button variant="ghost" onClick={reset} disabled={busy != null}>Choose another</Button>
                </div>
              ) : (
                <div className="restore-summary" role="status">
                  <strong><ShieldCheck size={13} /> Restored: {restoreSummaryLine(summary)}</strong>
                  {Object.entries(summary).filter(([, names]) => names.length > 0).map(([bucket, names]) => (
                    <div key={bucket} className="muted">{bucket}: {names.join(', ')}</div>
                  ))}
                  <div><Button variant="ghost" size="sm" onClick={reset}>Done</Button></div>
                </div>
              )}
            </div>
          )}
          {previewError && <p className="settings-error" role="alert">{previewError}</p>}
          <p className="settings-hint">
            Here only the configuration is applied, and nothing is deleted. Run history comes back offline, with RunRail stopped:
          </p>
          <pre className="settings-command"><code>runrail restore {file?.name ?? 'runrail-backup.zip'}</code></pre>
        </div>
      </div>

      <div className="restore-points">
        <span className="event-group-title">Automatic restore points</span>
        {points == null ? <p className="muted">Loading…</p> : points.points.length === 0 ? (
          <p className="settings-hint">None yet. RunRail takes one before every schema upgrade.</p>
        ) : (
          <ol className="restore-timeline">
            {points.points.map(p => (
              <li key={p.name}>
                <i />
                <span className="restore-point-name" title={p.name}>{p.name.replace(/^runrail-/, '').replace(/\.db$/, '')}</span>
                <span className="muted">{formatSize(p.size_bytes)} · {timeAgo(p.created_at)}</span>
              </li>
            ))}
          </ol>
        )}
        {points && <div className="restore-points-foot"><span className="settings-hint">Taken before every schema upgrade; the newest three are kept in</span> <PathChip path={points.directory} what="restore points folder" /></div>}
      </div>
    </section>
  );
}

/* ─── Appearance ──────────────────────────────────────────── */

function AppearanceSection() {
  const [theme, setTheme] = useState<string>(() => document.documentElement.dataset.theme ?? 'dark');
  const switchTheme = (t: string) => {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem('runrail-theme', t); } catch { /* private window: this session only */ }
    setTheme(t);
  };
  return (
    <section className="settings-section settings-section--wide" aria-label="Appearance">
      <SectionHead icon={theme === 'dark' ? <Moon size={15} /> : <Sun size={15} />} title="Appearance">
        Saved in this browser.
      </SectionHead>
      <div className="theme-cards" role="radiogroup" aria-label="Theme">
        {(['dark', 'light'] as const).map(t => (
          <label key={t} className={clsx('theme-card', `theme-card--${t}`, theme === t && 'is-on')}>
            <input type="radio" name="theme" checked={theme === t} onChange={() => switchTheme(t)} />
            <span className="theme-preview" aria-hidden="true">
              <span className="theme-preview-side" />
              <span className="theme-preview-main"><i /><i /><i /></span>
            </span>
            <span className="theme-card-label">{t === 'dark' ? <><Moon size={12} /> Dark</> : <><Sun size={12} /> Light</>}</span>
          </label>
        ))}
      </div>
    </section>
  );
}

/* ─── Footer: the version and friends, quietly ───────────── */

function SettingsFooter({ status }: { status: SystemStatus }) {
  return (
    <footer className="settings-footer">
      <span>RunRail v{status.version}</span>
      <span>Python {status.python}</span>
      <span>{status.platform}</span>
      <span>API {window.location.origin}/api</span>
      <a href={GITHUB} target="_blank" rel="noreferrer">GitHub <ExternalLink size={11} /></a>
    </footer>
  );
}
