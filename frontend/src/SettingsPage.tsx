import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from 'react';
import {
  Activity, Archive, Bell, CalendarClock, Clock, Cpu, Database, Download, FolderOpen, Globe,
  HardDrive, Info, Package, Palette, Pause, Play, RefreshCw, Server, SlidersHorizontal, Trash2, Upload,
} from 'lucide-react';
import clsx from 'clsx';
import { ApiError, api, errorMessage, post, put } from './api';
import { useNow, useVisibleInterval } from './hooks';
import { formatDate, timeAgo, viewerZone } from './format';
import { Button, PageHeader } from './components/ui';
import { useToast } from './components/toast';
import {
  eventLabel, formatCountdown, formatSize, formatUptime, lockedNote, parseCount, parseDays,
  pausedBanner, provenance, restoreSummaryLine, retentionSentence, sameValue, updateLine,
  type BackupPreview, type NotifyTestResult, type QuietHours, type RestorePoint,
  type RestoreSummary, type RetentionPreview, type Setting, type SettingKey,
  type SettingValues, type SettingsResponse, type SystemStatus,
} from './settingsModel';

/* ─── Settings ─────────────────────────────────────────────
   The operator's panel: what this RunRail is (version, uptime, where its data
   is and how big), the one switch that pauses everything new, backups and
   restores, and the settings that apply without a restart. Every setting the
   environment pins is shown locked and names the variable; every save is per
   group and all-or-nothing, with the server's reason shown next to the group
   it refused. */

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
  // Storage sizes and the PyPI answer arrive from a background measurement;
  // while one is running, ask again sooner so the dashes fill in.
  const awaiting = status != null && (status.storage.measuring
    || (status.update.enabled && status.update.latest == null && status.update.error == null));
  useVisibleInterval(awaiting, () => loadStatus(), MEASURING_POLL_MS);

  // A save changes what the worker does next; the status cards should agree.
  const onSaved = useCallback((response: SettingsResponse) => { applySettings(response); loadStatus(); }, [applySettings, loadStatus]);

  return (
    <div className="settings-page">
      <PageHeader
        eyebrow="SYSTEM" title="Settings"
        subtitle="What this RunRail is doing, and the settings that apply without a restart."
        action={<a className="btn btn-ghost btn-sm" href={GITHUB} target="_blank" rel="noreferrer"><Info size={13} /> GitHub</a>}
      />
      {statusError && !status && (
        <div className="callout callout--warning" role="alert">Could not read the system status: {statusError}</div>
      )}
      <StatusSection status={status} now={now} onRefresh={() => loadStatus(true)} />
      <PauseSection status={status} help={settings?.get('paused')?.help ?? null}
                    onChanged={next => { setStatus(next); loadSettings(); }} />
      <BackupSection home={status?.home ?? null} />
      {settings && (
        <div className="settings-grid">
          <GeneralGroup settings={settings} now={now} onSaved={onSaved} />
          <WorkersGroup settings={settings} now={now} onSaved={onSaved} />
          <RetentionGroup settings={settings} now={now} onSaved={onSaved} />
          <NotificationsGroup settings={settings} eventKinds={eventKinds} now={now} onSaved={onSaved} />
        </div>
      )}
      <AppearanceSection />
    </div>
  );
}

/* ─── Status ──────────────────────────────────────────────── */

function StatusCard({ icon, label, value, note, tone = 'default', action }: {
  icon: ReactNode; label: string; value: ReactNode; note?: ReactNode;
  tone?: 'default' | 'warning'; action?: ReactNode;
}) {
  return (
    <div className={clsx('sys-card', `sys-card--${tone}`)}>
      <span className="sys-card-icon">{icon}</span>
      <div className="sys-card-body">
        <span className="sys-card-label">{label}</span>
        <strong className="sys-card-value">{value}</strong>
        {note && <span className="sys-card-note">{note}</span>}
      </div>
      {action}
    </div>
  );
}

