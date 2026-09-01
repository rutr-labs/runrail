import { createContext, CSSProperties, ReactNode, useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { attachComet, CometKind } from '../comet';
import {
  CheckCircle2, XCircle, Loader2, Clock, CircleDot,
  MinusCircle, AlertCircle, AlertTriangle, X, ShieldAlert, ShieldCheck, CircleSlash
} from 'lucide-react';
import clsx from 'clsx';

/* ─── Animated counter ───────────────────────────────── */
const REDUCED_MOTION = typeof window !== 'undefined'
  && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

/** Eases from the previous value to `target` on genuine live changes.
 *  Shows the value immediately on first load / when coming from 0, so metrics
 *  never dwell at 0 while data arrives (which also kept breaking screenshots). */
function useCountUp(target: number, duration = 600): number {
  const fromRef = useRef(target);
  const [value, setValue] = useState(target);
  useEffect(() => {
    const from = fromRef.current;
    if (REDUCED_MOTION || from === target || from === 0) {
      fromRef.current = target;
      setValue(target);
      return;
    }
    let raf = 0;
    let shown = from;
    const start = performance.now();
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      shown = Math.round(from + (target - from) * eased);
      setValue(shown);
      if (progress < 1) raf = requestAnimationFrame(tick);
      else { fromRef.current = target; setValue(target); }
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      // Resume from what is on screen. Without this a second change mid-flight
      // restarts from the value this animation began at, so a counter easing
      // 10 → 20 that is updated to 25 halfway visibly snaps back to 10 first.
      fromRef.current = shown;
    };
  }, [target, duration]);
  return value;
}

function CountUp({ value }: { value: number }) {
  return <>{useCountUp(value)}</>;
}

