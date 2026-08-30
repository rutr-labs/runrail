import {
  CSSProperties, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
} from 'react';
import type { FC } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, Bell, CalendarX, CheckCheck, CheckCircle2, ChevronRight,
  Inbox, PauseCircle, ShieldAlert, Timer, WifiOff, XCircle,
} from 'lucide-react';
import clsx from 'clsx';
import { api } from '../api';
import { timeAgo } from '../format';
import { Button, EmptyState } from './ui';

/* ─── Notification centre ──────────────────────────────────
   The bell in the topbar and the panel behind it. Until now the
   only notification surface was the toaster: miss one and it is
   gone forever. This gives that history a home.

   Read state is deliberately CLIENT-SIDE. The backend stores none
   — single user, one browser — so a last-read instant lives in
   localStorage, rides up as `read_at`, and the response answers
   with `unread`. Two rules follow from that and are load-bearing:

   • The stamp is always the response's own `generated_at`, never
     Date.now(). A browser clock a few minutes fast would silently
     mark events read that the operator never saw.
   • localStorage is allowed to throw (Safari private mode, site
     data blocked). Every access is wrapped; the worst case is that
     unread resets on reload, which is a nuisance, not a broken
     topbar.

   The feed endpoint is polled, and this app is wallboard-adjacent:
   people leave it open for days. Polling stops dead while the tab
   is hidden and catches up the moment it comes back. */

const FEED_PATH = '/activity';
const READ_KEY = 'runrail-activity-read-at';

/** Slow on purpose. The feed is derived from rows the app already writes and
 *  nothing here is second-by-second actionable; the run pages own that. */
const DEFAULT_POLL_MS = 45_000;
const DEFAULT_LIMIT = 40;
/** Matches the server's own default window (7 days) so the copy can say so. */
const DEFAULT_WINDOW_HOURS = 24 * 7;

/* ─── Wire types ─────────────────────────────────────────── */

/** The six kinds `activity.SEVERITY` names. Typed loosely on the wire so a
 *  newer backend adding a seventh renders as a generic row instead of a hole. */
export type ActivityKind =
  | 'run_failed' | 'run_recovered' | 'workflow_paused'
  | 'sla_breached' | 'run_missed' | 'approval_requested';

export type ActivitySeverity = 'error' | 'warning' | 'info' | 'success';

export interface ActivityEvent {
  /** `${kind}:${key}` — stable across polls, so it keys the list directly. */
  id: string;
  /** One of {@link ActivityKind}. */
  kind: string;
  /** One of {@link ActivitySeverity}. */
  severity: string;
  /** A complete sentence from the server; the panel never rewrites it. */
  title: string;
  at: string;
  workflow_id: number;
  workflow_name: string;
  run_id: number | null;
  task_name: string | null;
}

export interface ActivityFeed {
  events: ActivityEvent[];
  /** In-window total, which can exceed `events.length` when `limit` truncates. */
  total: number;
  unread: number;
  window_hours: number;
  /** The server's clock at response time — the only thing ever stamped as read. */
  generated_at: string;
}

/* ─── Read-state storage ─────────────────────────────────── */

function loadReadAt(): string | null {
  try {
    const raw = window.localStorage.getItem(READ_KEY);
    // A corrupted value would be sent up as read_at and rejected 422 on every
    // poll — a dead bell. Counting the window unread once is the cheaper loss.
    return raw && !Number.isNaN(Date.parse(raw)) ? raw : null;
  } catch {
    return null;
  }
}

function saveReadAt(value: string): void {
  try {
    window.localStorage.setItem(READ_KEY, value);
  } catch {
    /* Private mode / blocked site data. Unread resets on reload; the bell lives. */
  }
}

/* ─── Time ───────────────────────────────────────────────── */

/** Epoch ms, or 0 for absent/unparseable — so a missing boundary reads as
 *  "nothing has been read", exactly how the server treats a missing read_at. */
const ms = (value?: string | null): number => {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
};

function absoluteTime(value: string): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return '—';
  return at.toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

const startOfDay = (date: Date) =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();