function StatusSection({ status, now, onRefresh }: { status: SystemStatus | null; now: number; onRefresh: () => void }) {
  if (!status) {
    return (
      <section className="settings-section settings-section--wide" aria-busy="true">
        <h3><Activity size={14} /> Status</h3>
        <p className="muted">Loading…</p>
      </section>
    );
  }
  const { worker, services, storage, disk, database, update } = status;
  const updateNote = updateLine(update, status.version);
  const storageTotal = [storage.logs_bytes, storage.artifacts_bytes, storage.environments_bytes, storage.backups_bytes]
    .reduce<number | null>((sum, part) => (sum == null || part == null ? null : sum + part), 0);
  const storageParts = ([['logs', storage.logs_bytes], ['artifacts', storage.artifacts_bytes],
    ['environments', storage.environments_bytes], ['backups', storage.backups_bytes]] as const)
    .map(([name, bytes]) => `${name} ${formatSize(bytes)}`).join(' · ');
  const workerNote = `${worker.queued} queued · ${worker.waiting_approval} waiting approval · ${worker.concurrency} at once, ${worker.task_parallelism} tasks per run`;
  return (
    <section className="settings-section settings-section--wide settings-status">
      <h3><Activity size={14} /> Status</h3>
      <div className="sys-grid">
        <StatusCard icon={<Package size={15} />} label="Version" value={`v${status.version}`}
                    note={updateNote.tone === 'warning' && update.available
                      ? <>{update.latest} available · how to upgrade: <code>pip install -U runrail</code></>
                      : updateNote.text}
                    tone={updateNote.tone} />
        <StatusCard icon={<Clock size={15} />} label="Uptime" value={formatUptime(status.started_at, now)}
                    note={`since ${formatDate(status.started_at)}`} />
        <StatusCard icon={<Cpu size={15} />} label="Worker"
                    value={worker.paused ? `Paused · ${worker.running} finishing` : `${worker.running} running`}
                    note={workerNote} tone={worker.paused ? 'warning' : 'default'} />
        <StatusCard icon={<Server size={15} />} label="Services" value={`${services.running} of ${services.total} running`}
                    note={services.supervised ? 'Supervised' : 'Nothing is supervising services — start runrail serve or runrail worker'}
                    tone={services.supervised ? 'default' : 'warning'} />
        <StatusCard icon={<CalendarClock size={15} />} label="Next scheduled run"
                    value={status.scheduler.next_fire_at ? formatCountdown(status.scheduler.next_fire_at, now) : 'Nothing scheduled'}
                    note={status.scheduler.next_fire_at ? formatDate(status.scheduler.next_fire_at) : 'No enabled workflow has a schedule'} />
        <StatusCard icon={<Database size={15} />} label="Database" value={database.backend}
                    note={<><code>{database.location}</code>{database.size_bytes != null && ` · ${formatSize(database.size_bytes)}`}</>} />
        <StatusCard icon={<HardDrive size={15} />} label="Disk" value={`${formatSize(disk.free_bytes)} free`}
                    note={`of ${formatSize(disk.total_bytes)}`} />
        <StatusCard icon={<Archive size={15} />} label="Storage"
                    value={storageTotal == null ? 'measuring…' : formatSize(storageTotal)}
                    note={<>{storageParts}{storage.measured_at && <> · measured {timeAgo(storage.measured_at, now)}</>}{storage.measuring && storage.measured_at && ' · measuring…'}</>}
                    action={
                      <button type="button" className="icon-button sys-card-action" onClick={onRefresh}
                              disabled={storage.measuring} aria-label="Measure storage again" title="Measure storage again">
                        <RefreshCw size={13} className={storage.measuring ? 'icon-spin' : undefined} />
                      </button>
                    } />
        <StatusCard icon={<FolderOpen size={15} />} label="Home" value={<code>{status.home}</code>}
                    note="Logs, artifacts, environments and backups live here" />
        <StatusCard icon={<SlidersHorizontal size={15} />} label="Runtime" value={`Python ${status.python}`} note={status.platform} />
      </div>
    </section>
  );
}