/* ─── Button ──────────────────────────────────────────── */
interface ButtonProps {
  children: ReactNode;
  onClick?: () => void;
  variant?: 'primary' | 'ghost' | 'danger' | 'secondary';
  size?: 'sm' | 'md';
  type?: 'button' | 'submit';
  disabled?: boolean;
  className?: string;
  title?: string;
}
export function Button({
  children, onClick, variant = 'primary', size = 'md',
  type = 'button', disabled = false, className, title
}: ButtonProps) {
  return (
    <button
      type={type}
      className={clsx('btn', `btn-${variant}`, `btn-${size}`, className)}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}

/* ─── Status Badge ────────────────────────────────────── */
const STATUS_MAP: Record<string, {
  cls: string; icon: React.FC<{ size?: number; className?: string; strokeWidth?: number }>; label: string
}> = {
  success:  { cls: 'status-success',  icon: CheckCircle2,  label: 'Success' },
  failed:   { cls: 'status-danger',   icon: XCircle,       label: 'Failed' },
  running:  { cls: 'status-running',  icon: Loader2,       label: 'Running' },
  queued:   { cls: 'status-queued',   icon: Clock,         label: 'Queued' },
  enabled:  { cls: 'status-success',  icon: CircleDot,     label: 'Enabled' },
  disabled: { cls: 'status-muted',    icon: MinusCircle,   label: 'Disabled' },
  warning:  { cls: 'status-warning',  icon: AlertTriangle, label: 'Warning' },
  error:    { cls: 'status-danger',   icon: AlertCircle,   label: 'Error' },
  skipped:  { cls: 'status-muted',    icon: MinusCircle,   label: 'Skipped' },
  cancelled:{ cls: 'status-muted',    icon: MinusCircle,   label: 'Cancelled' },
  // Approval gates. A run parked on one is warning-toned, never danger: it is
  // waiting on a person, not broken.
  waiting_approval:  { cls: 'status-warning', icon: ShieldAlert, label: 'Waiting approval' },
  awaiting_approval: { cls: 'status-warning', icon: ShieldAlert, label: 'Awaiting approval' },
  approved:          { cls: 'status-success', icon: ShieldCheck, label: 'Approved' },
  rejected:          { cls: 'status-danger',  icon: CircleSlash, label: 'Rejected' },
};

export function StatusBadge({ value }: { value: string }) {
  const cfg = STATUS_MAP[value] ?? { cls: 'status-muted', icon: CircleDot, label: value };
  const Icon = cfg.icon;
  return (
    // Keyed on value: a status change remounts the chip so badge-land replays.
    <span key={value} className={clsx('status-badge', cfg.cls, value === 'running' && 'status-pulsing')}>
      <Icon size={11} strokeWidth={2.5} className={value === 'running' ? 'icon-spin' : ''} />
      {cfg.label}
    </span>
  );
}

/* ─── Glass Card ─────────────────────────────────────── */
export function GlassCard({
  children, className, glow
}: { children: ReactNode; className?: string; glow?: boolean }) {
  return (
    <div className={clsx('glass-card', glow && 'glass-card--glow', className)}>
      {children}
    </div>
  );
}

/* ─── Card Header ────────────────────────────────────── */
export function CardHeader({
  title, subtitle, action
}: { title: ReactNode; subtitle?: ReactNode; action?: ReactNode }) {
  return (
    <div className="card-header">
      <div>
        <h2 className="card-title">{title}</h2>
        {subtitle && <p className="card-subtitle">{subtitle}</p>}
      </div>
      {action && <div className="card-action">{action}</div>}
    </div>
  );
}

/* ─── Page Header ────────────────────────────────────── */
export function PageHeader({
  eyebrow, title, subtitle, action
}: { eyebrow?: string; title: string; subtitle?: string; action?: ReactNode }) {
  return (
    <header className="page-header">
      <div className="page-header-text">
        {eyebrow && <span className="eyebrow-label">{eyebrow}</span>}
        <h1 className="page-title">{title}</h1>
        {subtitle && <p className="page-subtitle">{subtitle}</p>}
      </div>
      {action && <div className="page-header-action">{action}</div>}
    </header>
  );
}

/* ─── Metric Card ────────────────────────────────────── */
export function MetricCard({
  icon, label, value, tone = 'default', note
}: {
  icon: ReactNode; label: string; value: ReactNode;
  tone?: 'default' | 'success' | 'danger' | 'running' | 'warning';
  note?: string;
}) {
  return (
    <div className={clsx('metric-card', `metric-${tone}`)}>
      <div className="metric-icon-wrap">{icon}</div>
      <div className="metric-body">
        <span className="metric-label">{label}</span>
        <strong className="metric-value" style={{ fontVariantNumeric: 'tabular-nums' }}>
          {typeof value === 'number' ? <CountUp value={value} /> : value}
        </strong>
        {note && <span className="metric-note">{note}</span>}
      </div>
    </div>
  );
}

/* ─── Particle comet ─────────────────────────────────
   Canvas overlay for live progress surfaces. Mount inside any
   positioned, overflow-hidden bar; the shared engine in comet.ts
   sizes it to the host and drives it from one rAF loop.
   kind='fill' anchors the comet head at the host's right edge
   (the progress frontier); kind='loader' loops it; kind='still'
   paints the landed comet once for finished bars. */
export function CometCanvas({ kind }: { kind: CometKind }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => attachComet(ref.current!, kind), [kind]);
  return <canvas ref={ref} className="comet-canvas" aria-hidden="true" />;
}

/* ─── Loading bar ─────────────────────────────────────
   The particle comet as a reusable indeterminate loader.
   `overRatio` (0..1) optionally crystallizes it toward amber when the
   awaited thing is running past its expected duration. */
export function LoadingBar({ tone, size, className, overRatio }: {
  tone?: 'running' | 'warning' | 'success'; size?: 'sm'; className?: string;
  overRatio?: number;
}) {
  return (
    <div className={clsx('dot-loader', size, tone && tone !== 'running' && tone, className)}
         role="progressbar" aria-label="In progress"
         style={overRatio ? { '--over-ratio': Math.min(1, Math.max(0, overRatio)).toFixed(3) } as CSSProperties : undefined}>
      <CometCanvas kind="loader" />
    </div>
  );
}