function dayLabel(value: string, now: Date): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return 'Earlier';
  const days = Math.round((startOfDay(now) - startOfDay(at)) / 86_400_000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return at.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

function windowLabel(hours: number): string {
  if (hours % 24 === 0) {
    const days = hours / 24;
    return days === 1 ? 'last 24 hours' : `last ${days} days`;
  }
  return hours === 1 ? 'last hour' : `last ${hours} hours`;
}

/** One shared clock for the relative labels, alive only while the panel is.
 *  30s is finer than the smallest label ("1m ago") ever needs. */
function useTicker(active: boolean, intervalMs = 30_000): number {
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setTick(Date.now());
    const timer = window.setInterval(() => setTick(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [active, intervalMs]);
  return tick;
}

/* ─── Kind vocabulary ────────────────────────────────────── */

type IconComponent = FC<{ size?: number; strokeWidth?: number; className?: string }>;

interface KindMeta {
  icon: IconComponent;
  /** The eyebrow. The server's `title` carries the detail; this carries the type. */
  label: string;
  /** Where the row goes. `workflow` for the two whose fix is a workflow-level
   *  one — re-enabling an auto-paused workflow, or looking at a dead schedule. */
  target: 'run' | 'workflow';
}

const KIND_META: Record<string, KindMeta> = {
  run_failed:         { icon: XCircle,      label: 'Run failed',      target: 'run' },
  run_recovered:      { icon: CheckCircle2, label: 'Recovered',       target: 'run' },
  workflow_paused:    { icon: PauseCircle,  label: 'Auto-paused',     target: 'workflow' },
  sla_breached:       { icon: Timer,        label: 'SLA breached',    target: 'run' },
  run_missed:         { icon: CalendarX,    label: 'Schedule missed', target: 'workflow' },
  approval_requested: { icon: ShieldAlert,  label: 'Needs approval',  target: 'run' },
};

const FALLBACK_META: KindMeta = { icon: AlertTriangle, label: 'Activity', target: 'run' };

const metaFor = (kind: string): KindMeta => KIND_META[kind] ?? FALLBACK_META;

/** Severity drives a `--sev` triplet on the row, so the disc, the rail and the
 *  eyebrow all read one token set instead of four hand-picked colours. */
const SEVERITY_CLASS: Record<string, string> = {
  error:   'notif-sev-error',
  warning: 'notif-sev-warning',
  info:    'notif-sev-info',
  success: 'notif-sev-success',
};

function eventHref(event: ActivityEvent): string {
  const meta = metaFor(event.kind);
  if (meta.target === 'run' && event.run_id != null) return `/runs/${event.run_id}`;
  return `/workflows/${event.workflow_id}`;
}

function destinationLabel(event: ActivityEvent): string {
  const meta = metaFor(event.kind);
  return meta.target === 'run' && event.run_id != null
    ? `Run #${event.run_id}`
    : event.workflow_name;
}

/* ─── Feed hook ──────────────────────────────────────────── */

export interface ActivityFeedOptions {
  /** Poll cadence while the tab is visible. Default 45s. */
  pollMs?: number;
  /** Rows requested; the server caps at 200. Default 40. */
  limit?: number;
  /** Look-back window in hours; the server caps at 720. Default 168 (7 days). */
  windowHours?: number;
  /** Last-read instant, sent as `read_at`. Null means "nothing read yet". */
  readAt?: string | null;
}

export interface ActivityFeedState {
  feed: ActivityFeed | null;
  /** True only until the first response lands — later polls never re-blank. */
  loading: boolean;
  /** Last failure. `feed` is kept alongside it, so a blip shows stale-but-real. */
  error: string | null;
  reload: () => void;
}

/** Polls GET /api/activity. Exported because the same feed is a reasonable
 *  dashboard panel; the bell is just its first consumer. */
export function useActivityFeed(options: ActivityFeedOptions = {}): ActivityFeedState {
  const {
    pollMs = DEFAULT_POLL_MS,
    limit = DEFAULT_LIMIT,
    windowHours = DEFAULT_WINDOW_HOURS,
    readAt = null,
  } = options;

  const [feed, setFeed] = useState<ActivityFeed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Read at fetch time rather than baked into the effect: marking read changes
  // read_at on every poll while the panel is open, and that must not tear down
  // and restart the interval each time.
  const readRef = useRef(readAt);
  readRef.current = readAt;
  const aliveRef = useRef(true);
  const inflight = useRef(false);

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const load = useCallback(() => {
    // A slow response must not queue a second request behind it; the interval
    // keeps firing regardless of how long the API takes.
    if (inflight.current) return;
    inflight.current = true;
    const params = new URLSearchParams({
      limit: String(limit), window_hours: String(windowHours),
    });
    if (readRef.current) params.set('read_at', readRef.current);
    api<ActivityFeed>(`${FEED_PATH}?${params.toString()}`)
      .then(next => {
        if (!aliveRef.current) return;
        setFeed(next);
        setError(null);
      })
      .catch((err: unknown) => {
        // The last good feed is deliberately kept: a one-off blip should not
        // empty the panel someone is reading.
        if (aliveRef.current) {
          setError(err instanceof Error ? err.message : 'Could not load activity');
        }
      })
      .finally(() => {
        inflight.current = false;
        if (aliveRef.current) setLoading(false);
      });
  }, [limit, windowHours]);

  useEffect(() => {
    let timer = 0;
    const start = () => {
      window.clearInterval(timer);
      timer = window.setInterval(() => { if (!document.hidden) load(); }, pollMs);
    };
    // Wallboard-adjacent: a tab left open for a week must not burn a request a
    // minute behind someone's back, and must not come back showing Tuesday.
    const onVisibility = () => {
      if (document.hidden) {
        window.clearInterval(timer);
        timer = 0;
      } else {
        load();
        start();
      }
    };
    if (!document.hidden) { load(); start(); }
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [load, pollMs]);

  // A new read stamp changes what `unread` means, so re-ask immediately. On
  // mount this coincides with the interval effect's first load; the inflight
  // guard collapses the two into one request.
  useEffect(() => {
    if (!document.hidden) load();
  }, [readAt, load]);

  return { feed, loading, error, reload: load };
}

/* ─── Popover placement ──────────────────────────────────────
   Portaled to <body> so the topbar's backdrop-filter cannot clip
   or repaint it, and re-placed on scroll/resize so it stays welded
   to the bell. z-index is inline for the same reason SnoozeControl
   sets it inline: it has to be true from the first paint. */
function usePlacement(anchor: HTMLElement | null, open: boolean) {
  const ref = useRef<HTMLDivElement>(null);
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
      const width = panel.offsetWidth;
      // Right-aligned to the trigger: the bell sits at the end of the topbar,
      // so a left-aligned panel would hang off the viewport on every screen.
      const left = Math.max(12, Math.min(box.right - width, window.innerWidth - width - 12));
      setStyle({
        position: 'fixed', zIndex: 120, top: box.bottom + 8, left, visibility: 'visible',
        // The room actually left below the bell, handed to CSS as a variable
        // rather than as an inline max-height: the stylesheet still gets to
        // impose the smaller aesthetic cap, and geometry stays measured.
        '--notif-max-h': `${Math.max(220, window.innerHeight - box.bottom - 20)}px`,
      } as CSSProperties);
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

/* ─── Grouping ───────────────────────────────────────────── */

/** The row's position in the whole flattened list, not in its group — the entry
 *  stagger has to keep running across a day heading or it restarts mid-panel. */
interface DayRow { event: ActivityEvent; index: number }
interface DayGroup { label: string; rows: DayRow[] }

/** Newest-first order is preserved exactly; this only inserts the day headings.
 *  Grouping by the VIEWER's midnight, which is the "today" an operator means. */
function groupByDay(events: ActivityEvent[], now: number): DayGroup[] {
  const reference = new Date(now);
  const groups: DayGroup[] = [];
  events.forEach((event, index) => {
    const label = dayLabel(event.at, reference);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.rows.push({ event, index });
    else groups.push({ label, rows: [{ event, index }] });
  });
  return groups;
}

/* ─── Row ────────────────────────────────────────────────── */

function NotificationRow({
  event, unread, index, now, onNavigate,
}: {
  event: ActivityEvent;
  unread: boolean;
  index: number;
  now: number;
  onNavigate: () => void;
}) {
  const meta = metaFor(event.kind);
  const Icon = meta.icon;
  return (
    <Link
      to={eventHref(event)}
      onClick={onNavigate}
      className={clsx(
        'notif-item',
        SEVERITY_CLASS[event.severity] ?? 'notif-sev-info',
        unread && 'is-unread',
      )}
      style={{ '--i': index } as CSSProperties}
      title={absoluteTime(event.at)}
    >
      <span className="notif-item-disc" aria-hidden="true">
        <Icon size={13} strokeWidth={2.5} />
      </span>
      <span className="notif-item-body">
        <span className="notif-item-top">
          <span className="notif-item-kind">
            {meta.label}
            {unread && <span className="notif-item-dot" aria-hidden="true" />}
          </span>
          <time className="notif-item-when" dateTime={event.at}>
            {timeAgo(event.at, now)}
          </time>
        </span>
        <span className="notif-item-title">{event.title}</span>
        <span className="notif-item-go">{destinationLabel(event)}</span>
      </span>
      <ChevronRight className="notif-item-chevron" size={14} aria-hidden="true" />
      {unread && <span className="notif-sr">Unread</span>}
    </Link>
  );
}

/* ─── The bell ───────────────────────────────────────────── */

export interface NotificationBellProps {
  /** Extra classes on the trigger — it is a topbar sibling of .cmd-k-btn. */
  className?: string;
  /** Poll cadence while the tab is visible. Default 45s. */
  pollMs?: number;
  /** Rows requested per poll. Default 40. */
  limit?: number;
  /** Look-back window in hours. Default 168 (7 days). */
  windowHours?: number;
}

export function NotificationBell({
  className, pollMs, limit, windowHours,
}: NotificationBellProps) {
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [readAt, setReadAt] = useState<string | null>(() => loadReadAt());

  // The boundary the ROWS are drawn against, frozen at the moment of opening.
  // Opening marks everything read; if the rows flipped read at the same instant
  // the panel would erase the very thing it was opened to show. It advances
  // only on "mark all read", where erasing them is the point.
  const [boundary, setBoundary] = useState<string | null>(readAt);

  const { feed, loading, error, reload } = useActivityFeed({
    pollMs, limit, windowHours, readAt,
  });
  const { ref: panelRef, style } = usePlacement(anchor, open);
  const now = useTicker(open);

  /* Marking read. The stamp is the server's generated_at, never a local clock;
     while the panel stays open every poll advances it, so a feed watched for an
     hour does not accumulate a phantom badge. */
  useEffect(() => {
    if (!open || !feed || readAt === feed.generated_at) return;
    setReadAt(feed.generated_at);
    saveReadAt(feed.generated_at);
  }, [open, feed, readAt]);

  // The server's count, except in the instant between stamping read and the
  // confirming response — where it would otherwise linger for a round trip.
  const unread = useMemo(() => {
    if (!feed) return 0;
    return readAt && ms(readAt) >= ms(feed.generated_at) ? 0 : feed.unread;
  }, [feed, readAt]);

  const groups = useMemo(
    () => groupByDay(feed?.events ?? [], now),
    [feed, now],
  );
  const visibleUnread = useMemo(
    () => (feed?.events ?? []).filter(event => ms(event.at) > ms(boundary)).length,
    [feed, boundary],
  );

  /* A nudge when the count grows, never as the only carrier of the state — the
     badge itself is. Cleared by a timer so it can replay on the next arrival. */
  const [ringing, setRinging] = useState(false);
  const previousUnread = useRef(unread);
  const seeded = useRef(false);
  useEffect(() => {
    if (!feed) return;
    // The first snapshot is not an arrival — a page load finding four old
    // failures should not shake the topbar as if they had just happened.
    const grew = seeded.current && unread > previousUnread.current;
    previousUnread.current = unread;
    seeded.current = true;
    if (!grew) return;
    setRinging(true);
    const timer = window.setTimeout(() => setRinging(false), 700);
    return () => window.clearTimeout(timer);
  }, [unread, feed]);

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) anchor?.focus();
  }, [anchor]);

  const openPanel = () => {
    setBoundary(readAt);   // snapshot before the mark-read effect moves it
    setOpen(true);
  };

  const markAllRead = () => {
    if (!feed) return;
    setReadAt(feed.generated_at);
    saveReadAt(feed.generated_at);
    setBoundary(feed.generated_at);   // here, clearing the highlight IS the action
  };

  /* Focus moves into the panel on open so a keyboard user is not stranded at
     the topbar with a dialog they cannot reach. */
  useEffect(() => {
    if (open) panelRef.current?.focus({ preventScroll: true });
  }, [open, panelRef]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing) return;
      if (e.key === 'Escape') {
        // Claimed, so an enclosing Modal does not close underneath the panel.
        e.preventDefault();
        close(true);
        return;
      }
      const panel = panelRef.current;
      if (!panel) return;
      const active = document.activeElement as HTMLElement | null;
      if (e.key === 'Tab') {
        // Portaled to <body>: without wrapping, Tab walks out of the panel into
        // the end of the document instead of around the list.
        const items = Array.from(
          panel.querySelectorAll<HTMLElement>('a[href],button:not([disabled])'),
        ).filter(el => el.offsetParent !== null);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && (active === first || active === panel)) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && active === last) {
          e.preventDefault();
          first.focus();
        }
        return;
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        const rows = Array.from(panel.querySelectorAll<HTMLElement>('.notif-item'));
        if (!rows.length) return;
        e.preventDefault();
        const index = active ? rows.indexOf(active) : -1;
        const next = e.key === 'ArrowDown'
          ? (index < 0 ? 0 : Math.min(rows.length - 1, index + 1))
          : (index < 0 ? rows.length - 1 : Math.max(0, index - 1));
        rows[next].focus();
      }
    };
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      // The trigger is excluded so its own click toggles rather than
      // closing-then-reopening.
      if (panelRef.current?.contains(target) || anchor?.contains(target)) return;
      close();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [open, anchor, close, panelRef]);

  const hours = feed?.window_hours ?? windowHours ?? DEFAULT_WINDOW_HOURS;
  const scope = windowLabel(hours);
  const degraded = Boolean(error) && !feed;
  const badge = unread > 99 ? '99+' : String(unread);

  return (
    <>
      <button
        type="button"
        ref={setAnchor}
        className={clsx(
          'notif-bell',
          unread > 0 && 'has-unread',
          ringing && 'is-ringing',
          open && 'is-open',
          degraded && 'is-degraded',
          className,
        )}
        onClick={() => (open ? close(true) : openPanel())}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
        title={degraded ? 'Activity feed unavailable' : 'Notifications'}
      >
        <Bell className="notif-bell-icon" size={15} strokeWidth={2} />
        {unread > 0 && (
          // Keyed on the value so a change remounts the chip and replays its
          // landing, the same trick StatusBadge uses.
          <span key={badge} className="notif-bell-badge" aria-hidden="true">{badge}</span>
        )}
      </button>
      {/* Always mounted: a live region inserted at the same time as its text is
          not reliably announced. */}
      <span className="notif-sr" aria-live="polite" role="status">
        {unread > 0 ? `${unread} unread notification${unread === 1 ? '' : 's'}` : ''}
      </span>

      {open && createPortal(
        <div
          id={panelId}
          ref={panelRef}
          style={style}
          className="notif-panel"
          role="dialog"
          aria-label="Notifications"
          tabIndex={-1}
        >
          <header className="notif-panel-head">
            <div className="notif-panel-heading">
              <h3>Notifications</h3>
              <p>
                {visibleUnread > 0
                  ? `${visibleUnread} new · ${scope}`
                  : `Failures, gates and misses · ${scope}`}
              </p>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={markAllRead}
              disabled={visibleUnread === 0}
              title="Mark everything as read"
            >
              <CheckCheck size={12} /> Mark all read
            </Button>
          </header>

          <div className="notif-scroll">
            {error && feed && (
              <div className="notif-stale" role="status">
                <WifiOff size={12} />
                <span>Live updates interrupted — showing the last snapshot.</span>
                <button type="button" className="notif-stale-retry" onClick={reload}>Retry</button>
              </div>
            )}

            {loading && !feed && (
              <div className="notif-skeletons" aria-hidden="true">
                {[0, 1, 2].map(index => (
                  <div className="notif-skel" key={index}>
                    <span className="skeleton-line notif-skel-disc" />
                    <span className="notif-skel-lines">
                      <span className="skeleton-line notif-skel-a" />
                      <span className="skeleton-line notif-skel-b" />
                    </span>
                  </div>
                ))}
              </div>
            )}
            {loading && !feed && <span className="notif-sr">Loading notifications</span>}

            {degraded && !loading && (
              <div className="notif-state notif-state--error">
                <WifiOff size={20} />
                <strong>Activity is unavailable</strong>
                <span>{error}</span>
                <Button variant="ghost" size="sm" onClick={reload}>Try again</Button>
              </div>
            )}

            {feed && feed.events.length === 0 && (
              <div className="notif-empty">
                <EmptyState
                  icon={<CheckCheck size={20} />}
                  title="You’re all caught up"
                  text={`Nothing has failed, stalled or asked for you in the ${scope}. New activity lands here the moment it happens.`}
                />
              </div>
            )}

            {groups.map(group => (
              <section className="notif-group" key={group.label}>
                <h4 className="notif-group-label">{group.label}</h4>
                {group.rows.map(({ event, index }) => (
                  <NotificationRow
                    key={event.id}
                    event={event}
                    index={index}
                    unread={ms(event.at) > ms(boundary)}
                    now={now}
                    onNavigate={() => close()}
                  />
                ))}
              </section>
            ))}
          </div>

          <footer className="notif-panel-foot">
            <span className="notif-foot-count">
              {feed
                ? feed.total > feed.events.length
                  ? `Showing ${feed.events.length} of ${feed.total}`
                  : `${feed.total} event${feed.total === 1 ? '' : 's'} · ${scope}`
                : scope}
            </span>
            <Link to="/runs" className="notif-foot-link" onClick={() => close()}>
              <Inbox size={12} /> All runs <ChevronRight size={12} />
            </Link>
          </footer>
        </div>,
        document.body,
      )}
    </>
  );
}