/* ─── Pause ───────────────────────────────────────────────── */

function PauseSection({ status, help, onChanged }: {
  status: SystemStatus | null; help: string | null; onChanged: (next: SystemStatus) => void;
}) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const paused = status?.worker.paused ?? false;
  const set = async (next: boolean) => {
    if (next && !confirm('Pause everything new? Queued runs and services wait until you resume; what is executing finishes.')) return;
    setBusy(true);
    try { onChanged(await post<SystemStatus>('/system/pause', { paused: next })); }
    catch (error) { toast(reasonOf(error, next ? 'Could not pause' : 'Could not resume'), 'error'); }
    finally { setBusy(false); }
  };
  return (
    <section className={clsx('settings-section settings-section--wide settings-pause', paused && 'is-paused')}>
      <h3><Pause size={14} /> Pause</h3>
      {paused ? (
        <div className="callout callout--warning settings-pause-banner" role="status">
          <span>{pausedBanner(status?.worker.running ?? 0)}</span>
          <Button onClick={() => set(false)} disabled={busy || !status}><Play size={13} /> Resume</Button>
        </div>
      ) : (
        <div className="settings-pause-row">
          <div>
            <strong>Pause everything new</strong>
            <p>{help ?? 'While paused nothing new starts: queued runs wait, services are not started. What is executing finishes.'}</p>
          </div>
          <Button variant="danger" onClick={() => set(true)} disabled={busy || !status}><Pause size={13} /> Pause everything new</Button>
        </div>
      )}
    </section>
  );
}

/* ─── Backup & restore ────────────────────────────────────── */

