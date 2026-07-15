import { CSSProperties, ReactNode, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  CheckCircle2, XCircle, Loader2, Clock, CircleDot,
  MinusCircle, AlertCircle, AlertTriangle
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
    const start = performance.now();
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      setValue(Math.round(from + (target - from) * eased));
      if (progress < 1) raf = requestAnimationFrame(tick);
      else { fromRef.current = target; setValue(target); }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
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
};

export function StatusBadge({ value }: { value: string }) {
  const cfg = STATUS_MAP[value] ?? { cls: 'status-muted', icon: CircleDot, label: value };
  const Icon = cfg.icon;
  return (
    <span className={clsx('status-badge', cfg.cls, value === 'running' && 'status-pulsing')}>
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

/* ─── Loading bar ─────────────────────────────────────
   The comet dot-matrix as a reusable indeterminate loader.
   `overRatio` (0..1) optionally crystallizes it toward amber when the
   awaited thing is running past its expected duration. */
export function LoadingBar({ tone, size, className, overRatio }: {
  tone?: 'running' | 'warning' | 'success'; size?: 'sm'; className?: string;
  overRatio?: number;
}) {
  return (
    <div className={clsx('dot-loader', size, tone && tone !== 'running' && tone, className)}
         role="progressbar" aria-label="In progress"
         style={overRatio ? { '--over-ratio': Math.min(1, Math.max(0, overRatio)).toFixed(3) } as CSSProperties : undefined} />
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
export function Modal({
  title, subtitle, onClose, children
}: { title: string; subtitle?: string; onClose: () => void; children: ReactNode }) {
  return createPortal(
    <div
      className="modal-shade"
      onMouseDown={e => e.target === e.currentTarget && onClose()}
    >
      <section className="modal">
        <div className="modal-head">
          <div>
            <h3 className="modal-title">{title}</h3>
            {subtitle && <p className="modal-subtitle">{subtitle}</p>}
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </section>
    </div>,
    document.body
  );
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
