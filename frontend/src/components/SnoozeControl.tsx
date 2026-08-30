import { CSSProperties, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, Bell, BellOff, CalendarClock, Clock, Pause, Sunrise, X } from 'lucide-react';
import clsx from 'clsx';
import { api, post } from '../api';
import { Button } from './ui';
import { useToast } from './toast';

/* ─── Snooze ───────────────────────────────────────────────
   Mute a workflow's alerts until an instant. Two exports:

   • <SnoozeBadge>   — the always-visible state. A snooze that hides is just
     "disable and forget" with extra steps, so wherever a muted workflow is
     listed the chip shows the live countdown and offers one-click un-snooze.
   • <SnoozeControl> — the action. Presets resolve in the VIEWER's zone
     ("tomorrow 9am" is a wall-clock idea the browser knows and the server
     does not) and post an absolute instant with its offset attached.

   Plain snooze mutes alerts while runs keep executing — that is the safe
   default and stays one click. Also pausing scheduled runs is an escalation:
   it must be armed deliberately and then confirmed, never fired by reflex. */

/** The snooze-relevant slice of WorkflowOut. Structural, so a host can hand its
 *  own Workflow straight in; generic, so onChange hands the same type back. */
export interface SnoozeWorkflow {
  id: number;
  snooze_until?: string | null;
  snooze_pauses_runs?: boolean;
  /** Server-computed at response time — the components re-derive it from
   *  snooze_until so the UI flips the moment the countdown lands, with no refetch. */
  snoozed?: boolean;
}

/* ─── Shared ticker ───────────────────────────────────────
   A grid of muted workflow cards would otherwise own one interval each.
   Every countdown subscribes to a single timer that exists only while
   something is actually counting down. */
const subscribers = new Set<() => void>();
let timer = 0;

function useTick(active: boolean): number {
  const [, bump] = useState(0);
  useEffect(() => {
    if (!active) return;
    const fn = () => bump(n => n + 1);
    subscribers.add(fn);
    if (!timer) timer = window.setInterval(() => subscribers.forEach(f => f()), 1000);
    return () => {
      subscribers.delete(fn);
      if (subscribers.size === 0) { window.clearInterval(timer); timer = 0; }
    };
  }, [active]);
  return Date.now();
}

/* ─── Time helpers ───────────────────────────────────────── */

/** "3h 20m" — coarse far out, precise near the end, so the last minute is
 *  visibly the last minute. */
export function formatRemaining(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  return `${seconds}s`;
}

const two = (n: number) => String(n).padStart(2, '0');

/** ISO 8601 carrying the viewer's own offset (2026-08-30T09:00:00+04:00).
 *  Deliberately not a bare local string: the backend would read that as UTC and
 *  the mute would land hours away from the wall clock the operator picked. */
function isoWithOffset(date: Date): string {
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? '+' : '-';
  const abs = Math.abs(offset);
  return `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`
    + `T${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`
    + `${sign}${two(Math.floor(abs / 60))}:${two(abs % 60)}`;
}

/** Value shape for <input type="datetime-local">, which is local wall clock. */
const toInputValue = (date: Date) =>
  `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`
  + `T${two(date.getHours())}:${two(date.getMinutes())}`;

/** setHours works on wall clock, so a preset that crosses a DST boundary still
 *  lands on 09:00 rather than 08:00 or 10:00. */
function atHour(date: Date, hour: number): Date {
  const result = new Date(date);
  result.setHours(hour, 0, 0, 0);
  return result;
}

function tomorrowAt9(from: Date): Date {
  const result = atHour(from, 9);
  result.setDate(result.getDate() + 1);
  return result;
}

/** The next Monday 09:00 strictly in the future — Monday morning at 08:00 gets
 *  an hour, not eight days. */
function mondayAt9(from: Date): Date {
  const result = atHour(from, 9);
  while (result <= from || result.getDay() !== 1) result.setDate(result.getDate() + 1);
  return result;
}

function formatUntil(date: Date): string {
  const sameDay = date.toDateString() === new Date().toDateString();
  return date.toLocaleString(undefined, {
    ...(sameDay ? {} : { weekday: 'short' }),
    hour: '2-digit', minute: '2-digit',
  });
}

const formatUntilLong = (date: Date) => date.toLocaleString(undefined, {
  weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
});

/** SnoozeIn rejects anything past 30 days, so the picker refuses it first. */
const MAX_DAYS = 30;