function BackupSection({ home }: { home: string | null }) {
  const { toast } = useToast();
  const [points, setPoints] = useState<{ points: RestorePoint[]; directory: string } | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<BackupPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [summary, setSummary] = useState<RestoreSummary | null>(null);
  const [busy, setBusy] = useState<'preview' | 'restore' | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void api<{ points: RestorePoint[]; directory: string }>('/system/restore-points').then(setPoints).catch(() => {});
  }, []);

  const pick = async (e: ChangeEvent<HTMLInputElement>) => {
    const chosen = e.target.files?.[0] ?? null;
    setFile(chosen); setPreview(null); setPreviewError(null); setSummary(null);
    if (!chosen) return;
    setBusy('preview');
    try { setPreview(await uploadZip<BackupPreview>('/system/restore/preview', chosen)); }
    catch (error) { setPreviewError(reasonOf(error, 'Could not read the backup')); }
    finally { setBusy(null); }
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
  const zipName = file?.name ?? 'runrail-backup.zip';
  return (
    <section className="settings-section settings-section--wide settings-backup">
      <h3><Archive size={14} /> Backup &amp; restore</h3>
      <div className="settings-columns">
        <div className="form-stack">
          <div>
            <a className="btn btn-primary btn-md" href="/api/system/backup" download><Download size={14} /> Download backup</a>
            <p className="settings-hint" style={{ marginTop: 10 }}>
              A zip of the configuration — workflows, services, projects, environments — and a snapshot of the
              database. Logs and artifacts live in {home ? <code>{home}</code> : 'the home folder'}; copy that
              folder for a full copy.
            </p>
          </div>
          <div>
            <strong className="settings-subhead">Restore points</strong>
            {points == null ? <p className="muted">Loading…</p> : points.points.length === 0 ? (
              <p className="settings-hint">None yet. One is taken under <code>{points.directory}</code> before every schema upgrade.</p>
            ) : (
              <>
                <ul className="restore-points">
                  {points.points.map(p => (
                    <li key={p.name} className="restore-point">
                      <code>{p.name}</code>
                      <span className="muted">{formatSize(p.size_bytes)} · {timeAgo(p.created_at)}</span>
                    </li>
                  ))}
                </ul>
                <p className="settings-hint">Taken under <code>{points.directory}</code> before every schema upgrade.</p>
              </>
            )}
          </div>
        </div>
        <div className="form-stack">
          <div className="field settings-file">
            <span><label htmlFor="restore-file">Restore from a backup</label></span>
            <input id="restore-file" ref={fileInput} type="file" accept=".zip,application/zip" onChange={pick} disabled={busy != null} />
            <small>Pick a zip made by Download backup or <code>runrail backup</code>. It is inspected before anything is applied.</small>
          </div>
          {busy === 'preview' && <p className="muted">Reading the backup…</p>}
          {previewError && <p className="settings-error" role="alert">{previewError}</p>}
          {preview && (
            <div className="callout restore-manifest">
              <div><strong>Made {preview.manifest.created_at ? formatDate(preview.manifest.created_at) : 'at an unknown time'}</strong> by RunRail {preview.manifest.runrail_version ?? '?'}</div>
              <div>
                {counts.workflows ?? preview.configuration.workflows ?? 0} workflows · {counts.services ?? preview.configuration.services ?? 0} services ·{' '}
                {counts.projects ?? preview.configuration.projects ?? 0} projects · {counts.environments ?? preview.configuration.environments ?? 0} environments
                {counts.runs != null && <> · {counts.runs.toLocaleString('en-US')} runs</>}
              </div>
              <div>{preview.includes_database ? 'Includes a database snapshot' : 'Configuration only — no database inside'}</div>
              {!summary && (
                <div className="settings-inline-actions">
                  <Button onClick={restore} disabled={busy != null}><Upload size={13} /> {busy === 'restore' ? 'Restoring…' : 'Restore configuration'}</Button>
                  <Button variant="ghost" onClick={reset} disabled={busy != null}>Clear</Button>
                </div>
              )}
              {summary && (
                <div className="restore-summary" role="status">
                  <strong>Restored: {restoreSummaryLine(summary)}</strong>
                  {Object.entries(summary).filter(([, names]) => names.length > 0).map(([bucket, names]) => (
                    <div key={bucket} className="muted">{bucket}: {names.join(', ')}</div>
                  ))}
                  <div><Button variant="ghost" size="sm" onClick={reset}>Done</Button></div>
                </div>
              )}
            </div>
          )}
          <p className="settings-hint">
            Restoring here applies the configuration only; nothing is deleted. The database inside a backup
            — run history — is restored offline, with the server stopped:
          </p>
          <pre className="settings-command"><code>runrail restore {zipName}</code></pre>
        </div>
      </div>
    </section>
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
        <em className="settings-provenance">{provenance(setting, now)}</em>
      </span>
      {children}
      <small>{setting.help}</small>
      {locked && <small className="settings-locked">{locked}</small>}
    </div>
  );
}