/* ─── Empty State ────────────────────────────────────── */
export function EmptyState({
  icon, title, text, action
}: { icon: ReactNode; title: string; text: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-icon">{icon}</div>
      <h3 className="empty-title">{title}</h3>
      <p className="empty-text">{text}</p>
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}

/* ─── Modal ──────────────────────────────────────────── */
/* Portaled to <body>: the app shell blurs and recedes behind open dialogs
   (depth-of-field), and a filtered ancestor would otherwise drag the
   fixed-position dialog into its own blur. */

/* Children (Cancel buttons etc.) reach the ANIMATED close through this —
   calling the raw onClose prop skips the 160ms exit choreography. */
const ModalCloseContext = createContext<() => void>(() => {});
export const useModalClose = () => useContext(ModalCloseContext);

export function Modal({
  title, subtitle, onClose, children, wide
}: { title: string; subtitle?: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  // Exit must outlive the .closing exit animation (160ms in style.css)
  // before the caller unmounts us; the ref guards against double-fire.
  const close = () => {
    if (closingRef.current) return;
    closingRef.current = true;
    setClosing(true);
    setTimeout(onClose, 160);
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing) return;
      // Overlays above us (command palette, file browser) mark Escape
      // handled; a native <select> popup's Escape targets the select.
      if (e.defaultPrevented) return;
      if ((e.target as HTMLElement | null)?.tagName === 'SELECT') return;
      close();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return createPortal(
    <div
      className={clsx('modal-shade', closing && 'closing')}
      onMouseDown={e => e.target === e.currentTarget && close()}
    >
      <section className={clsx('modal', wide && 'modal--wide')}>
        <div className="modal-head">
          <div>
            <h3 className="modal-title">{title}</h3>
            {subtitle && <p className="modal-subtitle">{subtitle}</p>}
          </div>
          <button className="modal-close" onClick={close} aria-label="Close"><X size={16} /></button>
        </div>
        <ModalCloseContext.Provider value={close}>
          {children}
        </ModalCloseContext.Provider>
      </section>
    </div>,
    document.body
  );
}

/* Modal footer Cancel — routes through the animated close so dismissing
   with the button matches Escape/shade/X instead of blinking out. */
export function CancelButton({ children = 'Cancel' }: { children?: ReactNode }) {
  const close = useModalClose();
  return <Button variant="ghost" onClick={close}>{children}</Button>;
}

/* ─── Health Chip ────────────────────────────────────── */
export function HealthChip({
  label, status
}: { label: string; status: 'online' | 'offline' | 'unknown' }) {
  const dot = status === 'online' ? 'dot-online' : status === 'offline' ? 'dot-offline' : 'dot-unknown';
  return (
    <span className={clsx('health-chip', `health-${status}`)}>
      <span className={clsx('health-dot', dot)} />
      {label}
    </span>
  );
}

/* ─── Skeleton ───────────────────────────────────────── */
export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx('skeleton-line', className)} />;
}
export function SkeletonCard() {
  return (
    <div className="glass-card skeleton-card">
      <Skeleton className="sk-title" />
      <Skeleton className="sk-text" />
      <Skeleton className="sk-text sk-short" />
    </div>
  );
}

/* ─── Type Icon chip ─────────────────────────────────── */
const TYPE_COLORS: Record<string, string> = {
  shell: 'type-shell', python: 'type-python', notebook: 'type-notebook', sql: 'type-sql'
};
export function TaskTypeBadge({ type }: { type: string }) {
  const labels: Record<string, string> = { shell: '›_', python: 'Py', notebook: 'Nb', sql: 'SQL' };
  return (
    <span className={clsx('task-type-badge', TYPE_COLORS[type] ?? 'type-shell')}>
      {labels[type] ?? type}
    </span>
  );
}
