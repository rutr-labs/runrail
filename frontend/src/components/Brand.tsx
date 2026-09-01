/* ─── The mark ─────────────────────────────────────────────
   Two rails leaving the bottom edge and curving away to the right, the inner
   one carrying the accent.

   Inlined rather than imported from docs/brand/*.svg, because the four file
   variants (light, dark, mono, mono-light) differ only in the ink colour.
   `currentColor` takes that from whatever the mark sits in, so one component
   covers all four, themes with the app instead of needing a file swap at the
   theme boundary, and costs no request. The geometry is copied verbatim from
   the source files — if those change, this is the one place to update.

   docs/brand holds the canonical files for anything outside the app (README,
   press, anyone who needs a PNG), and frontend/public/favicon.svg is the one
   the browser tab loads. */

const VIEWBOX = '0 0 64 64';
/** Outer rail: ink, inherited from context. */
const OUTER = 'M 14 58 V 30 C 14 19, 24 12, 36 12 H 58';
/** Inner rail: the accent, unless the mark is asked to be monochrome. */
const INNER = 'M 26 58 V 32 C 26 27, 31 24, 38 24 H 58';

export function RunRailMark({ size = 30, mono = false, className, title = 'RunRail' }: {
  size?: number;
  /** Single-colour, for places where the accent would compete or print. */
  mono?: boolean;
  className?: string;
  /** Empty string marks it decorative, for use beside a visible wordmark. */
  title?: string;
}) {
  return (
    <svg
      viewBox={VIEWBOX}
      width={size}
      height={size}
      className={className}
      fill="none"
      role={title ? 'img' : 'presentation'}
      aria-label={title || undefined}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      {title && <title>{title}</title>}
      <path d={OUTER} stroke="currentColor" strokeWidth={6} />
      <path d={INNER} stroke={mono ? 'currentColor' : 'var(--brand)'} strokeWidth={6} />
    </svg>
  );
}