function GroupFooter({ error, children }: { error: string | null; children: ReactNode }) {
  return (
    <div className="settings-actions">
      {error && <p className="settings-error" role="alert">{error}</p>}
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

/** Only the keys the page may change go in a save; a pinned key would make
 *  the whole save a 409. */
function unlocked(settings: SettingsMap, values: Partial<SettingValues>): Partial<SettingValues> {
  const out: Partial<SettingValues> = {};
  for (const key of Object.keys(values) as SettingKey[]) {
    if (!settings.get(key)?.locked) Object.assign(out, { [key]: values[key] });
  }
  return out;
}

/* General: public URL and the update check. */
function GeneralGroup({ settings, now, onSaved }: GroupProps) {
  const url = settings.get('public_url');
  const check = settings.get('update_check');
  const [urlText, setUrlText] = useState(valueOf(settings, 'public_url') ?? '');
  const [checkOn, setCheckOn] = useState(valueOf(settings, 'update_check') ?? true);
  useEffect(() => { setUrlText(valueOf(settings, 'public_url') ?? ''); }, [url?.value]);
  useEffect(() => { setCheckOn(valueOf(settings, 'update_check') ?? true); }, [check?.value]);
  const { save, saving, error } = useGroupSave(onSaved);
  if (!url || !check) return null;
  const values: Partial<SettingValues> = { public_url: urlText.trim() || null, update_check: checkOn };
  const dirty = !sameValue(values.public_url, url.value) || !sameValue(values.update_check, check.value);
  return (
    <section className="settings-section settings-group" aria-label="General">
      <h3><Globe size={14} /> General</h3>
      <div className="form-stack">
        <SettingField setting={url} now={now} htmlFor="setting-public-url">
          <input id="setting-public-url" type="url" value={urlText} disabled={url.locked} placeholder={window.location.origin}
                 onChange={e => setUrlText(e.target.value)} />
        </SettingField>
        <SettingField setting={check} now={now}>
          <div className="settings-toggle-row">
            <span className="settings-hint">{checkOn ? 'PyPI is asked a few times a day' : 'PyPI is not asked'}</span>
            <Toggle id="setting-update-check" label={check.title} checked={checkOn} disabled={check.locked} onChange={setCheckOn} />
          </div>
        </SettingField>
      </div>
      <GroupFooter error={error}>
        <Button onClick={() => save(unlocked(settings, values))} disabled={!dirty || saving}>{saving ? 'Saving…' : 'Save'}</Button>
      </GroupFooter>
    </section>
  );
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
      <h3><Cpu size={14} /> Workers</h3>
      <div className="form-stack">
        <div className="field-row">
          <SettingField setting={concurrency} now={now} htmlFor="setting-worker-concurrency">
            <input id="setting-worker-concurrency" type="number" min={1} max={64} step={1} inputMode="numeric"
                   value={runsText} disabled={concurrency.locked} onChange={e => setRunsText(e.target.value)} />
          </SettingField>
          <SettingField setting={parallelism} now={now} htmlFor="setting-task-parallelism">
            <input id="setting-task-parallelism" type="number" min={1} max={64} step={1} inputMode="numeric"
                   value={tasksText} disabled={parallelism.locked} onChange={e => setTasksText(e.target.value)} />
          </SettingField>
        </div>
      </div>
      <GroupFooter error={error}>
        <Button onClick={submit} disabled={!dirty || saving || (concurrency.locked && parallelism.locked)}>{saving ? 'Saving…' : 'Save'}</Button>
      </GroupFooter>
    </section>
  );
}

/* Retention: how long finished runs are kept, with what a change would delete
   said before it can be saved. */
function RetentionGroup({ settings, now, onSaved }: GroupProps) {
  const setting = settings.get('retention_days');
  const saved = valueOf(settings, 'retention_days');
  const [forever, setForever] = useState(saved == null);
  const [daysText, setDaysText] = useState(saved == null ? '' : String(saved));
  useEffect(() => { setForever(saved == null); setDaysText(saved == null ? '' : String(saved)); }, [saved]);
  const [preview, setPreview] = useState<RetentionPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const { save, saving, error, setError } = useGroupSave(onSaved);

  const parsed = forever ? { ok: true as const, value: null } : parseDays(daysText);
  const wanted = parsed.ok ? parsed.value : undefined;
  // The preview for the number on screen, ~400ms after the last keystroke;
  // an older answer never lands over a newer question.
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
  }, [wanted]);

  if (!setting) return null;
  const dirty = !parsed.ok || wanted !== saved;
  const previewReady = wanted == null || (preview != null && preview.days === wanted);
  const submit = () => {
    if (!parsed.ok) { setError(`${setting.title}: ${parsed.reason}`); return; }
    if (preview && preview.runs > 0 && !confirm(`${retentionSentence(preview)}. Finished runs older than ${preview.days} days will be deleted with their logs and artifacts within six hours. Continue?`)) return;
    void save({ retention_days: parsed.value });
  };
  return (
    <section className="settings-section settings-group" aria-label="Retention">
      <h3><Trash2 size={14} /> Retention</h3>
      <div className="form-stack">
        <SettingField setting={setting} now={now} htmlFor="setting-retention-days">
          <div className="settings-inline-field">
            <input id="setting-retention-days" type="number" min={1} step={1} inputMode="numeric" value={daysText}
                   placeholder="never" disabled={setting.locked || forever} onChange={e => setDaysText(e.target.value)} />
            <span className="settings-unit">days</span>
            <label className="settings-check">
              <input type="checkbox" checked={forever} disabled={setting.locked} onChange={e => setForever(e.target.checked)} />
              Keep forever
            </label>
          </div>
        </SettingField>
        {!setting.locked && (
          <p className={clsx('retention-preview', preview && preview.runs > 0 && 'is-destructive')} role="status">
            {forever ? 'Nothing is ever deleted.'
              : !parsed.ok ? parsed.reason
              : previewError ? `Could not preview: ${previewError}`
              : preview && preview.days === wanted ? retentionSentence(preview)
              : 'Working out what this would delete…'}
          </p>
        )}
      </div>
      <GroupFooter error={error}>
        <Button onClick={submit} disabled={!dirty || saving || setting.locked || !previewReady}>{saving ? 'Saving…' : 'Save'}</Button>
      </GroupFooter>
    </section>
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
  return (
    <section className="settings-section settings-group" aria-label="Notifications">
      <h3><Bell size={14} /> Notifications</h3>
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
        <SettingField setting={events} now={now}>
          <div className="settings-checks" role="group" aria-label={events.title}>
            {eventKinds.map(kind => (
              <label key={kind} className="settings-check">
                <input type="checkbox" checked={chosen.includes(kind)} onChange={e => toggleKind(kind, e.target.checked)} />
                {eventLabel(kind)}
              </label>
            ))}
          </div>
        </SettingField>
        <SettingField setting={quiet} now={now}>
          <div className="settings-toggle-row">
            <span className="settings-hint">{quietOn ? `Nothing is posted from ${start} to ${end} (${zone || 'UTC'})` : 'None — alerts go out around the clock'}</span>
            <Toggle id="setting-quiet-hours" label={quiet.title} checked={quietOn} onChange={setQuietOn} />
          </div>
          {quietOn && (
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
          )}
        </SettingField>
      </div>
      <GroupFooter error={error}>
        <Button onClick={() => save(unlocked(settings, values))} disabled={!dirty || saving}>{saving ? 'Saving…' : 'Save'}</Button>
      </GroupFooter>
    </section>
  );
}

/* ─── Appearance ──────────────────────────────────────────── */

function AppearanceSection() {
  const [theme, setTheme] = useState<string>(() => document.documentElement.dataset.theme ?? 'dark');
  const switchTheme = (t: string) => {
    document.documentElement.dataset.theme = t;
    localStorage.setItem('runrail-theme', t);
    setTheme(t);
  };
  return (
    <section className="settings-section settings-section--wide">
      <h3><Palette size={14} /> Appearance</h3>
      <div className="settings-row">
        <span className="settings-label">Theme</span>
        <div style={{ display: 'flex', gap: 6 }}>
          {(['dark', 'light'] as const).map(t => (
            <button key={t} className={`btn btn-sm ${theme === t ? 'btn-primary' : 'btn-ghost'}`} onClick={() => switchTheme(t)}>
              {t === 'dark' ? '☾ Dark' : '☀ Light'}
            </button>
          ))}
        </div>
      </div>
      <div className="settings-row">
        <span className="settings-label">API endpoint</span>
        <span className="settings-value">{window.location.origin}/api</span>
      </div>
    </section>
  );
}