/* ─── State reader ───────────────────────────────────────── */
/** Is there a countdown left to run? Read against the wall clock, not a ticked
 *  `now`, so it is safe to gate the ticker on. */
const counting = (workflow: SnoozeWorkflow): boolean =>
  workflow.snooze_until != null && new Date(workflow.snooze_until).getTime() > Date.now();

function readSnooze(workflow: SnoozeWorkflow, now: number) {
  const until = workflow.snooze_until ? new Date(workflow.snooze_until) : null;
  const active = until != null && !Number.isNaN(until.getTime()) && until.getTime() > now;
  return { until, active, pausesRuns: active && Boolean(workflow.snooze_pauses_runs) };
}

/* ─── Badge ───────────────────────────────────────────────
   The unmistakable, always-visible state: live countdown + un-snooze.
   Renders nothing when the workflow is not muted. */
export function SnoozeBadge<W extends SnoozeWorkflow>({
  workflow, onChange, onExpire, className, showUndo = true,
}: {
  workflow: W;
  /** Handed the fresh WorkflowOut after un-snoozing. */
  onChange?: (workflow: W) => void;
  /** Fired once when the countdown reaches zero — hosts that poll can refetch. */
  onExpire?: () => void;
  className?: string;
  /** Set false on read-only surfaces (wallboard) to drop the un-snooze button. */
  showUndo?: boolean;
}) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const expired = useRef(false);
  // Recomputed every render rather than derived from `now`, so the ticker
  // unsubscribes itself on the very render where the countdown lands — and a
  // long-expired snooze_until never starts one at all.
  const now = useTick(counting(workflow));
  const { until, active, pausesRuns } = readSnooze(workflow, now);

  useEffect(() => { expired.current = false; }, [workflow.snooze_until]);
  useEffect(() => {
    if (until && !active && !expired.current) { expired.current = true; onExpire?.(); }
  }, [until, active, onExpire]);

  if (!until || !active) return null;

  const unsnooze = async (e: React.MouseEvent) => {
    // Workflow cards navigate on click; the chip must never trigger that.
    e.stopPropagation();
    e.preventDefault();
    setBusy(true);
    try {
      onChange?.(await api<W>(`/workflows/${workflow.id}/snooze`, { method: 'DELETE' }));
      toast('Alerts un-muted', 'info');
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not un-snooze', 'error');
    } finally { setBusy(false); }
  };

  return (
    <span
      className={clsx('snooze-chip', pausesRuns && 'snooze-chip--paused', className)}
      title={`Alerts muted until ${formatUntilLong(until)}`}
    >
      <BellOff size={11} strokeWidth={2.5} />
      <span className="snooze-count">muted for {formatRemaining(until.getTime() - now)}</span>
      {pausesRuns && (
        <span className="snooze-chip-tag"><Pause size={9} strokeWidth={3} />runs paused</span>
      )}
      {showUndo && (
        <button
          type="button"
          className="snooze-chip-undo"
          onClick={unsnooze}
          onMouseDown={e => e.stopPropagation()}
          disabled={busy}
          title="Un-snooze now"
          aria-label="Un-snooze now"
        >
          <X size={11} strokeWidth={3} />
        </button>
      )}
    </span>
  );
}

/* ─── Popover placement ───────────────────────────────────
   Portaled to <body> so a card's overflow/transform cannot clip it, and
   re-placed on scroll/resize so it stays welded to its trigger. */
function usePlacement(anchor: HTMLElement | null, open: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  // z-index is inline on purpose, not left to the stylesheet: the panels and
  // cards this hangs over are positioned with z-index of their own, so an
  // `auto` popover is painted *under* them and swallows its own clicks — it
  // has to be true the moment the component mounts, CSS or no CSS.
  const [style, setStyle] = useState<CSSProperties>({
    position: 'fixed', top: 0, left: 0, zIndex: 120, visibility: 'hidden',
  });
  useLayoutEffect(() => {
    if (!open) return;
    let frame = 0;
    const place = () => {
      frame = 0;
      const box = anchor?.getBoundingClientRect();
      const panel = ref.current;
      if (!box || !panel) return;
      const { offsetWidth: width, offsetHeight: height } = panel;
      const below = box.bottom + 8;
      const flip = below + height > window.innerHeight - 12 && box.top - 8 - height > 12;
      setStyle({
        position: 'fixed',
        zIndex: 120,
        top: flip ? box.top - 8 - height : below,
        left: Math.max(12, Math.min(box.left, window.innerWidth - width - 12)),
        visibility: 'visible',
      });
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(place); };
    place();
    window.addEventListener('resize', schedule);
    window.addEventListener('scroll', schedule, true);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      window.removeEventListener('resize', schedule);
      window.removeEventListener('scroll', schedule, true);
    };
  }, [open, anchor]);
  return { ref, style };
}

/* ─── Control ───────────────────────────────────────────── */
export function SnoozeControl<W extends SnoozeWorkflow>({
  workflow, onChange, size = 'md', label,
}: {
  workflow: W;
  /** Handed the fresh WorkflowOut after every snooze/un-snooze. */
  onChange?: (workflow: W) => void;
  size?: 'sm' | 'md';
  /** Overrides the trigger's text; the icon and muted state are unchanged. */
  label?: string;
}) {
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pauseRuns, setPauseRuns] = useState(false);
  // Only used while the escalation is armed: pausing runs takes an explicit
  // confirm, so the chosen instant waits here instead of firing on click.
  // Carries the preset id too — "1 hour" slides forward every tick, and the
  // selection must not blink off underneath the operator when it does.
  const [pending, setPending] = useState<{ id: string; at: Date } | null>(null);
  const [custom, setCustom] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const { ref: panelRef, style } = usePlacement(anchor, open);

  // Mirrors the prop but survives the host's next poll returning stale data;
  // keyed on the snooze fields so unrelated identity churn never clobbers it.
  const [current, setCurrent] = useState<W>(workflow);
  useEffect(() => { setCurrent(workflow); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [workflow.id, workflow.snooze_until, workflow.snooze_pauses_runs]);

  const now = useTick(open || counting(current));
  const { until, active, pausesRuns } = readSnooze(current, now);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing) return;
      // Claim the key so an enclosing Modal does not close underneath us.
      e.preventDefault();
      setOpen(false);
    };
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target) || anchor?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [open, anchor, panelRef]);

  const reset = () => { setPending(null); setError(null); setCustom(''); setPauseRuns(false); };

  const apply = async (until_: Date, alsoPause: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const updated = await post<W>(`/workflows/${workflow.id}/snooze`, {
        until: isoWithOffset(until_), pause_runs: alsoPause,
      });
      setCurrent(updated);
      onChange?.(updated);
      toast(alsoPause
        ? `Alerts muted and scheduled runs paused until ${formatUntil(until_)}`
        : `Alerts muted until ${formatUntil(until_)}`);
      setOpen(false);
      reset();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not snooze this workflow');
    } finally { setBusy(false); }
  };

  const unsnooze = async () => {
    setBusy(true);
    try {
      const updated = await api<W>(`/workflows/${workflow.id}/snooze`, { method: 'DELETE' });
      setCurrent(updated);
      onChange?.(updated);
      toast('Alerts un-muted', 'info');
      setOpen(false);
      reset();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not un-snooze this workflow');
    } finally { setBusy(false); }
  };

  /* Safe path is one click; the escalation turns a preset into a selection
     that still needs the confirm button underneath it. */
  const choose = (id: string, date: Date) => {
    setError(null);
    if (pauseRuns) setPending({ id, at: date });
    else void apply(date, false);
  };

  const applyCustom = () => {
    if (!custom) { setError('Pick a date and time first'); return; }
    const date = new Date(custom);
    if (Number.isNaN(date.getTime())) { setError('That is not a valid date and time'); return; }
    if (date.getTime() <= Date.now()) { setError('Pick a time in the future'); return; }
    if (date.getTime() > Date.now() + MAX_DAYS * 86_400_000) {
      setError(`A snooze cannot run longer than ${MAX_DAYS} days`); return;
    }
    choose('custom', date);
  };

  const reference = new Date(now);
  const tomorrow = tomorrowAt9(reference);
  const monday = mondayAt9(reference);
  const presets: { id: string; icon: typeof Clock; label: string; at: Date }[] = [
    { id: 'hour', icon: Clock, label: '1 hour', at: new Date(now + 3_600_000) },
    { id: 'tomorrow', icon: Sunrise, label: 'Tomorrow 9am', at: tomorrow },
    // On a Sunday the two land on the same instant; offering it twice just asks
    // the operator to work out whether they differ.
    ...(monday.getTime() === tomorrow.getTime()
      ? []
      : [{ id: 'monday', icon: CalendarClock, label: 'Monday 9am', at: monday }]),
  ];

  const triggerText = label ?? (active ? `Muted · ${formatRemaining(until!.getTime() - now)}` : 'Snooze');

  return (
    <>
      {/* The wrapper is the popover's anchor (Button forwards no ref) and the
          click firebreak: on a workflow card the article itself navigates. */}
      <span
        className="snooze-anchor"
        ref={setAnchor}
        onMouseDown={e => e.stopPropagation()}
        onClick={e => e.stopPropagation()}
      >
        <Button
          variant="ghost"
          size={size}
          className={clsx('snooze-trigger', active && 'is-muted')}
          onClick={() => { setOpen(o => !o); setError(null); }}
          title={active ? `Alerts muted until ${formatUntilLong(until!)}` : 'Mute this workflow’s alerts'}
        >
          {active ? <BellOff size={13} /> : <Bell size={13} />}
          <span className={active ? 'snooze-count' : undefined}>{triggerText}</span>
        </Button>
      </span>

      {open && createPortal(
        <div
          className="snooze-pop"
          ref={panelRef}
          style={style}
          role="dialog"
          aria-label="Snooze alerts"
          onMouseDown={e => e.stopPropagation()}
          onClick={e => e.stopPropagation()}
        >
          {active && (
            <div className="snooze-pop-state">
              <div>
                <strong className="snooze-count">Muted for {formatRemaining(until!.getTime() - now)}</strong>
                <span>until {formatUntilLong(until!)}{pausesRuns ? ' · scheduled runs paused' : ''}</span>
              </div>
              <Button variant="secondary" size="sm" onClick={unsnooze} disabled={busy}>
                <Bell size={12} /> Un-snooze
              </Button>
            </div>
          )}

          <div className="snooze-pop-head">
            <h4>{active ? 'Change the mute' : 'Mute alerts'}</h4>
            <p>
              No webhook alerts — failures, missed runs, SLA breaches or approval
              requests — until it expires. Runs keep executing.
            </p>
          </div>

          <div className="snooze-presets" role="group" aria-label="Snooze duration">
            {presets.map(({ id, icon: Icon, label: text, at }) => (
              <button
                key={id}
                type="button"
                className={clsx('snooze-preset', pending?.id === id && 'is-picked')}
                onClick={() => choose(id, at)}
                disabled={busy}
                aria-pressed={pending?.id === id}
              >
                <Icon size={13} />
                <span className="snooze-preset-label">{text}</span>
                <span className="snooze-preset-when">{formatUntil(at)}</span>
              </button>
            ))}
          </div>

          <div className="snooze-custom">
            <label className="field">
              <span>Custom <em>your timezone</em></span>
              <div className="path-input">
                <input
                  type="datetime-local"
                  value={custom}
                  min={toInputValue(new Date(now + 60_000))}
                  max={toInputValue(new Date(now + MAX_DAYS * 86_400_000))}
                  onChange={e => { setCustom(e.target.value); setError(null); setPending(null); }}
                />
                <Button variant="secondary" size="sm" onClick={applyCustom} disabled={busy}>
                  {pauseRuns ? 'Pick' : 'Mute'}
                </Button>
              </div>
            </label>
          </div>

          <div className={clsx('snooze-escalation', pauseRuns && 'is-armed')}>
            <label className="snooze-escalation-row">
              <span className="toggle">
                <input
                  type="checkbox"
                  checked={pauseRuns}
                  onChange={e => { setPauseRuns(e.target.checked); setPending(null); setError(null); }}
                />
                <span />
              </span>
              <span className="snooze-escalation-text">
                <strong><Pause size={11} strokeWidth={2.5} /> Also pause scheduled runs</strong>
                <small>
                  Scheduled runs are skipped outright while muted — not queued, not
                  caught up afterwards. Manual runs and backfills still work, and the
                  pause lifts by itself, unlike disabling the workflow.
                </small>
              </span>
            </label>

            {pauseRuns && (
              <div className="snooze-confirm">
                {pending ? (
                  <>
                    <span><AlertTriangle size={12} /> Skip every scheduled run until {formatUntilLong(pending.at)}</span>
                    <Button variant="danger" size="sm" onClick={() => void apply(pending.at, true)} disabled={busy}>
                      Mute &amp; pause runs
                    </Button>
                  </>
                ) : (
                  <span className="snooze-confirm-hint">Now pick how long — you will confirm before anything is paused.</span>
                )}
              </div>
            )}
          </div>

          {error && <p className="snooze-error"><AlertTriangle size={12} /> {error}</p>}
        </div>,
        document.body,
      )}
    </>
  );
}
